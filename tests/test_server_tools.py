from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import httplib2
import pytest
from googleapiclient.errors import HttpError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from flin_ga4_mcp import server


TOOLS = {'health_check', 'list_properties', 'get_property', 'get_metadata', 'check_compatibility',
         'run_report', 'run_realtime_report', 'get_summary', 'get_traffic_sources', 'get_top_pages', 'get_events'}


def test_health_without_config(monkeypatch):
    monkeypatch.delenv('GOOGLE_CLIENT_ID', raising=False)
    monkeypatch.delenv('GOOGLE_CLIENT_SECRET', raising=False)
    result = server.health_check()
    assert result['ok'] is False
    assert result['status'] == 'missing_configuration'


def test_health_is_offline(monkeypatch, tmp_path):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'secret')
    monkeypatch.setenv('GOOGLE_GA4_TOKEN_PATH', str(tmp_path / 'token.json'))
    result = server.health_check()
    assert result['ok'] is False
    assert result['status'] == 'oauth_required'
    assert 'secret' not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('status,code', [(400, 'google_request_failed'), (401, 'oauth_required'),
    (403, 'permission_denied'), (429, 'quota_exceeded'), (503, 'service_unavailable')])
def test_google_errors_are_sanitized(monkeypatch, status, code):
    def fail(**kwargs):
        raise HttpError(httplib2.Response({'status': str(status)}), b'{"error":{"message":"private-secret"}}', uri='https://example.com/?key=private-secret')
    monkeypatch.setattr(server.analytics, 'list_properties', fail)
    with pytest.raises(ToolError, match=code) as caught:
        server.list_properties()
    assert 'private-secret' not in str(caught.value)


def test_wrapper_forwards_all_report_options(monkeypatch):
    captured = {}
    def report(**kwargs):
        captured.update(kwargs)
        return {'rows': []}
    monkeypatch.setattr(server.analytics, 'run_report', report)
    result = server.run_report('7daysAgo', 'yesterday', ['sessions'], property_id='2', account='work', offset=10)
    assert result == {'ok': True, 'rows': []}
    assert captured['account'] == 'work'
    assert captured['property_id'] == '2'
    assert captured['offset'] == 10


def test_actual_stdio_protocol_and_tool_schemas(tmp_path):
    async def check():
        env = dict(os.environ)
        for key in list(env):
            if key.startswith('GOOGLE_'):
                env.pop(key)
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'flin_ga4_mcp.server'], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} == TOOLS
                for tool in listing.tools:
                    assert tool.annotations.readOnlyHint is True
                    assert tool.annotations.destructiveHint is False
                    assert tool.annotations.openWorldHint is (tool.name != 'health_check')
                report = next(t for t in listing.tools if t.name == 'run_report')
                assert set(report.inputSchema['required']) == {'start_date', 'end_date', 'metrics'}
                result = await session.call_tool('health_check', {})
                assert not result.isError
                assert result.structuredContent['status'] == 'missing_configuration'
                invalid = await session.call_tool('run_report', {'start_date': 'today', 'end_date': 'yesterday', 'metrics': ['sessions']})
                assert invalid.isError
                assert 'invalid_arguments' in invalid.content[0].text
    asyncio.run(check())
