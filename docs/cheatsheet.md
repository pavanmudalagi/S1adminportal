# s1am Cheatsheet — Most-Used Endpoints

## Accounts

| Action | CLI | API endpoint |
|--------|-----|-------------|
| List accounts | `s1am list-accounts` | `GET /accounts` |
| Get account | `s1am get-account <id>` | `GET /accounts/<id>` |
| Get policy | `s1am account-policy <id> get` | `GET /accounts/<id>/policy` |
| Update policy | `s1am account-policy <id> update --payload '{...}'` | `PUT /accounts/<id>/policy` |

## Sites

| Action | CLI | API endpoint |
|--------|-----|-------------|
| List sites | `s1am list-sites` | `GET /sites` |
| List sites for account | `s1am list-sites --param accountId=<id>` | `GET /sites?accountId=<id>` |

## Agents / Endpoints

| Action | CLI | API endpoint |
|--------|-----|-------------|
| List agents | `s1am list-agents` | `GET /agents` |
| Filter by site | `s1am list-agents --param siteIds=<id>` | `GET /agents?siteIds=<id>` |
| All pages | `s1am list-agents --all` | (cursor-paginated) |

## Raw API Access

```bash
# Any GET
s1am raw GET /threats --param limit=50

# POST with JSON body
s1am raw POST /agents/actions/disconnect --payload '{"data":{},"filter":{"ids":["<id>"]}}'
```

## Common Flags

```
--all              Fetch all pages (cursor-paginated)
--limit N          Max results per page
--format table     Output as table (default)
--format json      Output as JSON
--format csv       Output as CSV
--param key=value  Extra query parameter (repeatable)
--payload JSON     JSON request body
--profile NAME     Use named profile from ~/.config/s1am/config
--dry-run          Print CLI command without executing (web UI)
```

## Pagination

Responses include `pagination.nextCursor`. Use `--cursor <value>` to resume, or `--all` to auto-paginate.

## Auth Header Formats

| Tenant type | S1_AUTH_PREFIX | Header sent |
|-------------|----------------|-------------|
| Most tenants | (empty) | `Authorization: <token>` |
| Some tenants | `APIToken` | `Authorization: APIToken <token>` |
