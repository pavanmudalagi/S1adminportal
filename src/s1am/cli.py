import argparse
import json
import re
import sys

from .api.client import SentinelOneClient
from .api.config import Config
from .api.exceptions import SentinelOneError
from . import account_fields, licenses


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


# (osTypes filter value, column label) — order defines the output columns.
_AGENT_PLATFORMS = [
    ("windows", "Windows"),
    ("windows_legacy", "Windows Legacy"),
    ("macos", "macOS"),
    ("linux", "Linux"),
]


def _count_from_response(resp):
    """Extract the integer total from a GET /agents/count response."""
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        return int(data.get("total") or 0)
    return 0


def _agent_counts_by_platform(client, account_id=None):
    """
    Return {"Total": n, "Windows": n, ..., "Other": n} for one console.

    Uses GET /agents/count (one call for the total, one per osTypes value) so
    no agent records are paged through. "Other" is whatever the total holds
    beyond the known platforms (e.g. unknown OS), never negative.
    """
    base = {"accountIds": account_id} if account_id else {}
    total = _count_from_response(client.request("GET", "agents/count", params=base))
    counts = {"Total": total}
    for os_type, label in _AGENT_PLATFORMS:
        resp = client.request("GET", "agents/count", params={**base, "osTypes": os_type})
        counts[label] = _count_from_response(resp)
    counts["Other"] = max(total - sum(counts[label] for _, label in _AGENT_PLATFORMS), 0)
    return counts


def cmd_agent_count(args):
    if getattr(args, "all_profiles", False):
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
            row = {"console": label}
            row.update(_agent_counts_by_platform(client, account_id=args.account_id))
            results.append(row)
        except SentinelOneError as exc:
            results.append({"console": label, "Total": f"ERROR: {exc}"})

    _print_json(results)


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


# Matches Salesforce tenant names like 1000_USEA1_2498094131181849447
_TENANT_RE = re.compile(r"^\d+_[A-Za-z0-9]+_(\d+)$")


