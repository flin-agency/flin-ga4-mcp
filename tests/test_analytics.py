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

    def service(api, settings, account, version="v1beta"):
        calls.append({'api': api, 'account': account})
        return build(api, version, http=Http(), static_discovery=True)

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


@pytest.mark.parametrize('section,version,path', [
    ('data_streams', 'v1beta', 'dataStreams'), ('key_events', 'v1beta', 'keyEvents'),
    ('custom_dimensions', 'v1beta', 'customDimensions'), ('custom_metrics', 'v1beta', 'customMetrics'),
    ('calculated_metrics', 'v1alpha', 'calculatedMetrics'),
    ('attribution', 'v1alpha', 'attributionSettings'), ('data_retention', 'v1beta', 'dataRetentionSettings'),
])
def test_configuration_uses_read_endpoint(section, version, path, wire):
    singleton = section in {'attribution', 'data_retention'}
    result = ga.get_property_configuration(sections=[section], page_size=20,
        page_tokens=None if singleton else {section: 'page2'}, account='work')
    req = wire[0][1]
    assert req['method'] == 'GET'
    assert f'/{version}/properties/123456789/{path}' in req['uri']
    assert ('pageToken=page2' in req['uri']) is not singleton
    assert result['sections'][section]['status'] == 'ok'
    assert result['partial'] is False


def test_configuration_partial_permission_error_and_page_token(monkeypatch, wire):
    from googleapiclient.errors import HttpError
    import httplib2
    original = ga._execute
    def execute(api, settings, account, operation, version='v1beta'):
        if version == 'v1alpha':
            raise HttpError(httplib2.Response({'status': '403'}), b'private-secret')
        return original(api, settings, account, operation, version)
    monkeypatch.setattr(ga, '_execute', execute)
    wire[1].update({'keyEvents': [{'eventName': 'purchase'}], 'nextPageToken': 'next'})
    result = ga.get_property_configuration(sections=['attribution', 'key_events'])
    assert result['partial'] is True
    assert result['sections']['attribution']['status'] == 'permission_denied'
    assert 'data' not in result['sections']['attribution']
    assert result['sections']['key_events']['data']['nextPageToken'] == 'next'
    assert 'private-secret' not in json.dumps(result)


def test_annotations_alpha_filter_and_empty_page(wire):
    wire[1]['nextPageToken'] = 'next'
    result = ga.list_property_annotations(filter='title="Sale"', page_token='previous', account='work')
    assert '/v1alpha/properties/123456789/reportingDataAnnotations' in wire[0][1]['uri']
    assert 'filter=' in wire[0][1]['uri']
    assert 'pageToken=previous' in wire[0][1]['uri']
    assert result['reportingDataAnnotations'] == [] and result['nextPageToken'] == 'next'


def test_batch_preserves_order_metadata_and_independent_offsets(wire):
    wire[1]['reports'] = [
        {'metricHeaders': [{'name': 'sessions'}], 'rows': [{'metricValues': [{'value': '9007199254740993'}]}],
         'rowCount': 10, 'metadata': {'subjectToThresholding': True}, 'propertyQuota': {'tokensPerDay': {'remaining': 9}}},
        {'rowCount': 0},
    ]
    result = ga.batch_run_reports(reports=[
        {'start_date': '7daysAgo', 'end_date': 'yesterday', 'metrics': ['sessions'], 'limit': 1, 'offset': 2},
        {'start_date': '14daysAgo', 'end_date': '8daysAgo', 'metrics': ['sessions']},
    ], account='work')
    assert len(wire[0]) == 2  # one service, one HTTP call
    assert ':batchRunReports' in wire[0][1]['uri']
    assert all(b['returnPropertyQuota'] for b in wire[0][1]['body']['requests'])
    assert [r['reportIndex'] for r in result['reports']] == [0, 1]
    assert [r['nextOffset'] for r in result['reports']] == [3, None]
    assert result['reports'][0]['rows'][0]['metrics']['sessions'] == '9007199254740993'
    assert result['reports'][0]['metadata']['subjectToThresholding'] is True
    assert result['reports'][0]['propertyQuota'] == wire[1]['reports'][0]['propertyQuota']


