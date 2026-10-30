from __future__ import annotations

from functools import wraps
from typing import Any

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import analytics
from .auth import AuthenticationError, AuthenticationRequiredError, describe_auth_state
from .config import ConfigurationError, load_settings, missing_required_env_vars

mcp = FastMCP(
    name="flin-ga4-mcp",
    instructions=(
        "Read-only Google Analytics 4 server. Start with health_check and list_properties. "
        "Choose a numeric property ID; account selects a local Google-login alias, not a GA account ID. "
        "The first data call may open Google sign-in and save a local token. "
        "Use get_metadata/check_compatibility before custom reports. All report metric values are "
        "strings preserving precision; inspect metricHeaders for types and metadata for timezone, "
        "currency, thresholding, sampling and data loss. Follow nextPageToken/nextOffset for more rows. "
        "Do not sum distinct users or average rates across report rows."
    ),
)

def _tool_error(exc: Exception) -> ToolError:
    """Expose only fixed messages; upstream exceptions can contain credentials or URLs."""
    if isinstance(exc, ConfigurationError):
        message = (
            "invalid_configuration: Check the Google OAuth configuration and account name. "
            "Named accounts must not refer to the default token file; omit account to use it."
        )
    elif isinstance(exc, AuthenticationRequiredError):
        message = "oauth_required: Sign in to the selected Google account and try again."
    elif isinstance(exc, AuthenticationError):
        message = (
            "authentication_failed: Check the saved credentials and token storage. "
            "Use a directory owned by you that other users cannot write to."
        )
    elif isinstance(exc, HttpError):
        status = exc.resp.status
        if status == 401:
            message = "oauth_required: Google rejected the credentials. Sign in again."
        elif status == 403:
            message = (
                "permission_denied: Google denied the request. "
                "Check property access, API enablement, and quota."
            )
        elif status == 429:
            message = "quota_exceeded: Google limited the requests. Try again later."
        elif status >= 500:
            message = "service_unavailable: Google is temporarily unavailable. Try again later."
        else:
            message = "google_request_failed: Google rejected the request. Check the tool arguments."
    elif isinstance(exc, TimeoutError):
        message = "request_timeout: The request timed out. Try again."
    elif isinstance(exc, OSError):
        message = "io_error: Check token storage permissions and network connectivity."
    elif isinstance(exc, ValueError):
        message = (
            "invalid_arguments: Check the dates, property ID, dimensions, metrics, filters, "
            "and pagination values."
        )
    else:
        message = "internal_error: The request failed. Restart the server and try again."
    return ToolError(message)



def _tool(title: str, *, external: bool = True):
    def decorate(func):
        @wraps(func)
        def guarded(*args, **kwargs):
            try:
                return {"ok": True, **func(*args, **kwargs)}
            except Exception as exc:
                raise _tool_error(exc) from None
        return mcp.tool(annotations=ToolAnnotations(
            title=title, readOnlyHint=True, destructiveHint=False,
            idempotentHint=True, openWorldHint=external,
        ))(guarded)
    return decorate


@_tool("Check GA4 connection", external=False)
def health_check(account: str | None = None) -> dict[str, Any]:
    """Check local config and saved login state; no network, refresh, or browser login."""
    missing = missing_required_env_vars()
    if missing:
        return {"ok": False, "status": "missing_configuration", "missing_env_vars": missing}
    settings = load_settings()
    state = describe_auth_state(settings=settings, account=account)
    return {
        "ok": state["status"] in {"ready", "refreshable"},
        "status": state["status"], "has_token_file": state["has_token_file"],
        "default_property_id": settings.default_property_id,
        **({"account": account.strip()} if account is not None else {}),
    }


@_tool("List GA4 properties")
def list_properties(account: str | None = None, page_size: int = 50,
                    page_token: str | None = None) -> dict[str, Any]:
    """List accessible GA accounts and properties. Follow nextPageToken until null.

    account is a local Google-login alias. First data call opens browser OAuth if needed.
    page_size counts GA accounts (1–200), each with its propertySummaries.
    """
    return analytics.list_properties(account=account, page_size=page_size, page_token=page_token)


