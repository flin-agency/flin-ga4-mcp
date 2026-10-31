"""GA4 read operations, separate from MCP transport and local authentication."""
from __future__ import annotations

from datetime import date, datetime
import re
import json
from typing import Any

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from googleapiclient.model import JsonModel

from .auth import get_credentials
from .config import Settings, load_settings


MAX_ROWS = 10000  # Keep individual tool responses bounded; use offset for more.
SUMMARY_METRICS = [
    "activeUsers", "newUsers", "sessions", "engagedSessions", "engagementRate",
    "screenPageViews", "eventCount", "keyEvents", "totalRevenue",
]


def property_name(value: str | None, settings: Settings) -> str:
    candidate = value if value is not None else settings.default_property_id
    candidate = (candidate or "").strip()
    if not re.fullmatch(r"(?:properties/)?[1-9][0-9]*", candidate):
        raise ValueError("Use a numeric GA4 property ID, not a measurement ID.")
    return candidate if candidate.startswith("properties/") else f"properties/{candidate}"


def _integer(value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Pagination or time range is outside the supported bounds.")
    return value


def _names(values: list[str] | None, maximum: int, *, required: bool = False) -> list[dict[str, str]]:
    names = [value.strip() for value in (values or [])]
    if (required and not names) or len(names) > maximum or len(set(names)) != len(names):
        raise ValueError("Missing, duplicate, or too many dimensions/metrics.")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_:\.]*", name) for name in names):
        raise ValueError("Invalid dimension or metric API name.")
    return [{"name": name} for name in names]


def _date_range(start: str, end: str) -> dict[str, str]:
    def parse(value: str) -> tuple[str, int | None]:
        value = value.strip()
        # Google resolves relative dates in the property's reporting timezone.
        if value in {"today", "yesterday"}:
            return value, 0 if value == "today" else 1
        if re.fullmatch(r"[0-9]+daysAgo", value):
            return value, int(value[:-7])
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Use YYYY-MM-DD, today, yesterday, or NdaysAgo.")
        date.fromisoformat(value)
        return value, None

    start, start_ago = parse(start)
    end, end_ago = parse(end)
    if start_ago is None and end_ago is None and start > end:
        raise ValueError("Start date must not be after end date.")
    if start_ago is not None and end_ago is not None and start_ago < end_ago:
        raise ValueError("Start date must not be after end date.")
    return {"startDate": start, "endDate": end}


def _http(settings: Settings, account: str | None) -> Any:
    credentials = get_credentials(settings=settings, account=account)
    return AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))


def _service(api: str, settings: Settings, account: str | None, version: str = "v1beta") -> Any:
    return build(api, version, cache_discovery=False, http=_http(settings, account))


def _execute(api: str, settings: Settings, account: str | None, operation: Any,
             version: str = "v1beta") -> dict[str, Any]:
    service = _service(api, settings, account, version)
    try:
        return operation(service).execute(num_retries=2)
    finally:
        service.close()


def _context(prop: str, account: str | None) -> dict[str, Any]:
    return {"property": prop, **({"account": account.strip()} if account is not None else {})}


def normalize_report(response: dict[str, Any]) -> dict[str, Any]:
    """Name positional values; preserve numeric strings, units, and quality metadata."""
    dimensions = [item.get("name", item.get("dimensionName")) for item in response.get("dimensionHeaders", [])]
    metrics = [item.get("name", item.get("metricName")) for item in response.get("metricHeaders", [])]

    def rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "dimensions": dict(zip(dimensions, [v.get("value", "") for v in row.get("dimensionValues", [])])),
            "metrics": dict(zip(metrics, [v.get("value", "") for v in row.get("metricValues", [])])),
        } for row in items]

    result = {key: value for key, value in response.items() if key not in {"rows", "totals", "maximums", "minimums"}}
    result["rows"] = rows(response.get("rows", []))
    result["rowCount"] = response.get("rowCount", 0)
    for key in ("totals", "maximums", "minimums"):
        if key in response:
            result[key] = rows(response[key])
    return result


