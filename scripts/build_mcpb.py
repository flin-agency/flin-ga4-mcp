"""Build the versioned desktop bundle from an explicit list of runtime files."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 development environments
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "flin_ga4_mcp"


def release_version(root: Path, tag: str | None = None) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Desktop releases require a stable major.minor.patch version.")
    tree = ast.parse((root / "src" / PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    package_version = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    )
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked_version = next(item["version"] for item in lock["package"] if item["name"] == project["name"])
    if package_version != version or locked_version != version:
        raise ValueError("Project, package and uv.lock versions must match before release.")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag must be v{version}.")
    return version


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Bundle inputs must be regular files: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def stage_bundle(root: Path, destination: Path, *, tag: str | None = None) -> dict:
    version = release_version(root, tag)
    manifest = json.loads((root / "mcpb" / "manifest.template.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "README.md", "PRIVACY.md", "LICENSE", "SUPPORT.md"):
        _copy_file(root / name, destination / name)
    _copy_file(root / "assets" / "icon.png", destination / "icon.png")
    _copy_file(root / "mcpb" / "server.py", destination / "server.py")
    source_dir = root / "src" / PACKAGE
    if source_dir.is_symlink():
        raise ValueError("The package directory must not be a symlink.")
    for source in sorted(source_dir.rglob("*")):
        if source.is_symlink():
            raise ValueError("Symlinks are not allowed in the runtime package.")
        if source.is_file() and source.suffix == ".py":
            _copy_file(source, destination / source.relative_to(root))
    return manifest


def build_bundle(root: Path, output_dir: Path, *, tag: str | None = None) -> Path:
    cli = root / "node_modules" / "@anthropic-ai" / "mcpb" / "dist" / "cli" / "cli.js"
    node = shutil.which("node")
    if node is None or not cli.is_file():
        raise RuntimeError("Install Node.js and run npm ci --ignore-scripts before building.")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flin-mcpb-build-") as temporary:
        staging = Path(temporary)
        manifest = stage_bundle(root, staging, tag=tag)
        output = output_dir.resolve() / f"{manifest['name']}-{manifest['version']}.mcpb"
        subprocess.run([node, str(cli), "validate", str(staging / "manifest.json")], check=True)
        subprocess.run([node, str(cli), "pack", str(staging), str(output)], check=True)
        expected = {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()}
        with zipfile.ZipFile(output) as archive:
            actual = {item.filename for item in archive.infolist() if not item.is_dir()}
            if actual != expected:
                raise RuntimeError("Packed files differ from the runtime allowlist.")
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    (output_dir / "SHA256SUMS").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "mcpb")
    parser.add_argument("--tag", help="Require the Git tag to match the project version.")
    args = parser.parse_args()
    print(build_bundle(ROOT, args.output_dir, tag=args.tag))


if __name__ == "__main__":
    main()