def _parse_tenant(s):
    """Parse '1000_USEA1_<id>' → (console_code, account_id), or return None."""
    parts = s.split("_", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
        return parts[1], parts[2]
    return None


def cmd_lookup_account_id(args):
    tenant = _parse_tenant(args.account_name)
    if tenant:
        console_code, account_id = tenant
        # Route to the profile whose name matches the console code in the tenant string.
        # --profile can override this (e.g. for testing against a different console).
        profile = args.profile if args.profile else console_code
        try:
            client = build_client(profile=profile)
        except Exception as exc:
            _print_json({
                "error": (
                    f"Console profile '{console_code}' not found. "
                    f"Add it via 'Add Console' with name '{console_code}'. Detail: {exc}"
                )
            })
            return
        pages = client.paginate("accounts", params={"ids": account_id, "limit": 10})
        accounts = [item for page in pages for item in (page.get("data") or [])]
        if not accounts:
            _print_json({"error": f"No account found with ID {account_id} on console '{profile}'"})
            return
        acct = accounts[0]
        lic = acct.get("licenses") or {}
        skus = sorted({b.get("displayName") or b.get("name") or "" for b in lic.get("bundles") or []} - {""})
        modules = sorted({m.get("displayName") or m.get("name") or "" for m in lic.get("modules") or []} - {""})
        _print_json({
            "tenant": args.account_name,
            "console": profile,
            "account_id": acct["id"],
            "account_name": acct.get("name"),
            "state": acct.get("state"),
            "skus": skus,
            "modules": modules,
        })
    else:
        # Existing behaviour: search by name (server-side contains, optional client-side exact)
        client = build_client(profile=args.profile)
        pages = client.paginate("accounts", params={"name": args.account_name, "limit": 100})
        accounts = [item for page in pages for item in (page.get("data") or [])]
        if args.exact:
            accounts = [a for a in accounts if a.get("name", "").lower() == args.account_name.lower()]
        if not accounts:
            print(json.dumps({"matches": [], "count": 0}, indent=2))
            return
        matches = [{"id": a["id"], "name": a.get("name"), "state": a.get("state")} for a in accounts]
        _print_json({"count": len(matches), "matches": matches})


def cmd_sf_check(args):
    """Verify the Salesforce connection: sign in if needed, run one tiny query, print org + user."""
    from .salesforce import auth as sf_auth
    from .salesforce import client as sf

    if args.logout:
        sf_auth.clear_token()
        _print_json({"signed_out": True, "token_file": str(sf_auth.token_file())})
        return

    status = sf_auth.status()
    if args.login or not status.connected:
        if not args.login and not sys.stdin.isatty():
            _print_json({"error": status.detail})
            sys.exit(1)
        import webbrowser

        def _open(url):
            # Print the URL as well: a headless shell or a blocked default browser can't open it.
            print(f"Opening the browser for Salesforce sign-in (waiting up to {args.timeout}s).\n"
                  f"If no tab appeared, open this URL yourself:\n{url}\n", file=sys.stderr)
            webbrowser.open(url)

        status = sf_auth.login_interactive(timeout=args.timeout, open_browser=_open)
        if not status.connected:
            _print_json({"error": status.detail})
            sys.exit(1)

    try:
        org = sf.run_soql("SELECT Id, Name, InstanceName, OrganizationType, IsSandbox FROM Organization",
                          use_cache=False)[0]
    except sf.SalesforceConfigError as exc:
        _print_json({"error": str(exc)})
        sys.exit(1)
    except Exception as exc:  # simple-salesforce raises its own hierarchy
        hint = ""
        if sf._looks_like_auth_error(exc):
            hint = (f"Cached session is no longer valid (refresh failed: {sf_auth.last_refresh_error or 'unknown'}). "
                    "Run `s1am sf-check --login` to sign in again.")
        _print_json({"error": str(exc).split(". Response content")[0], "hint": hint} if hint else {"error": str(exc)})
        sys.exit(1)
    me = sf.whoami()
    _print_json({
        "auth":         status.detail,
        "token_file":   str(sf_auth.token_file()),
        "instance_url": (sf_auth.load_token() or {}).get("instance_url"),
        "org":          {"id": org.get("Id"), "name": org.get("Name"), "instance": org.get("InstanceName"),
                         "type": org.get("OrganizationType"), "sandbox": org.get("IsSandbox")},
        "user":         me or {"id": status.identity},
    })


def _eap_ids(raw):
    return [int(x) for x in re.split(r"[,\s]+", raw) if x.strip()] if raw else None


def cmd_eap_sync(args):
    from .eap import service
    st = service.open_store()
    result = service.sync(st, path=args.path)
    _print_json(result)


def cmd_eap_enrich(args):
    from .eap import service
    st = service.open_store()
    _print_json(service.enrich_all(st, ids=_eap_ids(args.ids)))


def cmd_eap_check(args):
    from .eap import service
    st = service.open_store()
    rows = service.check_all(st, lambda code: build_client(profile=code), ids=_eap_ids(args.ids))
    _print_json([{k: r.get(k) for k in ("id", "company", "status", "status_reason", "s1_console", "s1_account_name",
                                         "s1_prompt_enabled", "s1_prompt_tenant", "s1_prompt_users", "s1_error")}
                 for r in rows])


def cmd_eap_seed(args):
    from .eap import service
    st = service.open_store()
    _print_json(service.seed_customer_list(st, args.path))


def _parse_names(raw):
    """Split a comma-or-newline-separated string into a list of non-empty, stripped names."""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]


def _parse_sku_specs(raw):
    """Parse 'NAME=COUNT' / 'NAME=unlimited' entries → list of (name, count_or_None)."""
    specs = []
    for item in _parse_names(raw):
        name, sep, value = item.partition("=")
        value = value.strip().lower()
        if not sep:
            specs.append((name.strip(), None))
        elif value in ("unlimited", "-1"):
            specs.append((name.strip(), -1))
        elif value.isdigit():
            specs.append((name.strip(), int(value)))
        else:
            raise ValueError(f"Invalid SKU count '{item}'. Use NAME=COUNT or NAME=unlimited.")
    return specs