def list_properties(*, account: str | None = None, page_size: int = 50, page_token: str | None = None) -> dict[str, Any]:
    _integer(page_size, 1, 200)
    settings = load_settings()
    params: dict[str, Any] = {"pageSize": page_size}
    if page_token:
        params["pageToken"] = page_token
    response = _execute("analyticsadmin", settings, account, lambda s: s.accountSummaries().list(**params))
    return {
        **({"account": account.strip()} if account is not None else {}),
        "accountSummaries": response.get("accountSummaries", []),
        "nextPageToken": response.get("nextPageToken"),
    }


def get_property(*, property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsadmin", settings, account, lambda s: s.properties().get(name=prop))
    return {**_context(prop, account), "details": response}


def get_metadata(*, property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account, lambda s: s.properties().getMetadata(name=f"{prop}/metadata"))
    return {**_context(prop, account), **response}


def check_compatibility(*, metrics: list[str], dimensions: list[str] | None = None,
                        property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    body = {"metrics": _names(metrics, 10, required=True), "dimensions": _names(dimensions, 9)}
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account, lambda s: s.properties().checkCompatibility(property=prop, body=body))
    return {**_context(prop, account), **response}


def _report_body(*, start_date: str, end_date: str, metrics: list[str],
               dimensions: list[str] | None = None, property_id: str | None = None,
               dimension_filter: dict[str, Any] | None = None, metric_filter: dict[str, Any] | None = None,
               order_bys: list[dict[str, Any]] | None = None, limit: int = 1000, offset: int = 0,
               currency_code: str | None = None, keep_empty_rows: bool = False,
               account: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "dateRanges": [_date_range(start_date, end_date)],
        "metrics": _names(metrics, 10, required=True), "dimensions": _names(dimensions, 9),
        "limit": str(_integer(limit, 1, MAX_ROWS)), "offset": str(_integer(offset, 0, 2**63 - 1)),
        "keepEmptyRows": keep_empty_rows, "returnPropertyQuota": True,
    }
    for key, value in (("dimensionFilter", dimension_filter), ("metricFilter", metric_filter), ("orderBys", order_bys)):
        if value is not None:
            body[key] = value
    # Break metric ties by the complete dimension key so adjacent pages are stable.
    # An explicit dimension ordering (including pivot ordering) always takes priority.
    ordering = list(body.get("orderBys", []))
    ordered_dimensions = {
        item["dimension"]["dimensionName"] for item in ordering
        if isinstance(item.get("dimension"), dict) and "dimensionName" in item["dimension"]
    }
    if not any("pivot" in item for item in ordering):
        ordering.extend(
            {"dimension": {"dimensionName": dimension["name"]}}
            for dimension in body["dimensions"] if dimension["name"] not in ordered_dimensions
        )
    if ordering:
        body["orderBys"] = ordering
    if currency_code is not None:
        if not re.fullmatch(r"[A-Z]{3}", currency_code):
            raise ValueError("currency_code must be a three-letter uppercase currency code.")
        body["currencyCode"] = currency_code
    return body


def _report_result(response: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    result = normalize_report(response)
    offset = int(body["offset"])
    next_offset = offset + len(result["rows"])
    result.update({
        "dateRanges": body["dateRanges"], "limit": int(body["limit"]), "offset": offset,
        "nextOffset": next_offset if result["rows"] and next_offset < int(result["rowCount"]) else None,
    })
    return result


def run_report(*, start_date: str, end_date: str, metrics: list[str],
               dimensions: list[str] | None = None, property_id: str | None = None,
               dimension_filter: dict[str, Any] | None = None, metric_filter: dict[str, Any] | None = None,
               order_bys: list[dict[str, Any]] | None = None, limit: int = 1000, offset: int = 0,
               currency_code: str | None = None, keep_empty_rows: bool = False,
               account: str | None = None) -> dict[str, Any]:
    body = _report_body(**locals())
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account, lambda s: s.properties().runReport(property=prop, body=body))
    return {**_context(prop, account), **_report_result(response, body)}


def batch_run_reports(*, reports: list[dict[str, Any]], property_id: str | None = None,
                      account: str | None = None) -> dict[str, Any]:
    if not 1 <= len(reports) <= 5:
        raise ValueError("Supply between one and five reports.")
    allowed = {"start_date", "end_date", "metrics", "dimensions", "dimension_filter",
               "metric_filter", "order_bys", "limit", "offset", "currency_code", "keep_empty_rows"}
    bodies = []
    for report in reports:
        if not isinstance(report, dict) or set(report) - allowed:
            raise ValueError("Batch reports use run_report arguments without property_id/account.")
        if not {"start_date", "end_date", "metrics"} <= set(report):
            raise ValueError("Each report needs dates and metrics.")
        bodies.append(_report_body(**report))
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account,
                        lambda s: s.properties().batchRunReports(property=prop, body={"requests": bodies}))
    results = response.get("reports", [])
    if len(results) != len(bodies):
        raise RuntimeError("Unexpected batch response count.")
    return {**_context(prop, account), "reports": [
        {"reportIndex": i, **_report_result(result, body)}
        for i, (result, body) in enumerate(zip(results, bodies))
    ]}


