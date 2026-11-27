# Security Policy

## Supported Versions

Only the latest released version on `main` receives security fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately via GitHub's **Security → Report a vulnerability** (Private Vulnerability Reporting) or contact the maintainer directly. Include a clear description and reproduction steps without exposing real credentials.

## Privacy Reminder for Bug Reports

When opening regular issues or attaching logs/API excerpts, please **redact**:
- OmaWatti usernames and passwords
- `x-frends-bearer-token` JWT tokens
- `customerNumber` and full metering point GSRN numbers (`usageplacenumber`)
- Street addresses if you prefer not to publish them

## Security Design

- **Zero external runtime dependencies** (`"requirements": []` in `manifest.json`) — uses only Home Assistant Core's `aiohttp` session with standard TLS certificate verification.
- **Hardcoded API origin** (`https://omawatti.lahtienergia.fi/backend/api/v1/enduser`) — no user-supplied URLs.
- **Credential handling** — credentials are stored solely in Home Assistant's internal config entry storage (never in entity attributes or logs). Bearer JWT tokens are kept in memory only and refreshed proactively before expiry.
- **Auth failure protection** — HTTP 401 triggers at most one re-authentication attempt, and invalid credentials raise `ConfigEntryAuthFailed` to pause polling until re-authenticated.
