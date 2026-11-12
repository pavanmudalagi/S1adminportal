"""Cross-check a request against the SentinelOne console (read-only, GET only): does the
account already carry a Prompt bundle, and if so which tenant name and first users.

The target account comes, in order, from: the console/account the user chose on the row;
what the form says — a 1000_ tenant, a console host plus account ID, or a console host plus
an account name/GUID (looked up on that console); the selected Salesforce tenant
(1000_<CODE>_<id>); or a bare account ID tried on every saved console. The form wins over
Salesforce because it is the customer naming their own account; if a form lookup finds
nothing, the next source is tried. Console hosts map to profiles by exact hostname: the
profile names in ~/.config/s1am/config are the tenant console codes.
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


def resolve_targets(r, hosts_to_code):
    """Candidate targets in precedence order: [{"console", "account_id", "source", …}], possibly empty."""
    d = r.get("details") or {}
    out = []
    if r.get("chosen_console") and r.get("chosen_account_id"):
        out.append({"console": r["chosen_console"], "account_id": str(r["chosen_account_id"]), "source": "chosen"})
    for t in d.get("tenants") or []:
        code, aid = tenant_code(t), tenant_account_id(t)
        if code and aid:
            out.append({"console": code, "account_id": aid, "source": "form-tenant"})
    hosts = d.get("hosts") or []
    console = next((hosts_to_code[h] for h in hosts if h in hosts_to_code), None)
    if console and d.get("account_ids"):
        out.append({"console": console, "account_id": d["account_ids"][0], "source": "form-id"})
    hints = [*(d.get("s1_names") or []), *(d.get("guids") or []), *([d["leftover"]] if d.get("leftover") else [])]
    if console and hints:
        out.append({"console": console, "account_id": None, "source": "console-lookup", "name_hint": hints[0], "name_hints": hints})
    code, aid = tenant_code(r.get("sf_tenant")), tenant_account_id(r.get("sf_tenant"))
    if code and aid:
        out.append({"console": code, "account_id": aid, "source": "sf-tenant"})
    if d.get("account_ids") and not console:
        out.append({"console": None, "account_id": d["account_ids"][0], "source": "id-scan"})
    return out


def resolve_target(r, hosts_to_code):
    """The first candidate target, or {"error": reason}."""
    targets = resolve_targets(r, hosts_to_code)
    if targets:
        return targets[0]
    unknown = [h for h in (r.get("details") or {}).get("hosts") or [] if h not in hosts_to_code]
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


def _try_target(tgt, r, client_for, all_codes):
    """→ (account | None, console, error text | None) for one candidate target."""
    console = tgt.get("console")
    if tgt["source"] == "console-lookup":
        client, found, hint = client_for(console), [], tgt["name_hint"]
        for hint in tgt.get("name_hints") or [tgt["name_hint"]]:
            # `name` is an exact match on the console; `name__contains` is the free-text filter.
            res = client.request("GET", "accounts", params={"name__contains": hint[:100], "limit": 50})
            found = (res.get("data") or []) if isinstance(res, dict) else []
            if found:
                break
        guid = next(iter((r.get("details") or {}).get("guids") or []), None)
        if guid and len(found) > 1:
            found = [a for a in found if guid in (a.get("name") or "").lower()] or found
        if len(found) != 1:
            return None, console, f"{len(found)} accounts match '{hint}' on {console}"
        return found[0], console, None
    if tgt["source"] == "id-scan":
        for code in all_codes:
            try:
                account = _get_account(client_for(code), tgt["account_id"])
            except Exception:
                account = None
            if account:
                return account, code, None
        return None, None, f"account {tgt['account_id']} not found on any saved console"
    account = _get_account(client_for(console), tgt["account_id"])
    if not account:
        return None, console, f"account {tgt['account_id']} not found on {console}"
    return account, console, None


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
    targets = resolve_targets(r, hosts_to_code)
    if not targets:
        out["s1_error"] = resolve_target(r, hosts_to_code)["error"]
        return out
    account, console, errors = None, None, []
    for tgt in targets:
        try:
            account, console, err = _try_target(tgt, r, client_for, all_codes)
        except Exception as exc:
            account, console, err = None, tgt.get("console"), str(exc)[:300]
            if "401" in err or "unauthor" in err.lower():      # a bad console token: stop, don't mask it
                out.update(s1_target_source=tgt["source"], s1_console=console, s1_error=err)
                return out
        if account:
            out["s1_target_source"] = tgt["source"]
            break
        errors.append(err)
    if not account:
        out["s1_target_source"] = targets[0]["source"]
        out["s1_console"] = targets[0].get("console")
        out["s1_error"] = "; ".join(e for e in errors if e)
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
