"""GA4 read operations, separate from MCP transport and local authentication."""
from __future__ import annotations

from datetime import date
import re
from typing import Any

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

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


def _service(api: str, settings: Settings, account: str | None) -> Any:
    credentials = get_credentials(settings=settings, account=account)
    return build(
        api, "v1beta", cache_discovery=False,
        http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
    )


def _execute(api: str, settings: Settings, account: str | None, operation: Any) -> dict[str, Any]:
    service = _service(api, settings, account)
    try:
        return operation(service).execute(num_retries=2)
    finally:
        service.close()


def _context(prop: str, account: str | None) -> dict[str, Any]:
    return {"property": prop, **({"account": account.strip()} if account is not None else {})}


def normalize_report(response: dict[str, Any]) -> dict[str, Any]:
    """Name positional values; preserve numeric strings, units, and quality metadata."""
    dimensions = [item["name"] for item in response.get("dimensionHeaders", [])]
    metrics = [item["name"] for item in response.get("metricHeaders", [])]

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


def run_report(*, start_date: str, end_date: str, metrics: list[str],
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
    if currency_code is not None:
        if not re.fullmatch(r"[A-Z]{3}", currency_code):
            raise ValueError("currency_code must be a three-letter uppercase currency code.")
        body["currencyCode"] = currency_code
    settings = load_settings()
    prop = property_name(property_id, settings)
    response = _execute("analyticsdata", settings, account, lambda s: s.properties().runReport(property=prop, body=body))
    result = normalize_report(response)
    next_offset = offset + len(result["rows"])
    result.update({
        **_context(prop, account), "dateRanges": body["dateRanges"], "limit": limit, "offset": offset,
        "nextOffset": next_offset if result["rows"] and next_offset < result["rowCount"] else None,
    })
    return result


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
