from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from flin_ga4_mcp import analytics as ga
from flin_ga4_mcp.config import load_settings


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-secret')
    monkeypatch.setenv('GOOGLE_GA4_PROPERTY_ID', '123456789')
    monkeypatch.setenv('GOOGLE_GA4_TOKEN_PATH', str(tmp_path / 'token.json'))
    monkeypatch.setattr(ga, 'get_credentials', lambda **kw: pytest.fail('Unexpected OAuth'))


@pytest.fixture
def wire(monkeypatch):
    """Use Google's real discovery documents to build each request without network."""
    calls = []
    response = {}

    class Http:
        def request(self, uri, method='GET', body=None, headers=None, **kwargs):
            import httplib2
            calls.append({'uri': uri, 'method': method, 'body': json.loads(body) if body else None})
            return httplib2.Response({'status': '200'}), json.dumps(response).encode()
        def close(self):
            pass

    def service(api, settings, account):
        calls.append({'api': api, 'account': account})
        return build(api, 'v1beta', http=Http(), static_discovery=True)

    monkeypatch.setattr(ga, '_service', service)
    return calls, response


@pytest.mark.parametrize('value', ['123', ' properties/123 '])
def test_property_formats(value):
    assert ga.property_name(value, load_settings()) == 'properties/123'


@pytest.mark.parametrize('value', ['', '0', 'G-ABCDEF', 'accounts/123', '../123', 'properties/1/extra', '1?key=secret'])
def test_invalid_property_before_network(value, wire):
    with pytest.raises(ValueError):
        ga.get_property(property_id=value)
    assert wire[0] == []


def test_report_preserves_filters_account_pagination_and_quality(wire):
    calls, response = wire
    response.update({
        'dimensionHeaders': [{'name': 'country'}],
        'metricHeaders': [{'name': 'activeUsers', 'type': 'TYPE_INTEGER'}],
        'rows': [{'dimensionValues': [{'value': 'Switzerland'}], 'metricValues': [{'value': '9007199254740993'}]}],
        'rowCount': 10,
        'metadata': {'subjectToThresholding': True, 'timeZone': 'Europe/Zurich', 'currencyCode': 'CHF'},
        'propertyQuota': {'tokensPerDay': {'remaining': 123}},
    })
    filt = {'filter': {'fieldName': 'country', 'stringFilter': {'matchType': 'EXACT', 'value': 'Switzerland'}}}
    result = ga.run_report(start_date='28daysAgo', end_date='yesterday', metrics=['activeUsers'],
                           dimensions=['country'], dimension_filter=filt, account='work', limit=1, offset=2)
    assert calls[0] == {'api': 'analyticsdata', 'account': 'work'}
    assert '/v1beta/properties/123456789:runReport' in calls[1]['uri']
    assert calls[1]['method'] == 'POST'
    assert calls[1]['body']['dimensionFilter'] == filt
    assert calls[1]['body']['dateRanges'] == [{'startDate': '28daysAgo', 'endDate': 'yesterday'}]
    assert calls[1]['body']['offset'] == '2'
    assert calls[1]['body']['returnPropertyQuota'] is True
    assert result['rows'][0]['metrics']['activeUsers'] == '9007199254740993'
    assert result['metadata'] == response['metadata']
    assert result['propertyQuota'] == response['propertyQuota']
    assert result['nextOffset'] == 3


def test_report_empty_and_last_page(wire):
    result = ga.run_report(start_date='2026-01-01', end_date='2026-01-01', metrics=['sessions'])
    assert result['rows'] == []
    assert result['rowCount'] == 0
    assert result['nextOffset'] is None
    wire[1].update({'rowCount': 1, 'rows': [{}]})
    assert ga.run_report(start_date='yesterday', end_date='today', metrics=['sessions'])['nextOffset'] is None


@pytest.mark.parametrize('extra', [
    {'start_date': '2026-02-30'}, {'start_date': '20260101'},
    {'start_date': 'today', 'end_date': 'yesterday'},
    {'start_date': '2026-02-01', 'end_date': '2026-01-01'},
    {'metrics': []}, {'metrics': ['sessions', 'sessions']}, {'metrics': ['invalid name']},
    {'dimensions': [f'dimension{i}' for i in range(10)]},
    {'metrics': [f'metric{i}' for i in range(11)]},
    {'limit': 0}, {'limit': 10001}, {'limit': True}, {'offset': -1},
    {'currency_code': 'chf'}, {'currency_code': 'INVALID'},
])
def test_invalid_arguments_never_authenticate(extra, wire):
    args = {'start_date': '2026-01-01', 'end_date': '2026-01-31', 'metrics': ['sessions'], **extra}
    with pytest.raises(ValueError):
        ga.run_report(**args)
    assert wire[0] == []


