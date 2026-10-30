# Privacy — flin GA4 MCP

This MCP runs locally. It sends OAuth requests to Google and read-only Analytics requests to
Google Analytics Admin/Data APIs. Returned analytics data is provided to the MCP client you
choose and is subject to that client's privacy settings. The server does not send analytics
data to flin GmbH and contains no telemetry or remote analytics storage.

OAuth access/refresh tokens and the OAuth client configuration are stored in local JSON token
files, outside the installed package. They are not encrypted by this server. On POSIX systems
new directories use mode 0700 and files use mode 0600. On Windows, protection relies on the
user profile's filesystem permissions. Credentials are never intentionally returned in tool
results; upstream error bodies are suppressed. The OS browser and Google handle sign-in.

Report data is processed in memory and returned to the MCP client, without a persistent report
cache. Tokens remain until you delete the token file/directory documented in README.md.
Uninstalling the server does not delete tokens. Revoke Google account app access as well if you
want to invalidate authorization. Deleting local tokens alone does not revoke Google's grant.

Google and your MCP client may retain data under their own policies. Read
[Google's privacy policy](https://policies.google.com/privacy) and your client's policy.

Publisher: flin GmbH, Weinbergstrasse 31, 8006 Zürich, Switzerland.
Privacy/security/support contact: flin@flin.agency.