def run_realtime_report(*, metrics: list[str] | None = None, dimensions: list[str] | None = None,
                        property_id: str | None = None, dimension_filter: dict[str, Any] | None = None,
                        metric_filter: dict[str, Any] | None = None,
                        order_bys: list[dict[str, Any]] | None = None, limit: int = 1000,
                        minutes_ago: int = 29, account: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "metrics": _names(["activeUsers"] if metrics is None else metrics, 10, required=True),
        "dimensions": _names(dimensions, 9), "limit": str(_integer(limit, 1, MAX_ROWS)),
        "minuteRanges": [{"startMinutesAgo": _integer(minutes_ago, 0, 59), "endMinutesAgo": 0}],
        "returnPropertyQuota": True,
    }
    for key, value in (("dimensionFilter", dimension_filter), ("metricFilter", metric_filter), ("orderBys", order_bys)):
        if value is not None:
            body[key] = value
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account, lambda s: s.properties().runRealtimeReport(property=prop, body=body))
    result = normalize_report(response)
    return {**_context(prop, account), **result, "minuteRanges": body["minuteRanges"],
            "truncated": result["rowCount"] > len(result["rows"])}


def get_summary(**kwargs: Any) -> dict[str, Any]:
    return run_report(metrics=SUMMARY_METRICS, limit=1, **kwargs)


def get_traffic_sources(**kwargs: Any) -> dict[str, Any]:
    return run_report(
        dimensions=["sessionDefaultChannelGroup", "sessionSourceMedium"],
        metrics=["sessions", "engagedSessions", "engagementRate", "keyEvents", "totalRevenue"],
        order_bys=[{"metric": {"metricName": "sessions"}, "desc": True}], **kwargs,
    )


def get_top_pages(**kwargs: Any) -> dict[str, Any]:
    return run_report(
        dimensions=["hostName", "pagePath"], metrics=["screenPageViews", "activeUsers", "userEngagementDuration"],
        order_bys=[{"metric": {"metricName": "screenPageViews"}, "desc": True}], **kwargs,
    )


def get_events(**kwargs: Any) -> dict[str, Any]:
    return run_report(
        dimensions=["eventName"], metrics=["eventCount", "totalUsers", "keyEvents"],
        order_bys=[{"metric": {"metricName": "eventCount"}, "desc": True}], **kwargs,
    )


# Fixed read-only resources: callers cannot select arbitrary API methods.
CONFIGURATION_SECTIONS = {
    "data_streams": ("v1beta", "dataStreams"),
    "key_events": ("v1beta", "keyEvents"),
    "custom_dimensions": ("v1beta", "customDimensions"),
    "custom_metrics": ("v1beta", "customMetrics"),
    "calculated_metrics": ("v1alpha", "calculatedMetrics"),
    "attribution": ("v1alpha", "attributionSettings"),
    "data_retention": ("v1beta", "dataRetentionSettings"),
}
_SINGLETON_METHODS = {
    "attribution": "getAttributionSettings", "data_retention": "getDataRetentionSettings",
}


