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

## Marketplace Connectors Tracker

A read-only page that lists every account with Singularity Marketplace connectors (data ingest, SIEM, identity, threat
intel…) across all configured consoles, with a drilldown console → account → site → connector and a reverse view per
connector. Open it from **Marketplace → Marketplace Connectors** or `#marketplace`.

- Consoles are scanned in parallel; calls inside one console are sequential with a small delay and back off on HTTP 429
  (the Marketplace API allows 5 requests/s per token). A console whose token is invalid or lacks the *Singularity
  Marketplace · View* permission shows an error banner and row while the others still load.
- Results are cached in memory for 5 minutes; **Refresh** bypasses the cache. Tokens never leave the server.
- Filters: view (Accounts / Connectors / All instances), console, connector, status (enabled, disabled, has alert) and
  free text. The URL hash carries the state, so `#marketplace?console=USEA1&account=<id>` links straight to an account.
- **Export CSV** downloads the rows currently shown (same filters, applied server-side).
- Clicking an instance shows its fields plus two on-demand loads: the invocation log and the config with secrets masked.
- Set `S1AM_MP_CONSOLES=Staging,USEA1` (or `consoles = …` under `[marketplace]` in `~/.config/s1am/config`) to limit the
  scan to some consoles.

**Retention.** The "Retention (DV)" column is the Deep Visibility data retention configured on the scope. Source:
`GET /accounts` and `GET /sites` → `licenses.settings[]`, the entry whose `groupName` is `dv_retention` ("14 Days",
"30 Days", "90 Days"…). Accounts and sites each carry their own value; the API has no inherit flag, so a site value equal
to its account's is labelled *inherited* and a different one *overrides account*. Sites without a `dv_retention` entry
(seen on Staging) show "not exposed by API" with the account value for context. Data Ingest retention is only visible as
licensed add-on modules `xdr_30d_retention` … `xdr_365d_retention`, shown as "ingest add-ons: up to N d". The
per-instance Marketplace config, the account/site policy and the Swagger have no other retention field. Filter with the
Retention select (under 30 days, exact values, not exposed); the CSV carries value, days, add-ons and source.

What the API gives us (observed on the Staging console): each installed app's `scopes[]` holds one instance with
`scopeLevel` (`account`, `site` or `global`), `scopeId`, `site`/`siteId`, the actual `status` next to `desiredStatus`,
`errorLog`, `retryUntil`, creator/modifier and timestamps. The list accepts `accountIds` and `siteIds` filters;
`groupIds` is ignored. The instance `log` endpoint ignores `limit`, so the server truncates it.

## Usage & Billing (SDL metering)

Daily usage from the SDL PowerQuery `metering` datasource, rolled up to one billable number per account and site per
month. Open **Usage & Billing** in the UI or `#usage?month=2026-10`.

**Token permission.** Each console's token needs **Metering Reports – View** (Admin and C-Level roles have it). The MGMT
API token works as the `Bearer` token for `/sdl/api/powerQuery`; set `s1_powerquery_token` in a profile to use a separate
one. A console whose token lacks the permission shows "token lacks Metering Reports – View" in the health strip and the
other consoles still load. Consoles without metering show "not enabled"; products a console does not report are skipped.

**Product config.** `src/s1am/metering/products.json` lists the billable reports: `report` (metering `report_name`),
`label`/`short`/`unit`, `rollup` (`peak` | `average` | `month_end` | `sum`), `alt_rollups` offered in the UI, and an
optional `breakdown` column (`endpoint_bundle`, `cloud_provider`, `source_name`…). `bytes_base` (1000 or 1024) drives
GB/TB display. To add a report (for example `ltdr_queries`, `workstation_endpoints`, `enrolled_mobile_devices` or
`hyper_automate_workflow_actions_ran`) append an entry and restart; discovery (`from "reports"`) decides per console
whether it is fetched.

**Data flow.** `Sync month` (or `s1am usage-sync [--month YYYY-MM] [--consoles A,B] [--force]` from cron) fans out across
consoles, runs one aggregated query per product (`group value=sum(value) by date, account, site, state`) and stores daily
rows in `~/.config/s1am/usage.db`. Archived days are never re-fetched; live days are. A daily background sync of the
previous and current month runs while the web server is up (`[usage] daily_sync = false` disables it, `daily_sync_hour`
sets the hour). Scope the consoles with `S1AM_USAGE_CONSOLES=Staging` or `[usage] consoles`.

**Rollups.** Agent and user counts default to the peak daily value (average and month-end selectable per column), CNS to
the average, Data Ingest to the sum of bytes. Rows with any `live` day carry a *provisional* badge; the "Finalize for
billing" export is only enabled when every row is final.

API facts observed on Staging: always send `endTime` (with only `startTime` the window is wrong); `endTime: "now"` is
rejected, use dates; results are capped at 1000 rows unless the query sets `| limit`; `xdr_ingested_bytes` and
`cns_workloads` carry scope ids but no names (joined from the `tenants` table); identity reports leave
`account_scope_id` empty and scope by `tenant_scope_id` / `tenant_scope_level`; data lags about two days.

Note: the older **Metering** category commands in the UI call a `metering` CLI subcommand that does not exist in this
repo; use Usage & Billing instead.

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
