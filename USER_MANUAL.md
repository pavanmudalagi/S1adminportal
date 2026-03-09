# SentinelOne Account Manager (s1am) User Manual

## 1. Overview

`s1am` is a command-line utility for SentinelOne management API operations.  
It can be used from source (`python -m s1am ...`) or as a standalone executable (`./s1am`).

## 2. Security First

- Do not share real API tokens.
- Do not commit `.env` files with real values.
- Prefer runtime token entry when sharing binaries.
- Use `.env.example` for placeholders only.

The CLI supports secure runtime token input:
- If `S1_API_TOKEN` is missing, the tool prompts for token with hidden input.

## 3. Prerequisites

### Source Mode
- Python 3.9+
- Network access to SentinelOne tenant
- Valid SentinelOne API token

### Standalone Mode
- Matching OS/architecture binary (for this release: macOS arm64)

## 4. Configuration

The tool reads configuration from environment variables:

- `S1_BASE_URL` (required)
- `S1_API_TOKEN` (required, or enter at prompt)
- `S1_AUTH_PREFIX` (optional, default often `APIToken`)
- `S1_API_PATH` (optional, default `/web/api/v2.1`)
- `S1_TIMEOUT` (optional, default `30`)
- `S1_VERIFY_SSL` (optional, default `true`)

Example:

```bash
export S1_BASE_URL="https://your-tenant.sentinelone.net"
export S1_AUTH_PREFIX="APIToken"
```

## 5. Running the Tool

### Standalone Binary

```bash
./s1am --help
```

### Source Mode

```bash
python -m s1am --help
```

## 6. Common Commands

List accounts:

```bash
./s1am list-accounts --limit 100
```

Get account details:

```bash
./s1am get-account <account_id>
```

Get account policy:

```bash
./s1am account-policy <account_id> get
```

Update account policy:

```bash
./s1am account-policy <account_id> update --payload '{"policy":{"threatReboot":true}}'
```

List sites:

```bash
./s1am list-sites --param accountId=<account_id>
```

List agents:

```bash
./s1am list-agents --param siteIds=<site_id>
```

Call raw API endpoint:

```bash
./s1am raw GET sites --param limit=50
./s1am raw POST sites --payload '{"name":"Demo Site","accountId":"<account_id>"}'
```

## 7. Pagination

Use `--all` for commands that support multi-page retrieval:

```bash
./s1am list-accounts --all
./s1am list-sites --all
./s1am list-agents --all
```

Use `--cursor` when manually continuing from a known cursor.

## 8. Error Handling

Typical failures:
- Missing required configuration (`S1_BASE_URL`, token)
- Invalid payload JSON
- HTTP errors from SentinelOne API
- Connectivity or SSL issues

The CLI prints errors to stderr and exits with non-zero status.

## 9. Build Standalone Release

From project root:

```bash
./scripts/build_standalone.sh
```

Output:
- `release/s1am`
- `release/README.md`
- `release/.env.example`
- `release/QUICKSTART.txt`

Optional single-file share:

```bash
cd release
zip -r s1am-macos-arm64.zip s1am README.md .env.example QUICKSTART.txt
```

## 10. Safe Sharing Checklist

- Verify `.env` is not present in shared package.
- Verify `.env.example` contains placeholders only.
- Verify no token appears in docs/scripts/history.
- Share only the `release/` folder or zip.

