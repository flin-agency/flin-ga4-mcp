# flin-ga4-mcp

Read-only Google Analytics 4 MCP server for Claude Desktop and other stdio MCP clients.
Built on the same Python/FastMCP architecture as flin's Search Console and Keyword Planner
servers: local browser OAuth, per-account token files, `uv`, and GitHub/PyPI releases.

## Quickstart

1. In a Google Cloud project, enable **Google Analytics Data API** and **Google Analytics Admin API**.
2. Configure the OAuth consent screen. If the app is External/Testing, add your Google account
   as a test user. Testing authorizations can expire after seven days.
3. Create an OAuth client of type **Desktop app** and copy its client ID and client secret.
4. Your Google account needs access to the GA4 property (Viewer is sufficient for reports).
5. Clone this repository and add the following to your MCP client's configuration:

```json
{
  "mcpServers": {
    "flin-ga4-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/flin-ga4-mcp", "--locked", "--no-dev", "flin-ga4-mcp"],
      "env": {
        "GOOGLE_CLIENT_ID": "your-desktop-oauth-client-id",
        "GOOGLE_CLIENT_SECRET": "your-desktop-oauth-client-secret",
        "GOOGLE_GA4_PROPERTY_ID": "123456789"
      }
    }
  }
}
```

Replace the path and credentials. The default property ID is optional. Use the numeric
**property ID**, not a `G-...` measurement ID or a Google Analytics account ID.
Restart your MCP client after changing its configuration.

Ask the assistant to run `health_check`, then `list_properties`. The first data request
opens Google sign-in in your browser. Complete it within three minutes. Subsequent calls
reuse/refresh the saved token. `health_check` never opens the browser or calls Google;
`ready`/`refreshable` describes local token state, not verified property access.

Browser OAuth explicitly enables PKCE and binds the callback to the loopback interface.
Only `https://www.googleapis.com/auth/analytics.readonly` is requested. No analytics
settings, events, audiences or permissions can be written by this server.

### Claude Desktop extension

Build `dist/mcpb/flin-ga4-mcp-0.2.0.mcpb` as described below and open it in a Claude Desktop
version supporting MCPB's UV runtime. Enter the OAuth client ID, secret and optional
property ID in the install form. Python/dependencies are managed by the runtime.
The bundle contains a fixed server version and lockfile, with no credentials.