def get_property_configuration(*, sections: list[str], property_id: str | None = None,
                               page_size: int = 50, page_tokens: dict[str, str] | None = None,
                               account: str | None = None) -> dict[str, Any]:
    if not sections or len(set(sections)) != len(sections) or set(sections) - CONFIGURATION_SECTIONS.keys():
        raise ValueError("Choose unique supported configuration sections.")
    _integer(page_size, 1, 200)
    page_tokens = page_tokens or {}
    if set(page_tokens) - (set(sections) - _SINGLETON_METHODS.keys()):
        raise ValueError("Page tokens must belong to requested list sections.")
    settings = load_settings()
    prop = property_name(property_id, settings)
    results = {}
    for section in sections:
        version, resource = CONFIGURATION_SECTIONS[section]
        try:
            if section in _SINGLETON_METHODS:
                response = _execute("analyticsadmin", settings, account,
                    lambda s: getattr(s.properties(), _SINGLETON_METHODS[section])(name=f"{prop}/{resource}"), version)
            else:
                params = {"parent": prop, "pageSize": page_size}
                if page_tokens.get(section):
                    params["pageToken"] = page_tokens[section]
                response = _execute("analyticsadmin", settings, account,
                    lambda s: getattr(s.properties(), resource)().list(**params), version)
                response = {**response, resource: response.get(resource, []),
                            "nextPageToken": response.get("nextPageToken")}
            results[section] = {"status": "ok", "data": response}
        except HttpError as exc:
            if exc.resp.status not in {403, 404}:
                raise
            # Never echo upstream error bodies, which can contain private details.
            results[section] = {
                "status": "permission_denied" if exc.resp.status == 403 else "unavailable",
                "message": "Check property access, API enablement and availability for this section.",
            }
    return {**_context(prop, account), "sections": results,
            "partial": any(value["status"] != "ok" for value in results.values())}


def list_property_annotations(*, property_id: str | None = None, filter: str | None = None,
                              page_size: int = 50, page_token: str | None = None,
                              account: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"pageSize": _integer(page_size, 1, 200)}
    if filter is not None:
        params["filter"] = filter
    if page_token:
        params["pageToken"] = page_token
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsadmin", settings, account,
        lambda s: s.properties().reportingDataAnnotations().list(parent=prop, **params), "v1alpha")
    return {**_context(prop, account), **response,
            "reportingDataAnnotations": response.get("reportingDataAnnotations", []),
            "nextPageToken": response.get("nextPageToken")}


def _ga_account_name(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"(?:accounts/)?[1-9][0-9]*", value):
        raise ValueError("Use a numeric GA account ID, not a local login alias.")
    return value if value.startswith("accounts/") else f"accounts/{value}"


