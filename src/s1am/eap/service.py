"""Orchestration for the EAP tracker, shared by the Flask endpoints and the CLI: settings,
sync from the Forms workbook, Salesforce enrichment, console checks, Customer List seed.

Settings — an environment variable S1AM_EAP_<KEY> wins over the [eap] section of
~/.config/s1am/config, which wins over the default:

  form_path      path to the Forms workbook. Default: the copy synced to OneDrive, else the
                 newest "…Early Access…xlsx" in ~/Downloads
  db_path        SQLite file, default ~/.config/s1am/eap.db
  default_count  seat count for Prompt bundles, default -1 (unlimited)
  actioned_by    recorded on writes; default is the signed-in Salesforce user
  dry_run        "true" (default) — the enable path only shows the PUT body
"""
import datetime
import os
from pathlib import Path

from .. import account_fields
from ..api.config import Config
from . import enrich as enrich_mod
from . import forms, s1check
from .store import DEFAULT_DB, Store

DEFAULTS = {
    "form_path": "",
    "db_path": str(DEFAULT_DB),
    "default_count": "-1",
    "actioned_by": "",
    "dry_run": "true",
}
SYNCED_COPY = (Path.home() / "Library" / "CloudStorage" / "OneDrive-N-Able,Inc"
               / "N-able Endpoint Security_SentinelOne _Early Access.xlsx")
DOWNLOADS_GLOB = "*Early Access*.xlsx"


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def setting(key):
    value = os.getenv("S1AM_EAP_" + key.upper())
    if value is None:
        value = Config.section("eap").get(key)
    if value is None:
        value = DEFAULTS.get(key, "")
    return value.strip() if isinstance(value, str) else value