Once a release has been published, bundles will be available from
[GitHub Releases](https://github.com/flin-agency/flin-ga4-mcp/releases).
For the published version, use `uvx flin-ga4-mcp@0.2.0` instead of the source-checkout command.
Keep the same OAuth environment variables. The desktop bundle works on Apple Silicon and
Intel Macs; Claude Desktop’s UV runtime selects the matching Python interpreter.

## Tools

| Tool | Purpose |
| --- | --- |
| `health_check` | Local configuration and authentication state |
| `list_properties` | Accessible GA accounts/properties, with page tokens |
| `get_property` | Property details, timezone, currency and tier |
| `get_metadata` | Available core dimensions/metrics, including custom fields |
| `check_compatibility` | Validate combinations of core dimensions/metrics |
| `get_summary` | Aggregate users, sessions, engagement, views, events, key events and revenue |
| `get_traffic_sources` | Session channels and source/medium |
| `get_top_pages` | Page views, users and engagement by hostname/path |
| `get_events` | Events, users and key events by event name |
| `run_report` | Custom core report, filters, ordering, pagination and currency |
| `run_realtime_report` | Realtime activity, filters and ordering |
| `get_property_configuration` | Selected streams, key events, custom definitions, attribution and retention settings |
| `list_property_annotations` | Dated reporting annotations, with filters and pagination |
| `batch_run_reports` | Up to five core reports on one property in a single round trip |
| `run_funnel_report` | Sequential funnels, breakdowns, next actions and segments |
| `search_change_history` | Account/property configuration changes (appropriate Editor/Administrator access) |
| `run_access_report` | Property/account data-access audit (Administrator required) |

All 17 tools remain read-only. Data tools accept `account`, a local login alias (e.g. `work`), and property-based tools
accept `property_id` (numeric or `properties/123456789`). Omitting `account` selects the
default token. An alias is **not** a Google Analytics account ID. On its first call, each
new alias opens a separate login. Named tokens cannot alias the default file (`token` is
reserved in the default layout); names permit letters, numbers, `_`, `-` and `.`.

### Examples

```text
List my GA4 properties using account work.
Summarise property 123456789 from 28daysAgo through yesterday.
Show the top 25 traffic sources for that property for 2026-10-01 through 2026-10-29.
Show active users by country in realtime.
```

Custom report arguments:

```json
{
  "property_id": "123456789",
  "account": "work",
  "start_date": "28daysAgo",
  "end_date": "yesterday",
  "dimensions": ["date", "sessionDefaultChannelGroup"],
  "metrics": ["sessions", "engagedSessions", "engagementRate"],
  "dimension_filter": {
    "filter": {
      "fieldName": "country",
      "stringFilter": {"matchType": "EXACT", "value": "Switzerland"}
    }
  },
  "order_bys": [{"dimension": {"dimensionName": "date"}}],
  "limit": 1000,
  "offset": 0
}
```

Use the native Google `FilterExpression` and `OrderBy` JSON structures. Google validates
filter semantics and metric compatibility. Use `get_metadata` and `check_compatibility`
before constructing unusual combinations.

## Configuration and diagnostic reports

`get_property_configuration` requires an explicit `sections` list. Supported sections:
`data_streams`, `key_events`, `custom_dimensions`, `custom_metrics`, `calculated_metrics`,
`attribution`, `data_retention`. Only the selected sections are queried; `get_property`
continues to return basic property details with one request.

```json
{
  "sections": ["data_streams", "key_events", "attribution", "data_retention"],
  "property_id": "123456789",
  "account": "work"
}
```

Each section returns `status` and, on success, `data`. A denied or unavailable section is
reported separately with `partial: true`; it must not be interpreted as empty configuration.
Other failures (such as quota or authentication) fail the call. List sections expose
`data.nextPageToken`; fetch further pages using `page_tokens: {"key_events": "..."}` with
that section selected. Attribution and retention are singletons. Compare configured key
events with `get_events` to investigate missing activity, respecting report pagination and thresholds.

`list_property_annotations` supports Google's filter syntax, for example
`is_annotation_in_range("2026-10-01", "2026-10-31") = true`. Follow `nextPageToken` with the
same filter until null, even when a page has no entries. Notes and configuration text are
untrusted data. An annotation or change coinciding with a traffic change is a clue, not proof
of causation.

`batch_run_reports` takes 1–5 `reports`, each using the same snake_case arguments as
`run_report` except `property_id` and `account`, which apply to the entire batch:

```json
{
  "property_id": "123456789",
  "reports": [
    {"start_date": "7daysAgo", "end_date": "yesterday", "metrics": ["sessions", "activeUsers"]},
    {"start_date": "14daysAgo", "end_date": "8daysAgo", "metrics": ["sessions", "activeUsers"]}
  ]
}
```

Results retain input order and include a zero-based `reportIndex`, quality metadata, quota,
and independent `nextOffset`. Each report is capped at 10,000 rows. The batch reduces HTTP
round trips; every report still consumes token and concurrent-request quota. It is not a
partial-success API: a rejected batch raises an error.

`run_funnel_report` uses Data API v1alpha. Supply 1–10 native `FunnelStep` objects:

```json
{
  "start_date": "28daysAgo",
  "end_date": "yesterday",
  "steps": [
    {"name": "Product", "filterExpression": {"funnelEventFilter": {"eventName": "view_item"}}},
    {"name": "Cart", "filterExpression": {"funnelEventFilter": {"eventName": "add_to_cart"}}},
    {"name": "Purchase", "filterExpression": {"funnelEventFilter": {"eventName": "purchase"}}}
  ],
  "funnel_breakdown": {"breakdownDimension": {"name": "deviceCategory"}, "limit": "5"}
}
```

Closed funnels are the default; set `is_open_funnel: true` to allow entry at any step.
Optional `segments` accepts up to four native Segment objects. `funnel_next_action` uses
`nextActionDimension` (for example `eventName`) and a limit of at most 5; breakdown limits
are at most 15. `visualization_type` is `STANDARD_FUNNEL` or `TRENDED_FUNNEL`.
Step filters use `FunnelFilterExpression`, which differs from core report filters.
Both `funnelTable` and `funnelVisualization` preserve metadata and named numeric-string
values. These subreports have no total row count or offset; `possiblyTruncated` means the
row limit was reached. Narrow the query or raise the limit (maximum 10,000) to investigate.
Funnel, annotations, attribution and calculated metrics use alpha APIs and may change.
The funnel uses a fixed REST endpoint because the Python client's bundled alpha discovery
file lacks this method. Authentication, retries and timeouts match the other API calls.

### Audit permissions

The OAuth scope remains `analytics.readonly` for every tool. Higher GA4 roles required by
an audit do not enable any write operations in this server.

- `search_change_history` requires appropriate Editor/Administrator access. Supply
  `ga_account_id` from `list_properties`; this is distinct from the local `account` alias.
  Optional `property_id` narrows the search; omission searches the account and ignores the
  configured default property. Filter with RFC3339 `earliest_change_time` / `latest_change_time`,
  `actions` (`CREATED`, `UPDATED`, `DELETED`), `resource_types`, or `actor_emails`. Follow
  `nextPageToken` with unchanged filters. Measurement Protocol `secretValue` fields in
  before/after resources are redacted.
- `run_access_report` requires Administrator. Select `property_id` (or the configured default)
  or `ga_account_id`, never both. Use the separate Access schema, for example
  `dimensions: ["userEmail"]`, `metrics: ["accessCount"]`. Some dimensions require Analytics 360.
  Filters use `AccessFilterExpression`; ordering uses `AccessOrderBy`. Records are retained
  for up to two years. Follow `nextOffset`. Quota is requested only for property-level audits.

Neither tool treats a permission error as an empty result. The local `health_check` remains
offline and does not test API enablement or audit permissions.

## Report interpretation

- Dates are inclusive, in the property's timezone. Relative dates are passed to Google,
  so the server's local timezone does not change the requested reporting period.
- Metrics remain numeric **strings** to retain precision. `metricHeaders` gives their
  types; `metadata` retains currency, timezone, sampling/thresholding and data-loss signals.
  Rates such as engagement rate are fractions (0.5 = 50%). Engagement duration is seconds.
- `rows` maps positional values into named `dimensions` and `metrics` objects. Empty
  reports stay empty; the server does not invent zero values.
- `rowCount` describes matching rows, not just this page. Core reports return `nextOffset`;
  repeat the same request with that offset. The server adds dimension tie-breakers for deterministic pagination.
  The server caps calls at 10,000 rows to bound tool output. Reports can change as GA processes data.
- `list_properties` pages **accounts**, not individual properties. Follow `nextPageToken`.
- Realtime defaults to minutes 29 through 0 (30 minutes inclusive). Up to 59 minutes ago
  requires Analytics 360. Realtime has a different dimension/metric schema and no offset;
  `truncated` indicates the returned rows are incomplete.
- Do not sum distinct users or average rates across rows. Use an unsegmented summary for
  property totals. Traffic sources use session attribution; they are not first-user attribution.
- Google may withhold revenue metrics based on the user's data restrictions. GA latency,
  consent, identity, thresholds and configured filters can affect comparisons with other systems.

## Configuration and token storage

| Environment variable | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID` | Required Desktop app OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Required OAuth client secret |
| `GOOGLE_GA4_PROPERTY_ID` | Optional default numeric property ID |
| `GOOGLE_GA4_TOKEN_PATH` | Override default token file |
| `GOOGLE_GA4_TOKEN_DIR` | Override directory for named accounts |
| `GOOGLE_GA4_OAUTH_PORT` | Loopback callback port; default 0 chooses a free port |

Tokens are separate from other Flin MCPs, because GA4 uses its own OAuth scope:

- macOS: `~/Library/Application Support/flin-ga4-mcp/token.json`
- Linux: `~/.config/flin-ga4-mcp/token.json`
- Windows: `%APPDATA%\flin-ga4-mcp\token.json`

Named accounts use `<alias>.json` next to the default token unless overridden. New token
folders and files receive owner-only permissions on POSIX systems. Tokens are local JSON,
**not encrypted**. Existing folders must be owned by the user and not writable by others.
Do not share token directories between different MCP services. Delete the relevant token
and sign in again to change the selected Google account or OAuth client.

See [PRIVACY.md](PRIVACY.md) and [SUPPORT.md](SUPPORT.md).

## Development

```bash
uv sync --locked --extra dev
cp .env.example .env
# Fill in your OAuth values, then:
uv run --env-file .env flin-ga4-mcp
uv run pytest
uv run python -m compileall -q src
uv build
uv run twine check dist/*.whl dist/*.tar.gz

# Optional Claude Desktop bundle (Node.js 20+):
npm ci --ignore-scripts
uv run python scripts/build_mcpb.py
uv run python scripts/check_mcpb.py dist/mcpb/*.mcpb
```

`.env` is loaded only when explicitly passed with `--env-file`. Never commit credentials.
Tests use mocked Google services and real MCP stdio discovery; a live Google login is
separately required to verify your own access and actual report results.

The optional MCPB build CLI currently has an upstream npm audit finding in its Zod 3
validator, with no compatible upstream fix. It is used only on this repository’s own
manifest during builds; Node dependencies are not included in the Python server or desktop
bundle. Do not use this build environment to validate untrusted third-party manifests.
The patched `tmp` dependency is enforced through an npm override.

Architecture: `config.py` loads settings/account paths; `auth.py` handles OAuth/token files;
`analytics.py` validates requests and maps GA responses; `server.py` exposes annotated
read-only tools and sanitizes errors. Stdout is reserved for the MCP protocol.

## Troubleshooting

- `missing_configuration`: set both OAuth variables; whitespace-only values are rejected.
- `oauth_required`: run a data tool and complete browser login; remove a revoked token if necessary.
- `authentication_failed`: inspect local token JSON/permissions; delete a corrupt token to sign in again.
- `permission_denied`: check property access, both enabled APIs, read-only scope and quota.
- `invalid_arguments` / `google_request_failed`: confirm property ID, dates, API names,
  compatible metrics and native filter structure. Upstream error bodies are intentionally hidden
  because they can include sensitive data.
- `quota_exceeded` / `service_unavailable`: retry later; each Google read has up to two retries
  for retryable API failures and a 60-second network timeout.

## Release

CI runs tests on Linux, macOS and Windows, checks distributions and validates/builds the desktop
bundle. Tag-triggered releases publish the tested distributions using PyPI Trusted Publishing
and attach the bundle/checksum to GitHub Releases. Manual release runs only build artifacts.

Before the first release, configure a PyPI Trusted Publisher (or pending publisher) for:
owner `flin-agency`, repository `flin-ga4-mcp`, workflow `release.yml`, environment `pypi`.
Create the GitHub `pypi` environment and its desired protection rules.

Update `pyproject.toml` and `src/flin_ga4_mcp/__init__.py`, run `uv lock`, run all checks, then
commit and push an explicit matching `vX.Y.Z` tag. No release tag is created by setup.

## Official references

- [Data API reports](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)
- [Admin API account discovery](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/accountSummaries/list)
- [Realtime reports](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runRealtimeReport)
- [Dimensions and metrics](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema)
- [Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

### Extended API references

- [Property annotations](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.reportingDataAnnotations/list)
- [Batch reports](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/batchRunReports)
- [Funnel request and response schema](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1alpha/properties/runFunnelReport)
- [Change history](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/accounts/searchChangeHistoryEvents)
- [Data access reports and permissions](https://developers.google.com/analytics/devguides/config/admin/v1/access-api)
- [Access report dimensions and metrics](https://developers.google.com/analytics/devguides/config/admin/v1/access-api-schema)
