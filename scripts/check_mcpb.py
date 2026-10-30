"""Smoke-test a locally built bundle with its locked Python runtime and real MCP stdio."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check_bundle(bundle: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='flin-ga4-smoke-') as directory:
        root = Path(directory)
        with zipfile.ZipFile(bundle) as archive:
            for name in archive.namelist():
                if Path(name).is_absolute() or '..' in Path(name).parts:
                    raise ValueError('Unsafe archive path')
            archive.extractall(root)
        manifest = json.loads((root / 'manifest.json').read_text())
        env = {key: value for key, value in os.environ.items() if not key.startswith('GOOGLE_')}
        params = StdioServerParameters(
            command='uv', args=['run', '--directory', str(root), '--locked', '--no-dev',
                                '--python', '3.12', str(root / 'server.py')], env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                await session.initialize()
                listing = await session.list_tools()
                if {t.name for t in listing.tools} != {t['name'] for t in manifest['tools']}:
                    raise RuntimeError('Bundle manifest and discovered tools differ')
                result = await session.call_tool('health_check', {})
                if result.isError or result.structuredContent['status'] != 'missing_configuration':
                    raise RuntimeError('Bundle health check failed')
        print(f'Bundle startup, tool discovery and offline health check passed: {bundle.name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    asyncio.run(check_bundle(parser.parse_args().bundle))
