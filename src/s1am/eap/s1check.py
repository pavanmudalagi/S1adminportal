"""Cross-check a request against the SentinelOne console (read-only, GET only): does the
account already carry a Prompt bundle, and if so which tenant name and first users.

The target account comes, in order, from: the console/account the user chose on the row,
the selected Salesforce tenant (1000_<CODE>_<id>), a tenant on the form, a form console host
plus account ID, a form console host plus an account name/GUID (looked up on that console),
or a bare account ID (tried on every saved console). Console hosts map to profiles by exact
hostname: the profile names in ~/.config/s1am/config are the tenant console codes.
"""
import datetime

from .. import licenses
from .enrich import tenant_account_id, tenant_code


def host_codes(profiles):
    """{console hostname: profile code} from {code: base_url}."""
    out = {}
    for code, base in profiles.items():
        host = (base or "").replace("https://", "").replace("http://", "").split("/")[0].strip().lower()
        if host:
            out[host] = code
    return out


def resolve_target(r, hosts_to_code):
    """→ {"console", "account_id", "source", "name_hint"} or {"error": reason}."""
    d = r.get("details") or {}
    if r.get("chosen_console") and r.get("chosen_account_id"):
        return {"console": r["chosen_console"], "account_id": str(r["chosen_account_id"]), "source": "chosen"}
    for name, source in ((r.get("sf_tenant"), "sf-tenant"), *((t, "form-tenant") for t in d.get("tenants") or [])):
        code, aid = tenant_code(name), tenant_account_id(name)
        if code and aid:
            return {"console": code, "account_id": aid, "source": source}
    hosts = d.get("hosts") or []
    console = next((hosts_to_code[h] for h in hosts if h in hosts_to_code), None)
    if console and d.get("account_ids"):
        return {"console": console, "account_id": d["account_ids"][0], "source": "form-id"}
    hints = [*(d.get("s1_names") or []), *(d.get("guids") or []), *([d["leftover"]] if d.get("leftover") else [])]
    if console and hints:
        return {"console": console, "account_id": None, "source": "console-lookup", "name_hint": hints[0], "name_hints": hints}
    if d.get("account_ids"):
        return {"console": None, "account_id": d["account_ids"][0], "source": "id-scan"}
    unknown = [h for h in hosts if h not in hosts_to_code]
    if unknown:
        return {"error": f"console {unknown[0]} is not a saved profile"}
    return {"error": "nothing on the form or in Salesforce identifies a console account"}


def prompt_info(account):
    """Prompt state of a GET /accounts/{id} payload."""
    lic = account.get("licenses") or {}
    bundles = [b.get("name") for b in lic.get("bundles") or [] if licenses.is_prompt_bundle(b.get("name"))]
    pf = account.get("promptFields") or {}
    users = pf.get("promptUsers") or (
        [{"promptUserName": pf.get("promptUserName"), "promptUserEmail": pf.get("promptUserEmail")}]
        if pf.get("promptUserName") or pf.get("promptUserEmail") else [])
    return {
        "enabled": bool(bundles) or bool(pf.get("promptTenantName")),
        "bundles": bundles,
        "tenant": pf.get("promptTenantName"),
        "users": [{"name": u.get("promptUserName"), "email": u.get("promptUserEmail")} for u in users],
        "region": pf.get("promptRegion"),
    }


def _get_account(client, account_id):
    """Single-account view first (it carries promptFields); list view as a fallback."""
    try:
        res = client.request("GET", f"accounts/{account_id}")
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, dict) and data.get("id"):
            return data
    except Exception:
        pass
    res = client.request("GET", "accounts", params={"ids": account_id, "limit": 10})
    accounts = (res.get("data") or []) if isinstance(res, dict) else []
    return accounts[0] if accounts else None


def _blank(now):
    return {"s1_checked_at": now, "s1_error": None, "s1_console": None, "s1_account_id": None,
            "s1_account_name": None, "s1_state": None, "s1_skus": [], "s1_prompt_enabled": None,
            "s1_prompt_bundles": [], "s1_prompt_tenant": None, "s1_prompt_users": [], "s1_prompt_region": None,
            "s1_target_source": None}


def check_request(r, client_for, hosts_to_code, all_codes=()):
    """→ the s1_* fields for one request. `client_for(code)` builds a client for a profile.
    Never raises: failures land in s1_error."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = _blank(now)
    tgt = resolve_target(r, hosts_to_code)
    if "error" in tgt:
        out["s1_error"] = tgt["error"]
        return out
    out["s1_target_source"] = tgt["source"]
    out["s1_console"] = tgt.get("console")
    console, account = tgt.get("console"), None
    try:
        if tgt["source"] == "console-lookup":
            client, found, hint = client_for(console), [], tgt["name_hint"]
            for hint in tgt.get("name_hints") or [tgt["name_hint"]]:
                res = client.request("GET", "accounts", params={"name": hint[:100], "limit": 50})
                found = (res.get("data") or []) if isinstance(res, dict) else []
                if found:
                    break
            guid = next(iter((r.get("details") or {}).get("guids") or []), None)
            if guid and len(found) > 1:
                found = [a for a in found if guid in (a.get("name") or "").lower()] or found
            if len(found) != 1:
                out["s1_error"] = f"{len(found)} accounts match '{hint}' on {console}"
                return out
            account = found[0]
        elif tgt["source"] == "id-scan":
            for code in all_codes:
                try:
                    account = _get_account(client_for(code), tgt["account_id"])
                except Exception:
                    account = None
                if account:
                    console = code
                    break
            if not account:
                out["s1_error"] = f"account {tgt['account_id']} not found on any saved console"
                return out
        else:
            account = _get_account(client_for(console), tgt["account_id"])
            if not account:
                out["s1_error"] = f"account {tgt['account_id']} not found on {console}"
                return out
    except Exception as exc:
        out["s1_error"] = str(exc)[:300]
        return out

    info = prompt_info(account)
    lic = account.get("licenses") or {}
    out.update(
        s1_console=console, s1_account_id=str(account.get("id")), s1_account_name=account.get("name"),
        s1_state=account.get("state"),
        s1_skus=sorted({b.get("displayName") or b.get("name") for b in lic.get("bundles") or [] if b.get("name")}),
        s1_prompt_enabled=int(info["enabled"]), s1_prompt_bundles=info["bundles"], s1_prompt_tenant=info["tenant"],
        s1_prompt_users=info["users"], s1_prompt_region=info["region"],
    )
    return out