def form_path():
    """The workbook to read, or None if nothing is configured and nothing was found."""
    configured = setting("form_path")
    if configured:
        return Path(os.path.expanduser(configured))
    if SYNCED_COPY.exists():
        return SYNCED_COPY
    found = sorted((Path.home() / "Downloads").glob(DOWNLOADS_GLOB), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def open_store():
    return Store(os.path.expanduser(setting("db_path")))


def profiles():
    """{profile code: base URL} for every console profile (sections with s1_base_url)."""
    out = {}
    for code in Config.list_profiles() or []:
        base = Config.section(code).get("s1_base_url")
        if base:
            out[code] = base
    return out


def host_codes():
    return s1check.host_codes(profiles())


def _restatus(st, rid):
    status, reason = enrich_mod.derive_status(st.get(rid))
    st.update(rid, {"status": status, "status_reason": reason})


# ── sync ────────────────────────────────────────────────────────────────────

def sync(st, path=None):
    """Read the workbook and upsert every response. Idempotent; never touches user edits."""
    path = Path(path) if path else form_path()
    if not path or not path.exists():
        raise FileNotFoundError("Forms workbook not found — set [eap] form_path in ~/.config/s1am/config")
    result = {"path": str(path), "new": [], "updated": [], "unchanged": 0, "rows": 0}
    for rec in forms.read_workbook(path):
        products = forms.normalize_products(rec.get("products_raw"))
        admin = forms.parse_super_admin(rec.get("super_admin_raw"), rec.get("contact_name"), rec.get("contact_email"))
        details = forms.parse_details(rec.get("details_raw"))
        rec.update(
            products=products, wants_prompt=int("Prompt Security" in products),
            super_admin_name=admin["name"], super_admin_email=admin["email"], super_admin_source=admin["source"],
            super_admin_needs_review=int(admin["needs_review"]), super_admin_note=admin["note"],
            details=details, no_s1_claimed=int(details["no_s1_claimed"]),
        )
        outcome = st.upsert_form_row(rec)
        # Defaults for the provisioning fields; skipped automatically once the user edits them.
        defaults = {"prompt_tenant_name": forms.suggest_tenant_name(rec.get("company"))}
        if admin["email"]:
            defaults["first_users"] = [{"name": admin["name"] or "", "email": admin["email"]}] + [
                {"name": a["promptUserName"], "email": a["promptUserEmail"]}
                for a in account_fields.PROMPT_STANDARD_ADMINS if a["promptUserEmail"].lower() != admin["email"].lower()]
        st.update(rec["id"], defaults)
        _restatus(st, rec["id"])
        result["rows"] += 1
        if outcome == "unchanged":
            result["unchanged"] += 1
        else:
            result[outcome].append(rec["id"])
            st.add_history(rec["id"], "sync", "Imported from the Forms workbook" if outcome == "new"
                           else "Form response changed in the workbook")
    st.set_meta("last_sync", _now())
    st.set_meta("form_path", str(path))
    return result


# ── Salesforce ──────────────────────────────────────────────────────────────

def enrich_all(st, ids=None):
    from ..salesforce import eap_queries as q
    rows = [r for r in st.list() if not ids or r["id"] in set(ids)]
    now = _now()
    results = enrich_mod.enrich(rows, q, host_codes=host_codes(), sf_as_of=now)
    for rid, fields in results.items():
        st.update(rid, fields)
        _restatus(st, rid)
        st.add_history(rid, "enrich", fields.get("match_note") or "no Salesforce match")
    st.set_meta("sf_as_of", now)
    return {"enriched": len(results), "matched": sum(1 for f in results.values() if f.get("sf_account_id")),
            "as_of": now}


# ── SentinelOne console ─────────────────────────────────────────────────────

def check_all(st, client_for, ids=None, progress=None):
    """Cross-check each request on its console. `progress(row)` is called after every row."""
    rows = [r for r in st.list() if not ids or r["id"] in set(ids)]
    codes, hc = list(profiles()), host_codes()
    out = []
    for r in rows:
        fields = s1check.check_request(r, client_for, hc, all_codes=codes)
        st.update(r["id"], fields)
        _restatus(st, r["id"])
        if fields.get("s1_error"):
            message = f"Console check: {fields['s1_error']}"
        elif fields.get("s1_prompt_enabled"):
            message = (f"Prompt is enabled on {fields['s1_console']} account {fields['s1_account_name']} "
                       f"(tenant {fields.get('s1_prompt_tenant') or '?'})")
        else:
            message = f"No Prompt on {fields['s1_console']} account {fields['s1_account_name']}"
        st.add_history(r["id"], "s1-check", message)
        row = st.get(r["id"])
        out.append(row)
        if progress:
            progress(row)
    st.set_meta("s1_as_of", _now())
    return out


# ── one-time seed from "EAP - Customer List.xlsx" ───────────────────────────

SEED_COLUMNS = {
    "Notes": "notes", "First contacted": "first_contacted", "Enabled (Y/N)": "enabled_yn",
    "Date Enabled": "date_enabled", "Enabled By": "enabled_by",
}


def seed_customer_list(st, path, sheet="Sheet1"):
    """Carry over the hand-kept columns (as user edits) from the old tracking sheet; "#" = form Id."""
    import openpyxl
    ws = openpyxl.load_workbook(str(path), data_only=True)[sheet]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    headers = [forms.normalize_header(h) for h in rows[0]]
    if "#" not in headers:
        raise ValueError(f"No '#' column on {sheet} of {path}")
    idx = {field: headers.index(col) for col, field in SEED_COLUMNS.items() if col in headers}
    result = {"seeded": [], "missing": [], "empty": 0}
    for r in rows[1:]:
        try:
            rid = int(float(r[headers.index("#")]))
        except (TypeError, ValueError):
            continue
        fields = {}
        for field, i in idx.items():
            value = r[i] if i < len(r) else None
            if value is None:
                continue
            fields[field] = value.strftime("%Y-%m-%d") if isinstance(value, datetime.datetime) else str(value).strip()
        if not fields:
            result["empty"] += 1
            continue
        if st.get(rid) is None:
            result["missing"].append(rid)
            continue
        st.update(rid, fields, source="user")
        st.add_history(rid, "seed", f"Imported from EAP - Customer List: {', '.join(sorted(fields))}")
        result["seeded"].append(rid)
    return result