def test_batch_rejects_unexpected_response_count(wire):
    with pytest.raises(RuntimeError):
        ga.batch_run_reports(reports=[{'start_date': 'today', 'end_date': 'today', 'metrics': ['sessions']}])


def test_history_uses_ga_account_and_redacts_secrets(wire):
    wire[1].update({'changeHistoryEvents': [{'changes': [
        {'resourceBefore': {'measurementProtocolSecret': {'secretValue': 'private-secret'}},
         'resourceAfter': {'measurementProtocolSecret': {'secretValue': 'other-secret'}}}]}], 'nextPageToken': 'next'})
    result = ga.search_change_history(ga_account_id='42', property_id='123', account='work',
        earliest_change_time='2026-10-01T00:00:00Z', latest_change_time='2026-10-31T23:59:59+01:00',
        actions=['UPDATED'], resource_types=['DATA_STREAM'], actor_emails=['editor@example.org'], page_token='prev')
    req = wire[0][1]
    assert '/v1beta/accounts/42:searchChangeHistoryEvents' in req['uri']
    assert req['method'] == 'POST'
    assert req['body']['property'] == 'properties/123'
    assert req['body']['action'] == ['UPDATED'] and req['body']['pageToken'] == 'prev'
    assert result['gaAccount'] == 'accounts/42' and result['account'] == 'work'
    assert result['nextPageToken'] == 'next'
    assert 'private-secret' not in json.dumps(result) and 'other-secret' not in json.dumps(result)
    ga.search_change_history(ga_account_id='42')
    assert 'property' not in wire[0][-1]['body']  # no implicit default-property restriction


@pytest.mark.parametrize('ga_account_id', [None, 'accounts/42'])
def test_access_schema_and_entity_quota(ga_account_id, wire):
    wire[1].update({'dimensionHeaders': [{'dimensionName': 'userEmail'}],
        'metricHeaders': [{'metricName': 'accessCount'}],
        'rows': [{'dimensionValues': [{'value': 'reader@example.org'}], 'metricValues': [{'value': '12'}]}],
        'rowCount': 5, 'quota': {'tokensPerHour': {'remaining': 100}}})
    result = ga.run_access_report(start_date='7daysAgo', end_date='yesterday', dimensions=['userEmail'],
        metrics=['accessCount'], ga_account_id=ga_account_id, account='work', limit=1, offset=1)
    req = wire[0][1]
    entity = ga_account_id or 'properties/123456789'
    assert f'/v1beta/{entity}:runAccessReport' in req['uri']
    assert req['body']['returnEntityQuota'] is (ga_account_id is None)
    assert req['body']['dimensions'] == [{'dimensionName': 'userEmail'}]
    assert req['body']['metrics'] == [{'metricName': 'accessCount'}]
    assert req['body']['orderBys'] == [{'dimension': {'dimensionName': 'userEmail'}}]
    assert result['rows'] == [{'dimensions': {'userEmail': 'reader@example.org'}, 'metrics': {'accessCount': '12'}}]
    assert result['nextOffset'] == 2 and result['quota'] == wire[1]['quota']


FUNNEL_STEPS = [{'name': 'Purchase', 'filterExpression': {'funnelEventFilter': {'eventName': 'purchase'}}}]