def _timestamp(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Use an RFC3339 timestamp including timezone.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _redact_history(value: Any) -> Any:
    # ChangeHistoryChange.resourceBefore/After may contain measurement protocol secrets.
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k == "secretValue" else _redact_history(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_history(v) for v in value]
    return value


def search_change_history(*, ga_account_id: str, property_id: str | None = None,
                          earliest_change_time: str | None = None, latest_change_time: str | None = None,
                          actions: list[str] | None = None, resource_types: list[str] | None = None,
                          actor_emails: list[str] | None = None, page_size: int = 50,
                          page_token: str | None = None, account: str | None = None) -> dict[str, Any]:
    ga_account = _ga_account_name(ga_account_id)
    body: dict[str, Any] = {"pageSize": _integer(page_size, 1, 200)}
    earliest = _timestamp(earliest_change_time) if earliest_change_time is not None else None
    latest = _timestamp(latest_change_time) if latest_change_time is not None else None
    if earliest and latest and earliest > latest:
        raise ValueError("Earliest change time must not follow latest change time.")
    if actions is not None and (not actions or set(actions) - {"CREATED", "UPDATED", "DELETED"}):
        raise ValueError("Unsupported change action.")
    for key, value in (("earliestChangeTime", earliest_change_time), ("latestChangeTime", latest_change_time),
                       ("action", actions), ("resourceType", resource_types), ("actorEmail", actor_emails),
                       ("pageToken", page_token)):
        if value is not None:
            body[key] = value
    settings = load_settings()
    if property_id is not None:
        body["property"] = property_name(property_id, settings)
    response = _execute("analyticsadmin", settings, account,
        lambda s: s.accounts().searchChangeHistoryEvents(account=ga_account, body=body))
    return {"gaAccount": ga_account, **({"account": account.strip()} if account is not None else {}),
            **({"property": body["property"]} if "property" in body else {}),
            **_redact_history(response), "nextPageToken": response.get("nextPageToken"),
            "changeHistoryEvents": _redact_history(response.get("changeHistoryEvents", []))}


def run_access_report(*, start_date: str, end_date: str, dimensions: list[str], metrics: list[str],
                      property_id: str | None = None, ga_account_id: str | None = None,
                      dimension_filter: dict[str, Any] | None = None,
                      metric_filter: dict[str, Any] | None = None,
                      order_bys: list[dict[str, Any]] | None = None, limit: int = 1000, offset: int = 0,
                      account: str | None = None) -> dict[str, Any]:
    if property_id is not None and ga_account_id is not None:
        raise ValueError("Choose a property or a GA account, not both.")
    body: dict[str, Any] = {
        "dateRanges": [_date_range(start_date, end_date)],
        "dimensions": [{"dimensionName": d["name"]} for d in _names(dimensions, 9)],
        "metrics": [{"metricName": m["name"]} for m in _names(metrics, 10, required=True)],
        "limit": str(_integer(limit, 1, MAX_ROWS)), "offset": str(_integer(offset, 0, 2**63 - 1)),
        "returnEntityQuota": ga_account_id is None,
    }
    for key, value in (("dimensionFilter", dimension_filter), ("metricFilter", metric_filter)):
        if value is not None:
            body[key] = value
    # Use all dimensions as tie-breakers for stable access-report pagination.
    ordering = list(order_bys or [])
    ordered = {item.get("dimension", {}).get("dimensionName") for item in ordering}
    ordering.extend({"dimension": item} for item in body["dimensions"] if item["dimensionName"] not in ordered)
    if ordering:
        body["orderBys"] = ordering
    settings = load_settings()
    entity = _ga_account_name(ga_account_id) if ga_account_id is not None else property_name(property_id, settings)
    response = _execute("analyticsadmin", settings, account,
        lambda s: (s.accounts() if ga_account_id is not None else s.properties()).runAccessReport(entity=entity, body=body))
    return {"entity": entity, **({"account": account.strip()} if account is not None else {}),
            **_report_result(response, body)}


def run_funnel_report(*, start_date: str, end_date: str, steps: list[dict[str, Any]],
                      property_id: str | None = None, is_open_funnel: bool = False,
                      funnel_breakdown: dict[str, Any] | None = None,
                      funnel_next_action: dict[str, Any] | None = None,
                      segments: list[dict[str, Any]] | None = None,
                      dimension_filter: dict[str, Any] | None = None,
                      visualization_type: str = "STANDARD_FUNNEL", limit: int = 1000,
                      account: str | None = None) -> dict[str, Any]:
    if not 1 <= len(steps) <= 10 or any(not isinstance(step, dict) or not step.get("filterExpression") for step in steps):
        raise ValueError("Supply 1–10 funnel steps with filterExpression.")
    if segments is not None and not 1 <= len(segments) <= 4:
        raise ValueError("Supply 1–4 segments, or omit them.")
    if visualization_type not in {"STANDARD_FUNNEL", "TRENDED_FUNNEL"}:
        raise ValueError("Unsupported funnel visualization type.")
    body: dict[str, Any] = {
        "dateRanges": [_date_range(start_date, end_date)],
        "funnel": {"isOpenFunnel": is_open_funnel, "steps": steps},
        "funnelVisualizationType": visualization_type,
        "limit": str(_integer(limit, 1, MAX_ROWS)), "returnPropertyQuota": True,
    }
    for key, value in (("funnelBreakdown", funnel_breakdown), ("funnelNextAction", funnel_next_action),
                       ("segments", segments), ("dimensionFilter", dimension_filter)):
        if value is not None:
            body[key] = value
    settings = load_settings()
    prop = property_name(property_id, settings)
    # Bundled analyticsdata v1alpha discovery is from 2021 and lacks runFunnelReport.
    # Call only this fixed documented read endpoint; retain auth, timeout and retries.
    http = _http(settings, account)
    try:
        response = HttpRequest(http, JsonModel().response,
            uri=f"https://analyticsdata.googleapis.com/v1alpha/{prop}:runFunnelReport",
            method="POST", body=json.dumps(body), headers={"content-type": "application/json"}).execute(num_retries=2)
    finally:
        http.close()
    result = {**_context(prop, account), **response, "dateRanges": body["dateRanges"], "limit": limit}
    for key in ("funnelTable", "funnelVisualization"):
        if key in response:
            report = normalize_report(response[key])
            # FunnelSubReport has no rowCount: completeness is unknown at the cap.
            report.pop("rowCount", None)
            report["possiblyTruncated"] = len(report["rows"]) >= limit
            result[key] = report
    return result