def _apply_license_changes(lic, add_skus=None, remove_skus=None,
                           add_modules=None, remove_modules=None, dv_retention=None):
    """Apply requested changes to an account's current licenses.

    Returns (licenses_payload, changes, errors). Bundles, modules and settings that
    are not mentioned are sent back unchanged (including ones s1am does not show).
    """
    errors = []
    changes = {"skus_added": [], "skus_updated": [], "skus_removed": [],
               "modules_added": [], "modules_removed": [], "settings_changed": []}

    # ── Bundles/SKUs ──────────────────────────────────────────────────────────
    bundles = []
    for b in lic.get("bundles") or []:
        out = {"name": b.get("name"),
               "surfaces": [{"name": s.get("name"), "count": s.get("count", 0)}
                            for s in b.get("surfaces") or []]}
        if b.get("majorVersion") is not None:
            out["majorVersion"] = b["majorVersion"]
        bundles.append(out)

    for raw in _parse_names(remove_skus):
        sku = licenses.resolve_sku(raw)
        name = sku["name"] if sku else raw.lower()
        before = len(bundles)
        bundles = [b for b in bundles if (b["name"] or "").lower() != name]
        if len(bundles) < before:
            changes["skus_removed"].append(name)

    newly_added_skus = []
    for raw, count in _parse_sku_specs(add_skus):
        sku = licenses.resolve_sku(raw)
        if not sku:
            errors.append(f"Unknown SKU '{raw}'. Valid: " + ", ".join(s["name"] for s in licenses.SKUS))
            continue
        if count == -1 and not sku["unlimited"]:
            errors.append(f"{sku['displayName']} does not support an Unlimited {sku['surface']}")
            continue
        existing = next((b for b in bundles if (b["name"] or "").lower() == sku["name"]), None)
        if existing is None:
            if count is None:
                errors.append(f"New SKU '{sku['name']}' needs a count: {sku['name']}=COUNT or {sku['name']}=unlimited")
                continue
            bundles.append({"name": sku["name"], "surfaces": [{"name": sku["surface"], "count": count}]})
            changes["skus_added"].append(f"{sku['name']}={count}")
            newly_added_skus.append(sku["name"])
        elif count is not None:
            surf = next((s for s in existing["surfaces"] if s["name"] == sku["surface"]), None)
            if surf is None:
                existing["surfaces"].append({"name": sku["surface"], "count": count})
            else:
                surf["count"] = count
            changes["skus_updated"].append(f"{sku['name']}={count}")

    # ── Modules/Add-ons ───────────────────────────────────────────────────────
    modules = [{"name": m.get("name")} for m in lic.get("modules") or []]
    for raw in _parse_names(remove_modules):
        addon = licenses.resolve_addon(raw)
        name = addon["name"] if addon else raw.lower()
        before = len(modules)
        modules = [m for m in modules if (m["name"] or "").lower() != name]
        if len(modules) < before:
            changes["modules_removed"].append(name)

    for raw in _parse_names(add_modules):
        addon = licenses.resolve_addon(raw)
        if not addon:
            errors.append(f"Unknown add-on '{raw}'. Valid: " + ", ".join(a["name"] for a in licenses.ADDONS))
            continue
        if not any((m["name"] or "").lower() == addon["name"] for m in modules):
            modules.append({"name": addon["name"]})
            changes["modules_added"].append(addon["name"])

    # Add-ons that come on by default with a newly added SKU (e.g. Complete → Purple AI
    # Foundations + Unprotected Endpoints Discovery), unless explicitly removed in this call.
    removed = {licenses.resolve_addon(r)["name"] if licenses.resolve_addon(r) else r.lower()
               for r in _parse_names(remove_modules)}
    with_defaults = licenses.add_default_modules(newly_added_skus, modules)
    for m in with_defaults[len(modules):]:
        if m["name"] not in removed:
            modules.append(m)
            changes["modules_added"].append(m["name"])

    # ── Settings: only Deep Visibility Data Retention is editable ─────────────
    settings = [{"groupName": s.get("groupName") or s.get("settingGroup"), "setting": s.get("setting")}
                for s in lic.get("settings") or []]
    if dv_retention:
        current = next((s for s in settings if s["groupName"] == licenses.DV_RETENTION_GROUP), None)
        if current is None:
            settings.append({"groupName": licenses.DV_RETENTION_GROUP, "setting": dv_retention})
        else:
            current["setting"] = dv_retention
        changes["settings_changed"].append(f"{licenses.DV_RETENTION_GROUP}={dv_retention}")

    # Drop settings tied to a SKU that is no longer present (e.g. ranger_ad_mode without Identity Security)
    pruned = licenses.prune_settings(bundles, settings)
    changes["settings_removed"] = [s["groupName"] for s in settings if s not in pruned]
    # ...and add default settings for newly present SKUs (e.g. ranger_ad_mode=Full with Identity Security)
    settings = licenses.add_default_settings(bundles, pruned)
    changes["settings_added"] = [f"{s['groupName']}={s['setting']}" for s in settings if s not in pruned]

    errors.extend(licenses.validate_licenses(bundles, modules))
    return {"bundles": bundles, "modules": modules, "settings": settings}, changes, errors