def test_funnel_fixed_endpoint_nested_metadata_and_http_close(monkeypatch):
    import httplib2
    calls = []
    subreport = {'dimensionHeaders': [{'name': 'funnelStepName'}],
                 'metricHeaders': [{'name': 'activeUsers', 'type': 'TYPE_INTEGER'}],
                 'rows': [{'dimensionValues': [{'value': '1. Purchase'}], 'metricValues': [{'value': '42'}]}],
                 'metadata': {'samplingMetadatas': [{'samplesReadCount': '50', 'samplingSpaceSize': '100'}]}}
    response = {'funnelTable': subreport, 'funnelVisualization': subreport, 'propertyQuota': {'remaining': 99}}
    http = Mock()
    def request(uri, method, body, headers, **kw):
        calls.append((uri, method, json.loads(body)))
        return httplib2.Response({'status': '200'}), json.dumps(response).encode()
    http.request.side_effect = request
    monkeypatch.setattr(ga, '_http', lambda settings, account: http)
    result = ga.run_funnel_report(start_date='7daysAgo', end_date='yesterday', steps=FUNNEL_STEPS,
        is_open_funnel=True, funnel_breakdown={'breakdownDimension': {'name': 'deviceCategory'}}, limit=1)
    uri, method, body = calls[0]
    assert uri == 'https://analyticsdata.googleapis.com/v1alpha/properties/123456789:runFunnelReport'
    assert method == 'POST' and body['returnPropertyQuota'] is True
    assert body['funnel'] == {'steps': FUNNEL_STEPS, 'isOpenFunnel': True}
    assert result['funnelTable']['rows'][0]['metrics']['activeUsers'] == '42'
    for key in ('funnelTable', 'funnelVisualization'):
        assert result[key]['metadata'] == subreport['metadata']
        assert result[key]['possiblyTruncated'] is True
        assert 'rowCount' not in result[key]
    assert result['propertyQuota'] == response['propertyQuota']
    http.close.assert_called_once()


def test_funnel_closes_transport_on_failure(monkeypatch):
    http = Mock()
    http.request.side_effect = TimeoutError()
    monkeypatch.setattr(ga, '_http', lambda settings, account: http)
    with pytest.raises(TimeoutError):
        ga.run_funnel_report(start_date='today', end_date='today', steps=FUNNEL_STEPS)
    http.close.assert_called_once()


@pytest.mark.parametrize('function,kwargs', [
    (ga.get_property_configuration, {'sections': []}),
    (ga.get_property_configuration, {'sections': ['delete']}),
    (ga.get_property_configuration, {'sections': ['key_events', 'key_events']}),
    (ga.get_property_configuration, {'sections': ['attribution'], 'page_tokens': {'attribution': 'p'}}),
    (ga.batch_run_reports, {'reports': []}),
    (ga.batch_run_reports, {'reports': [{}] * 6}),
    (ga.batch_run_reports, {'reports': [{'property_id': '2'}]}),
    (ga.batch_run_reports, {'reports': [{'metrics': ['sessions']}]}),
    (ga.batch_run_reports, {'reports': [{'start_date': 'today', 'end_date': 'today', 'metrics': ['sessions'], 'limit': 10001}]}),
    (ga.search_change_history, {'ga_account_id': 'work'}),
    (ga.search_change_history, {'ga_account_id': '42', 'earliest_change_time': 'yesterday'}),
    (ga.search_change_history, {'ga_account_id': '42', 'earliest_change_time': '2026-10-31T00:00:00Z', 'latest_change_time': '2026-10-01T00:00:00Z'}),
    (ga.run_funnel_report, {'start_date': 'today', 'end_date': 'today', 'steps': []}),
    (ga.run_funnel_report, {'start_date': 'today', 'end_date': 'today', 'steps': FUNNEL_STEPS, 'segments': [{}] * 5}),
    (ga.run_funnel_report, {'start_date': 'today', 'end_date': 'today', 'steps': FUNNEL_STEPS, 'property_id': '1/../../accounts'}),
    (ga.run_access_report, {'start_date': 'today', 'end_date': 'today', 'metrics': ['accessCount'], 'dimensions': [], 'property_id': '1', 'ga_account_id': '2'}),
])
def test_extended_invalid_arguments_do_not_authenticate(function, kwargs, wire):
    with pytest.raises(ValueError):
        function(**kwargs)
    assert not wire[0]
