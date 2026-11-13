# SentinelOne Account Manager (s1am)

A small Python CLI and client wrapper to automate SentinelOne account management tasks.

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Export your SentinelOne API settings (see `.env.example`):

```bash
export S1_BASE_URL="https://your-tenant.sentinelone.net"
export S1_API_TOKEN="YOUR_API_TOKEN"
export S1_AUTH_PREFIX="APIToken"
```

If you do not set `S1_API_TOKEN`, the CLI will prompt for the token at runtime with hidden input.

## Usage

Run as a module:

```bash
python -m s1am list-accounts
```

Common commands:

```bash
python -m s1am list-accounts --limit 100
python -m s1am get-account <account_id>
python -m s1am account-policy <account_id> get
python -m s1am list-sites --param accountId=<account_id>
python -m s1am list-agents --param siteIds=<site_id>
```

Call any endpoint (use your tenant API docs to confirm paths and payloads):

```bash
python -m s1am raw GET sites --param limit=50
python -m s1am raw POST sites --payload '{"name":"Demo Site","accountId":"<account_id>"}'
```

## Web UI

```bash
./scripts/start.sh            # opens http://127.0.0.1:5173
python run_web.py --port 8080 --no-browser
```

The browser UI follows the SentinelOne "Unity" console design language: dark theme by default, a light theme from
**⚙ My preferences** (instant apply, remembered per browser; `?theme=light` in the URL also works), a 46px top bar with the
console scope selector, and a narrow sidebar of categories. Picking a category shows its commands as a tab strip; the landing
page is a searchable directory of every command, with pages greyed out when their token is not configured.
Press **⌘K / Ctrl+K** to find a page by name. Every page has a deep link (`#list-accounts`, `#eap-tracker`, …).

## Notes

- This project is aligned to the attached `swagger_2.1.json` (S1 MGMT API 2.1). Paths use `/web/api/v2.1/...`.
- Authentication is the `Authorization` header. By default the CLI prefixes your token with `APIToken`, but you can set `S1_AUTH_PREFIX` to empty if your token already includes a prefix.
- The CLI prints raw JSON responses for easy piping into `jq` or saving to a file.

## Salesforce Connection (EAP tracker)

The EAP request tracker enriches requests from Salesforce (read-only). Sign-in is browser
SSO — OAuth 2.0 authorization code + PKCE with Salesforce's public `PlatformCLI` client, so
no Connected App or password is needed:

```bash
pip install simple-salesforce          # or: pip install -e ".[eap]"
s1am sf-check --login                  # opens the browser once; prints org + user
s1am sf-check                          # later runs reuse the cached session
s1am sf-check --logout                 # delete the cached tokens
```

Tokens are cached at `~/.config/s1am/sf_tokens.json` (mode 0600). If the
product-health-dashboard has already signed in (`~/.product-health-dashboard/tokens.json`),
its session is imported on first use so one sign-in serves both apps.

Optional settings — environment variables win over a `[salesforce]` section in
`~/.config/s1am/config`:

| Env var | Config key | Default |
|---|---|---|
| `SF_CLIENT_ID` | `client_id` | `PlatformCLI` |
| `SF_REDIRECT_URI` | `redirect_uri` | `http://localhost:1717/OauthRedirect` |
| `SF_LOGIN_URL` | `login_url` | `https://login.salesforce.com` (set your My Domain host if SSO requires it) |
| `SF_CLIENT_SECRET` | `client_secret` | — (only for a Connected App that requires one) |
| `SF_PROMPT` | `prompt` | `login` (always ask which account; empty reuses the browser session) |
| `SF_TOKEN_FILE` | `token_file` | `~/.config/s1am/sf_tokens.json` |

Fallback if OAuth is blocked in your org: `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN`
(and `SF_DOMAIN`, default `login`).

## Prompt EAP Request Tracker

The **Prompt EAP** section of the web UI lists every Prompt Security early-access request
from the Microsoft Forms workbook, enriched from Salesforce and cross-checked on the
SentinelOne consoles (read-only):

1. **Sync Excel** — reads the workbook (`Id` is the key; re-syncing is idempotent and never
   overwrites fields you edited in the app).
2. **Enrich Salesforce** — one batched query per rule: Joe's tenant link → `1000_` tenant on
   the form → S1 account ID → contact email → email domain → company name. Picks the
   `Contract` tenant, pulls country, Customer ID and Complete/Control agent counts.
3. **Check Consoles** — `GET /accounts/{id}` on the matching console: is a Prompt bundle
   present, and if so the tenant name and first users.

Click a row for the full detail (candidates, raw form text, console data, history) and the
editable provisioning fields (tenant name, first users, notes, enabled Y/N …).

```bash
s1am eap-sync [--path FILE]          # import / refresh from the workbook
s1am eap-enrich [--ids 1,2]          # Salesforce enrichment (needs sf-check sign-in)
s1am eap-check  [--ids 1,2]          # console cross-check
s1am eap-seed "EAP - Customer List.xlsx"   # one-time: carry over Notes / Enabled / Date / By
```

Settings — `S1AM_EAP_<KEY>` env vars win over an `[eap]` section in `~/.config/s1am/config`:

| Key | Default |
|---|---|
| `form_path` | the copy synced to `~/Library/CloudStorage/OneDrive-N-Able,Inc/`, else the newest `*Early Access*.xlsx` in `~/Downloads` |
| `db_path` | `~/.config/s1am/eap.db` |
| `default_count` | `-1` (unlimited) for Prompt bundles |
| `actioned_by` | the signed-in Salesforce user |
| `dry_run` | `true` |

Graph mode (pulling the workbook straight from the sharing link) is implemented in
`s1am.eap.graph` but needs an Entra app registration that Conditional Access allows; the
default public client is blocked in this tenant.

## Build A Standalone App (shareable)

Build a standalone binary with PyInstaller:

```bash
./scripts/build_standalone.sh
```

This generates a share-ready folder at `release/` with:
- `s1am` (standalone executable)
- `README.md`
- `.env.example` (placeholders only)
- `QUICKSTART.txt`

Security guidance for sharing:
- Never include a real `.env` file when sharing.
- Do not hardcode tokens in scripts.
- Token can be entered at runtime (hidden prompt) so it is not saved to files.
