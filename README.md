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
