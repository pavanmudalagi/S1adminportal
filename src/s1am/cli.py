import argparse
import json
import sys

from .api.client import SentinelOneClient
from .api.config import Config
from .api.exceptions import SentinelOneError


def _print_json(data):
    print(json.dumps(data, indent=2, sort_keys=True))


def _parse_params(params_list):
    params = {}
    for item in params_list or []:
        if "=" not in item:
            raise ValueError(f"Invalid param '{item}'. Use key=value.")
        key, value = item.split("=", 1)
        params[key] = value
    return params


def build_client(profile=None):
    config = Config(profile=profile)
    config.validate()
    return SentinelOneClient(
        base_url=config.base_url,
        api_token=config.api_token,
        api_path=config.api_path,
        timeout=config.timeout,
        verify=config.verify_ssl,
        auth_prefix=config.auth_prefix,
    )


def cmd_list_accounts(args):
    client = build_client(profile=args.profile)
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("accounts", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "accounts", params=params)
        _print_json(data)


def cmd_get_account(args):
    client = build_client(profile=args.profile)
    data = client.request("GET", f"accounts/{args.account_id}")
    _print_json(data)


def cmd_account_policy(args):
    client = build_client(profile=args.profile)
    path = f"accounts/{args.account_id}/policy"
    if args.action == "get":
        data = client.request("GET", path)
    else:
        payload = SentinelOneClient.load_json_payload(args.payload)
        if payload is None:
            raise ValueError("--payload is required for policy update")
        data = client.request("PUT", path, payload=payload)
    _print_json(data)


def cmd_list_sites(args):
    client = build_client(profile=args.profile)
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("sites", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "sites", params=params)
        _print_json(data)


_DATA_INGEST_KEYWORDS = frozenset([
    "data ingest", "sdl", "xdr", "unified data lake", "ai siem", "data lake",
])

_RETENTION_KEYWORDS = frozenset([
    "retention", "ltdr", "long-term", "long term",
])


def _module_matches_any(module, keywords):
    text = (
        (module.get("displayName") or "") + " " + (module.get("name") or "")
    ).lower()
    return any(kw in text for kw in keywords)


def cmd_sites_with_data_ingest(args):
    client = build_client(profile=args.profile)
    params = {"limit": 1000}
    if args.account_id:
        params["accountIds"] = args.account_id

    pages = client.paginate("sites", params=params)

    sites_all = []
    for page in pages:
        data = page.get("data") or {}
        if isinstance(data, dict):
            sites_all.extend(data.get("sites") or [])
        elif isinstance(data, list):
            sites_all.extend(data)

    rows = []
    for site in sites_all:
        modules = (site.get("licenses") or {}).get("modules") or []
        di_modules = [m for m in modules if _module_matches_any(m, _DATA_INGEST_KEYWORDS)]
        ret_modules = [m for m in modules if _module_matches_any(m, _RETENTION_KEYWORDS)]
        if di_modules and ret_modules:
            rows.append({
                "accountId": site.get("accountId") or "",
                "accountName": site.get("accountName") or "",
                "siteName": site.get("name") or "",
                "dataIngestSelection": ", ".join(
                    m.get("displayName") or m.get("name") or "" for m in di_modules
                ),
            })

    _print_json({"count": len(rows), "data": rows})


def cmd_list_agents(args):
    client = build_client(profile=args.profile)
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("agents", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "agents", params=params)
        _print_json(data)


