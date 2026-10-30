from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from scripts.build_mcpb import PACKAGE, ROOT, release_version, stage_bundle


@pytest.fixture
def bundle_source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    for filename in ("pyproject.toml", "uv.lock", "README.md", "PRIVACY.md", "LICENSE", "SUPPORT.md"):
        shutil.copyfile(ROOT / filename, root / filename)
    shutil.copytree(ROOT / "assets", root / "assets")
    shutil.copytree(ROOT / "mcpb", root / "mcpb")
    shutil.copytree(ROOT / "src" / PACKAGE, root / "src" / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_bundle_excludes_credentials_and_development_files(bundle_source: Path, tmp_path: Path) -> None:
    forbidden_files = [
        ".env", "token.json", "work.json", "tests/secrets.py", ".git/config",
        ".venv/token.json", f"src/{PACKAGE}/token.json",
        f"src/{PACKAGE}/__pycache__/server.pyc",
    ]
    for filename in forbidden_files:
        path = bundle_source / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("credential-marker", encoding="utf-8")
    staging = tmp_path / "staged"

    manifest = stage_bundle(bundle_source, staging)

    expected = {"manifest.json", "pyproject.toml", "uv.lock", "README.md", "server.py", "PRIVACY.md", "LICENSE", "SUPPORT.md", "icon.png"}
    expected.update(path.relative_to(bundle_source).as_posix() for path in (bundle_source / "src").rglob("*.py"))
    assert {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()} == expected
    assert all(b"credential-marker" not in path.read_bytes() for path in staging.rglob("*") if path.is_file())
    assert manifest["version"] == release_version(bundle_source)
    assert manifest["user_config"]["google_client_secret"]["sensitive"] is True
    assert "--locked" in manifest["server"]["mcp_config"]["args"]


def test_bundle_version_matches_tag_and_package(bundle_source: Path) -> None:
    version = release_version(bundle_source)
    assert release_version(bundle_source, f"v{version}") == version
    with pytest.raises(ValueError, match="Release tag must"):
        release_version(bundle_source, "v9.9.9")


@pytest.mark.parametrize("changed_file", ["uv.lock", f"src/{PACKAGE}/__init__.py"])
def test_bundle_rejects_inconsistent_versions(bundle_source: Path, changed_file: str) -> None:
    version = release_version(bundle_source)
    path = bundle_source / changed_file
    path.write_text(path.read_text(encoding="utf-8").replace(f'"{version}"', '"9.9.9"'), encoding="utf-8")

    with pytest.raises(ValueError, match="versions must match"):
        release_version(bundle_source)


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may require Windows developer mode")
def test_bundle_refuses_symlinked_runtime_files(bundle_source: Path, tmp_path: Path) -> None:
    secret = tmp_path / "private-file"
    secret.write_text("credential-marker", encoding="utf-8")
    (bundle_source / "src" / PACKAGE / "leak.py").symlink_to(secret)

    with pytest.raises(ValueError, match="Symlinks"):
        stage_bundle(bundle_source, tmp_path / "staged")


def test_staged_manifest_references_existing_entrypoint(bundle_source: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staged"
    stage_bundle(bundle_source, staging)
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    assert (staging / manifest["server"]["entry_point"]).is_file()