@_tool("Inspect GA4 property")
def get_property(property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Read a property's name, reporting timezone, currency and service tier."""
    return analytics.get_property(property_id=property_id, account=account)


@_tool("Discover GA4 dimensions and metrics")
def get_metadata(property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    """List core-report dimensions and metrics, including property-specific custom definitions.

    Realtime uses a separate schema; core metadata does not guarantee realtime support.
    """
    return analytics.get_metadata(property_id=property_id, account=account)


@_tool("Check GA4 report compatibility")
def check_compatibility(metrics: list[str], dimensions: list[str] | None = None,
                        property_id: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Check which core-report dimensions/metrics work together before querying."""
    return analytics.check_compatibility(metrics=metrics, dimensions=dimensions, property_id=property_id, account=account)


@_tool("Run a custom GA4 report")
def run_report(start_date: str, end_date: str, metrics: list[str],
               dimensions: list[str] | None = None, property_id: str | None = None,
               dimension_filter: dict[str, Any] | None = None, metric_filter: dict[str, Any] | None = None,
               order_bys: list[dict[str, Any]] | None = None, limit: int = 1000, offset: int = 0,
               currency_code: str | None = None, keep_empty_rows: bool = False,
               account: str | None = None) -> dict[str, Any]:
    """Query GA4 (max 9 dimensions, 10 metrics, 10,000 rows per call).

    Dates are inclusive: YYYY-MM-DD, today, yesterday, or NdaysAgo, in the property timezone.
    property_id is numeric or properties/123; omission uses GOOGLE_GA4_PROPERTY_ID.
    Filters use Google's FilterExpression JSON; e.g.
    {"filter":{"fieldName":"country","stringFilter":{"matchType":"EXACT","value":"Switzerland"}}}.
    order_bys uses Google OrderBy JSON, e.g. [{"metric":{"metricName":"sessions"},"desc":true}].
    Use deterministic order_bys and nextOffset to page. Metrics are numeric strings, never
    zero-filled when absent. Keep metadata/quota when interpreting data quality and limits.
    """
    return analytics.run_report(**locals())


@_tool("Run a realtime GA4 report")
def run_realtime_report(metrics: list[str] | None = None, dimensions: list[str] | None = None,
                        property_id: str | None = None, dimension_filter: dict[str, Any] | None = None,
                        metric_filter: dict[str, Any] | None = None,
                        order_bys: list[dict[str, Any]] | None = None, limit: int = 1000,
                        minutes_ago: int = 29, account: str | None = None) -> dict[str, Any]:
    """Read realtime data; defaults to activeUsers over the last 30 minutes.

    minutes_ago is inclusive (0–29 standard; 30–59 requires Analytics 360).
    Use realtime-specific dimensions/metrics such as country and activeUsers/eventCount.
    No offset exists in realtime; truncated=true means narrow dimensions/filters or raise limit.
    """
    return analytics.run_realtime_report(**locals())


@_tool("Summarise GA4 performance")
def get_summary(start_date: str, end_date: str, property_id: str | None = None,
                account: str | None = None) -> dict[str, Any]:
    """Read aggregate users, sessions, engagement, page views, events, key events and revenue."""
    return analytics.get_summary(**locals())


@_tool("Analyse GA4 traffic sources")
def get_traffic_sources(start_date: str, end_date: str, property_id: str | None = None,
                        limit: int = 50, offset: int = 0, account: str | None = None) -> dict[str, Any]:
    """Session channel and source/medium breakdown, sorted by sessions descending."""
    return analytics.get_traffic_sources(**locals())


@_tool("Find top GA4 pages")
def get_top_pages(start_date: str, end_date: str, property_id: str | None = None,
                  limit: int = 50, offset: int = 0, account: str | None = None) -> dict[str, Any]:
    """Page views, active users and engagement seconds by hostname and path (without query strings)."""
    return analytics.get_top_pages(**locals())


@_tool("Analyse GA4 events")
def get_events(start_date: str, end_date: str, property_id: str | None = None,
               limit: int = 50, offset: int = 0, account: str | None = None) -> dict[str, Any]:
    """Event counts, users and key events by eventName, sorted by eventCount descending."""
    return analytics.get_events(**locals())


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