def test_list_properties_with_next_page(wire):
    calls, response = wire
    response.update({'accountSummaries': [{'account': 'accounts/1', 'propertySummaries': [{'property': 'properties/2'}]}], 'nextPageToken': 'next'})
    result = ga.list_properties(account='work', page_size=20, page_token='previous')
    assert result['nextPageToken'] == 'next'
    assert result['accountSummaries'][0]['propertySummaries'][0]['property'] == 'properties/2'
    assert calls[0]['api'] == 'analyticsadmin'
    assert calls[1]['method'] == 'GET'
    assert 'pageToken=previous' in calls[1]['uri']
    assert 'pageSize=20' in calls[1]['uri']


@pytest.mark.parametrize('function,kwargs,path', [
    (ga.get_property, {}, '/v1beta/properties/123456789'),
    (ga.get_metadata, {}, '/v1beta/properties/123456789/metadata'),
    (ga.check_compatibility, {'metrics': ['sessions'], 'dimensions': ['country']}, ':checkCompatibility'),
])
def test_discovery_operations(function, kwargs, path, wire):
    function(**kwargs)
    assert path in wire[0][1]['uri']


def test_realtime_has_no_core_dates_or_offset_and_reports_truncation(wire):
    wire[1].update({'rowCount': 2, 'rows': [{}]})
    result = ga.run_realtime_report(dimensions=['country'], limit=1)
    request = wire[0][1]
    assert ':runRealtimeReport' in request['uri']
    assert request['body']['metrics'] == [{'name': 'activeUsers'}]
    assert request['body']['minuteRanges'] == [{'startMinutesAgo': 29, 'endMinutesAgo': 0}]
    assert 'offset' not in request['body'] and 'dateRanges' not in request['body']
    assert result['truncated'] is True


@pytest.mark.parametrize('kwargs', [{'minutes_ago': 60}, {'minutes_ago': -1}, {'metrics': []}, {'limit': 0}])
def test_realtime_invalid(kwargs, wire):
    with pytest.raises(ValueError):
        ga.run_realtime_report(**kwargs)
    assert wire[0] == []


@pytest.mark.parametrize('function,metric', [
    (ga.get_summary, 'activeUsers'), (ga.get_traffic_sources, 'sessions'),
    (ga.get_top_pages, 'screenPageViews'), (ga.get_events, 'eventCount'),
])
def test_presets_use_real_reporting_endpoint(function, metric, wire):
    function(start_date='7daysAgo', end_date='yesterday', account='work')
    body = wire[0][1]['body']
    assert {'name': metric} in body['metrics']
    if function is ga.get_summary:
        assert body['dimensions'] == []  # Aggregate users/rates, never sum page rows.
        assert body['limit'] == '1'
    else:
        assert body['orderBys'][0]['metric']['metricName'] == metric


def test_api_resources_close_on_failure(monkeypatch):
    service = Mock()
    service.properties.return_value.get.return_value.execute.side_effect = TimeoutError()
    monkeypatch.setattr(ga, '_service', lambda *a: service)
    with pytest.raises(TimeoutError):
        ga.get_property()
    service.close.assert_called_once()


def test_totals_preserve_numeric_strings():
    report = ga.normalize_report({'metricHeaders': [{'name': 'totalRevenue', 'type': 'TYPE_CURRENCY'}],
                                  'totals': [{'metricValues': [{'value': '1.2300001'}]}]})
    assert report['totals'][0]['metrics']['totalRevenue'] == '1.2300001'


def test_pagination_adds_stable_tie_breakers_without_replacing_user_order(wire):
    ordering = [{'metric': {'metricName': 'sessions'}, 'desc': True},
                {'dimension': {'dimensionName': 'country'}, 'desc': True}]
    ga.run_report(start_date='7daysAgo', end_date='yesterday', metrics=['sessions'],
                  dimensions=['country', 'city'], order_bys=ordering)
    actual = wire[0][1]['body']['orderBys']
    assert actual == ordering + [{'dimension': {'dimensionName': 'city'}}]
    assert len(ordering) == 2


def test_default_pagination_orders_by_all_dimensions(wire):
    ga.run_report(start_date='7daysAgo', end_date='yesterday', metrics=['sessions'],
                  dimensions=['date', 'country'])
    assert wire[0][1]['body']['orderBys'] == [
        {'dimension': {'dimensionName': 'date'}}, {'dimension': {'dimensionName': 'country'}},
    ]