def cmd_create_account(args):
    """POST /accounts from a JSON payload (the `data` object), validated like the web form."""
    raw = args.payload
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as fh:
            raw = fh.read()
    body = json.loads(raw)
    lic = body.pop("licenses", None) or {}
    for key in ("bundles", "modules", "settings"):
        body.setdefault(key, lic.get(key) or [])
    client = build_client(profile=args.profile)
    data, errors = account_fields.build_create_payload(body, base_url=client.base_url)
    if errors:
        _print_json({"error": "Not created", "errors": errors})
        sys.exit(2)

    existing = client.request("GET", "accounts", params={"name": data["name"], "limit": 10})
    rows = (existing.get("data") or []) if isinstance(existing, dict) else []
    if any((a.get("name") or "").strip().lower() == data["name"].lower() for a in rows):
        _print_json({"error": f"An account named '{data['name']}' already exists"})
        sys.exit(2)
    if args.dry_run:
        _print_json({"dry_run": True, "payload": {"data": data}})
        return
    _print_json(client.request("POST", "accounts", payload={"data": data}))


def cmd_update_account(args):
    tenant = _parse_tenant(args.tenant)
    if not tenant:
        _print_json({"error": "Invalid tenant format. Expected: 1000_CODE_<account_id>"})
        return

    console_code, account_id = tenant
    profile = args.profile if args.profile else console_code
    try:
        client = build_client(profile=profile)
    except Exception as exc:
        _print_json({
            "error": (
                f"Console profile '{console_code}' not found. "
                f"Add it via 'Add Console' with name '{console_code}'. Detail: {exc}"
            )
        })
        return

    # Fetch current account to preserve fields we are not touching
    pages = client.paginate("accounts", params={"ids": account_id, "limit": 10})
    accounts = [item for page in pages for item in (page.get("data") or [])]
    if not accounts:
        _print_json({"error": f"No account found with ID {account_id} on console '{profile}'"})
        return

    acct = accounts[0]
    try:
        new_licenses, changes, errors = _apply_license_changes(
            acct.get("licenses") or {},
            add_skus=args.add_skus,
            remove_skus=args.remove_skus,
            add_modules=args.add_modules,
            remove_modules=args.remove_modules,
            dv_retention=args.dv_retention,
        )
    except ValueError as exc:
        errors, changes = [str(exc)], {}
    if errors:
        _print_json({"error": "Update not sent: " + "; ".join(errors), "errors": errors})
        return

    data_payload = {"licenses": new_licenses}
    if args.name:
        data_payload["name"] = args.name

    if args.dry_run:
        _print_json({"dry_run": True, "account_id": account_id, "console": profile,
                     "changes": changes, "payload": {"data": data_payload}})
        return

    client.request("PUT", f"accounts/{account_id}", payload={"data": data_payload})

    # Re-fetch to confirm updated state
    pages2 = client.paginate("accounts", params={"ids": account_id, "limit": 10})
    accounts2 = [item for page in pages2 for item in (page.get("data") or [])]
    updated = accounts2[0] if accounts2 else acct
    lic2 = updated.get("licenses") or {}
    skus    = sorted({b.get("displayName") or b.get("name") or "" for b in lic2.get("bundles") or []} - {""})
    modules = sorted({m.get("displayName") or m.get("name") or "" for m in lic2.get("modules") or []} - {""})

    _print_json({
        "tenant":        args.tenant,
        "console":       profile,
        "account_id":    updated["id"],
        "account_name":  updated.get("name"),
        "state":         updated.get("state"),
        "changes": changes,
        "skus":    skus,
        "modules": modules,
    })


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
    lookup_account_id.add_argument("account_name", help="Account name or Salesforce tenant (1000_CODE_<id>)")
    lookup_account_id.add_argument("--exact", action="store_true", help="Require exact name match (default: contains)")
    lookup_account_id.set_defaults(func=cmd_lookup_account_id)

    create_account = sub.add_parser(
        "create-account",
        help="Create an account from a JSON payload (POST /accounts)",
    )
    create_account.add_argument(
        "--payload", required=True,
        help=("JSON for the account `data` object, or @file. Keys: name, accountType, usageType, "
              "billingMode, externalId, expiration|unlimitedExpiration, bundles, modules, settings, "
              "promptFields"),
    )
    create_account.add_argument("--dry-run", action="store_true", help="Validate and print the payload only")
    create_account.set_defaults(func=cmd_create_account)

    update_account = sub.add_parser(
        "update-account",
        help="Update account name, modules, and/or SKUs by Salesforce tenant",
    )
    update_account.add_argument("tenant", help="Salesforce tenant, e.g. 1000_USEA1_2498094131181849447")
    update_account.add_argument("--name", default=None, help="New account display name")
    update_account.add_argument(
        "--add-skus", default=None, metavar="SKU=COUNT,SKU=unlimited",
        help=(
            "Comma-separated SKUs to add, or to change the count of existing ones "
            "(e.g. unified_identity=500,hyperautomation=unlimited). "
            "Valid: " + ", ".join(s["name"] for s in licenses.SKUS)
        ),
    )
    update_account.add_argument(
        "--remove-skus", default=None, metavar="SKU1,SKU2",
        help="Comma-separated SKU names to remove",
    )
    update_account.add_argument(
        "--add-modules", default=None, metavar="MOD1,MOD2",
        help=(
            "Comma-separated add-on names to add (preserves existing). "
            "Valid: " + ", ".join(a["name"] for a in licenses.ADDONS)
        ),
    )
    update_account.add_argument(
        "--remove-modules", default=None, metavar="MOD1,MOD2",
        help="Comma-separated add-on names to remove",
    )
    update_account.add_argument(
        "--dv-retention", default=None, choices=licenses.DV_RETENTION_OPTIONS,
        help="Deep Visibility Data Retention setting",
    )
    update_account.add_argument(
        "--dry-run", action="store_true",
        help="Validate and print the payload without sending the update",
    )
    update_account.set_defaults(func=cmd_update_account)

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

    agent_count = sub.add_parser(
        "agent-count",
        help="Count installed agents per console, broken down by platform",
    )
    agent_count.add_argument(
        "--account-id", dest="account_id", default=None,
        help="Only count agents in this account",
    )
    agent_count.add_argument(
        "--profiles", default=None,
        help="Comma-separated profile names for multi-console query",
    )
    agent_count.add_argument(
        "--all-profiles", dest="all_profiles", action="store_true",
        help="Query every saved profile",
    )
    agent_count.set_defaults(func=cmd_agent_count)

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

    sf_check = sub.add_parser(
        "sf-check",
        help="Verify the Salesforce connection (browser SSO) and print the org and user",
    )
    sf_check.add_argument("--login", action="store_true",
                          help="Force a fresh browser sign-in even if a session is cached")
    sf_check.add_argument("--logout", action="store_true",
                          help="Delete the cached Salesforce tokens and exit")
    sf_check.add_argument("--timeout", type=int, default=180,
                          help="Seconds to wait for the browser redirect (default: 180)")
    sf_check.set_defaults(func=cmd_sf_check)

    eap_sync = sub.add_parser("eap-sync", help="Import Prompt EAP requests from the Microsoft Forms workbook")
    eap_sync.add_argument("--path", default=None, help="Workbook path (default: [eap] form_path / synced copy)")
    eap_sync.set_defaults(func=cmd_eap_sync)

    eap_enrich = sub.add_parser("eap-enrich", help="Match EAP requests to Salesforce accounts and tenants")
    eap_enrich.add_argument("--ids", default=None, help="Comma-separated request IDs (default: all)")
    eap_enrich.set_defaults(func=cmd_eap_enrich)

    eap_check = sub.add_parser("eap-check", help="Cross-check EAP requests on their SentinelOne consoles (read-only)")
    eap_check.add_argument("--ids", default=None, help="Comma-separated request IDs (default: all)")
    eap_check.set_defaults(func=cmd_eap_check)

    eap_seed = sub.add_parser("eap-seed", help="One-time import of notes/enabled columns from 'EAP - Customer List.xlsx'")
    eap_seed.add_argument("path", help="Path to the Customer List workbook (Sheet1, '#' = form Id)")
    eap_seed.set_defaults(func=cmd_eap_seed)

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