def _console_name(url):
    """Derive a short display name from a SentinelOne console URL."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    for suffix in (".sentinelone.net", ".sentinelone.com"):
        if host.endswith(suffix):
            return host[: -len(suffix)]
    return host


def _find_profile_for_url(url):
    """Return profile name whose s1_base_url matches url (case-insensitive), or None."""
    import configparser
    from pathlib import Path
    cfg_path = Path.home() / ".config" / "s1am" / "config"
    if not cfg_path.exists():
        return None
    parser = configparser.ConfigParser()
    parser.read(str(cfg_path))
    normalized = url.rstrip("/").lower()
    for section in parser.sections():
        stored = parser.get(section, "s1_base_url", fallback="").rstrip("/").lower()
        if stored == normalized:
            return section
    return None


def cmd_lookup_account_id(args):
    client = build_client(profile=args.profile)
    # The API `name` param does a server-side contains filter; we do client-side exact if requested.
    pages = client.paginate("accounts", params={"name": args.account_name, "limit": 100})
    accounts = [item for page in pages for item in (page.get("data") or [])]
    if args.exact:
        accounts = [a for a in accounts if a.get("name", "").lower() == args.account_name.lower()]
    if not accounts:
        print(json.dumps({"matches": [], "count": 0}, indent=2))
        return
    matches = [{"id": a["id"], "name": a.get("name"), "state": a.get("state")} for a in accounts]
    _print_json({"count": len(matches), "matches": matches})


def cmd_lookup_accounts_bulk(args):
    import csv

    if args.input_file == "-":
        f_in = sys.stdin
        close_after = False
    else:
        f_in = open(args.input_file, newline="")
        close_after = True

    try:
        reader = csv.reader(f_in)
        rows = []
        for r in reader:
            col_a = r[0].strip() if len(r) > 0 else ""
            col_b = r[1].strip() if len(r) > 1 else ""
            if col_a and col_b:
                rows.append((col_a, col_b))
    finally:
        if close_after:
            f_in.close()

    # Skip header row if present
    if rows and rows[0][0].lower() in ("console url", "console_url", "url", "console", "column a"):
        rows = rows[1:]

    results = []
    for console_url, account_name in rows:
        console_name = _console_name(console_url)
        profile = _find_profile_for_url(console_url)

        if profile is None:
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": None,
                "status": "NO_PROFILE",
            })
            continue

        try:
            client = build_client(profile=profile)
            pages = client.paginate("accounts", params={"name": account_name, "limit": 100})
            accounts = [item for page in pages for item in (page.get("data") or [])]
            match = next(
                (a for a in accounts if a.get("name", "").lower() == account_name.lower()),
                None,
            )
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": match["id"] if match else None,
                "status": "FOUND" if match else "NOT_FOUND",
            })
        except SentinelOneError as exc:
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": None,
                "status": f"ERROR: {exc}",
            })

    _print_json(results)


def _aggregate_licenses(accounts):
    """Return (bundles, modules, settings) as sorted comma-joined strings from a list of account dicts."""
    bundles, modules, settings = set(), set(), set()
    for acct in accounts:
        lic = acct.get("licenses") or {}
        for b in lic.get("bundles") or []:
            bundles.add(b.get("displayName") or b.get("name") or "")
        for m in lic.get("modules") or []:
            modules.add(m.get("displayName") or m.get("name") or "")
        for s in lic.get("settings") or []:
            settings.add(s.get("settingGroupDisplayName") or s.get("groupName") or "")
    bundles.discard("")
    modules.discard("")
    settings.discard("")
    return (
        ", ".join(sorted(bundles)) or "—",
        ", ".join(sorted(modules)) or "—",
        ", ".join(sorted(settings)) or "—",
    )


def cmd_list_console_modules(args):
    from .api.config import Config

    if args.all:
        profiles = Config.list_profiles() or [None]
    elif args.profiles:
        profiles = [p.strip() for p in args.profiles.split(",")]
    else:
        profiles = [args.profile]

    results = []
    for profile in profiles:
        label = profile or "default"
        try:
            client = build_client(profile=profile)
            pages = client.paginate("accounts", params={"limit": 100})
            accounts = [item for page in pages for item in (page.get("data") or [])]
            bundles, modules, settings = _aggregate_licenses(accounts)
            results.append({
                "console": label,
                "accounts": len(accounts),
                "bundles": bundles,
                "modules": modules,
                "settings": settings,
            })
        except SentinelOneError as exc:
            results.append({
                "console": label,
                "accounts": None,
                "bundles": f"ERROR: {exc}",
                "modules": "—",
                "settings": "—",
            })

    _print_json(results)


def cmd_export_agent_passphrases(args):
    client = build_client(profile=args.profile)
    params = _parse_params(args.param)
    if args.account_id:
        params["accountIds"] = args.account_id
    if args.limit is not None:
        params["limit"] = args.limit
    if args.all:
        pages = client.paginate("agents/passphrases", params=params)
        rows = [item for page in pages for item in (page.get("data") or [])]
        _print_json({"data": rows, "count": len(rows)})
    else:
        data = client.request("GET", "agents/passphrases", params=params)
        _print_json(data)


def cmd_raw(args):
    client = build_client(profile=args.profile)
    params = _parse_params(args.param)
    payload = SentinelOneClient.load_json_payload(args.payload)
    data = client.request(args.method, args.path, params=params, payload=payload)
    _print_json(data)


_OS_DISPLAY = {
    "windows": "Windows",
    "linux": "Linux",
    "macos": "macOS",
    "windows_legacy": "Windows (Legacy)",
}


def _parse_version(v):
    """Parse a dotted-numeric version string into a comparable tuple."""
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except Exception:
        return (0,)


def cmd_latest_agent_versions(args):
    if getattr(args, "all_profiles", False):
        from .api.config import Config
        profiles = Config.list_profiles() or [None]
    elif getattr(args, "profiles", None):
        profiles = [p.strip() for p in args.profiles.split(",")]
    else:
        profiles = [args.profile]

    results = []
    for profile in profiles:
        label = profile or "default"
        try:
            client = build_client(profile=profile)
            params = {"packageTypes": "Agent", "limit": 200}
            if args.status:
                params["status"] = args.status
            if getattr(args, "account_id", None):
                params["accountIds"] = args.account_id

            pages = client.paginate("update/agent/packages", params=params)
            packages = [item for page in pages for item in (page.get("data") or [])]

            # Pick the highest version per OS.
            # Multi-layer guard to exclude non-agent packages (Chrome etc.) that
            # the API returns with packageType="Agent":
            #   1. packageType must be Agent or AgentAndRanger
            #   2. fileName must contain "sentinel"
            #   3. minorVersion must start with an uppercase letter (GA, SP1, SP2, EA1…)
            #      — Chrome uses "test" which fails this check
            #   4. scopeLevel must be "global"
            _agent_types = {"Agent", "AgentAndRanger"}
            os_keys = ["windows", "linux", "macos"]
            latest = {}
            for pkg in packages:
                os_type = pkg.get("osType") or ""
                filename = (pkg.get("fileName") or "").lower()
                minor = pkg.get("minorVersion") or ""
                scope = pkg.get("scopeLevel") or ""
                if (os_type in os_keys
                        and pkg.get("packageType") in _agent_types
                        and "sentinel" in filename
                        and minor and minor[0].isupper()
                        and scope == "global"):
                    ver = pkg.get("version") or ""
                    if os_type not in latest or _parse_version(ver) > _parse_version(latest[os_type].get("version") or ""):
                        latest[os_type] = pkg

            results.append({
                "console": label,
                "Windows": latest.get("windows", {}).get("version") or "—",
                "Linux": latest.get("linux", {}).get("version") or "—",
                "macOS": latest.get("macos", {}).get("version") or "—",
            })
        except SentinelOneError as exc:
            results.append({
                "console": label,
                "Windows": f"ERROR: {exc}",
                "Linux": "—",
                "macOS": "—",
            })

    _print_json(results)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="s1am",
        description="SentinelOne Account Management helper CLI",
    )
    parser.add_argument("--profile", default=None, help="Named profile from ~/.config/s1am/config")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-JSON output")
    sub = parser.add_subparsers(dest="command", required=True)

    list_accounts = sub.add_parser("list-accounts", help="List accounts")
    list_accounts.add_argument("--limit", type=int, default=None)
    list_accounts.add_argument("--cursor", type=str, default=None)
    list_accounts.add_argument("--param", action="append", help="Extra query params key=value")
    list_accounts.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_accounts.set_defaults(func=cmd_list_accounts)

    get_account = sub.add_parser("get-account", help="Get account by ID")
    get_account.add_argument("account_id")
    get_account.set_defaults(func=cmd_get_account)

    account_policy = sub.add_parser("account-policy", help="Get or update account policy")
    account_policy.add_argument("account_id")
    account_policy.add_argument("action", choices=["get", "update"])
    account_policy.add_argument("--payload", help="JSON payload for update")
    account_policy.set_defaults(func=cmd_account_policy)

    lookup_account_id = sub.add_parser("lookup-account-id", help="Find account ID by account name")
    lookup_account_id.add_argument("account_name", help="Account name to search for")
    lookup_account_id.add_argument("--exact", action="store_true", help="Require exact name match (default: contains)")
    lookup_account_id.set_defaults(func=cmd_lookup_account_id)

    list_console_mods = sub.add_parser(
        "list-console-modules",
        help="Show SKUs, modules, and settings per console (profile)",
    )
    list_console_mods.add_argument(
        "--all", action="store_true", help="Query every saved profile"
    )
    list_console_mods.add_argument(
        "--profiles", default=None,
        help="Comma-separated profile names, e.g. acme,contoso",
    )
    list_console_mods.set_defaults(func=cmd_list_console_modules)

    lookup_bulk = sub.add_parser(
        "lookup-accounts-bulk",
        help="Bulk lookup account IDs from a 2-column CSV (console_url, account_name)",
    )
    lookup_bulk.add_argument(
        "input_file",
        help="Path to CSV file, or - to read from stdin",
    )
    lookup_bulk.set_defaults(func=cmd_lookup_accounts_bulk)

    list_sites = sub.add_parser("list-sites", help="List sites")
    list_sites.add_argument("--limit", type=int, default=None)
    list_sites.add_argument("--cursor", type=str, default=None)
    list_sites.add_argument("--param", action="append", help="Extra query params key=value")
    list_sites.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_sites.set_defaults(func=cmd_list_sites)

    sites_data_ingest = sub.add_parser(
        "sites-with-data-ingest",
        help="List sites that have a Data Ingest add-on with any retention add-on selected",
    )
    sites_data_ingest.add_argument(
        "--account-id", dest="account_id", default=None,
        help="Filter by account ID",
    )
    sites_data_ingest.set_defaults(func=cmd_sites_with_data_ingest)

    list_agents = sub.add_parser("list-agents", help="List agents")
    list_agents.add_argument("--limit", type=int, default=None)
    list_agents.add_argument("--cursor", type=str, default=None)
    list_agents.add_argument("--param", action="append", help="Extra query params key=value")
    list_agents.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_agents.set_defaults(func=cmd_list_agents)

    export_passphrases = sub.add_parser(
        "export-agent-passphrases",
        help="Export agent passphrases for a given account via agents/passphrases",
    )
    export_passphrases.add_argument(
        "--account-id", dest="account_id", default=None,
        help="Account ID to filter by (accountIds param)",
    )
    export_passphrases.add_argument("--limit", type=int, default=None)
    export_passphrases.add_argument(
        "--all", action="store_true", help="Fetch all pages",
    )
    export_passphrases.add_argument(
        "--param", action="append", help="Extra query params key=value",
    )
    export_passphrases.set_defaults(func=cmd_export_agent_passphrases)

    latest_agent_vers = sub.add_parser(
        "latest-agent-versions",
        help="Show latest available agent package version per OS (Windows, Linux, macOS)",
    )
    latest_agent_vers.add_argument(
        "--account-id", dest="account_id", default=None,
        help="Filter by account ID",
    )
    latest_agent_vers.add_argument(
        "--status", default="ga",
        help="Package status filter: ga, beta, ea (default: ga)",
    )
    latest_agent_vers.add_argument(
        "--profiles", default=None,
        help="Comma-separated profile names for multi-console query",
    )
    latest_agent_vers.add_argument(
        "--all-profiles", dest="all_profiles", action="store_true",
        help="Query every saved profile",
    )
    latest_agent_vers.set_defaults(func=cmd_latest_agent_versions)

    raw = sub.add_parser("raw", help="Call any SentinelOne API endpoint")
    raw.add_argument("method", help="HTTP method, e.g. GET, POST")
    raw.add_argument("path", help="Relative path, e.g. sites or accounts/123")
    raw.add_argument("--param", action="append", help="Query param key=value")
    raw.add_argument("--payload", help="JSON payload string")
    raw.set_defaults(func=cmd_raw)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, SentinelOneError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
