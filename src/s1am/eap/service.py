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


def actor():
    """Who is acting: [eap] actioned_by, else the cached Salesforce identity, else the OS user."""
    who = setting("actioned_by")
    if who:
        return who
    try:
        from ..salesforce import auth as sf_auth
        token = sf_auth.load_token() or {}
        if token.get("id"):
            return "sf:" + sf_auth._identity_from(token["id"])
    except Exception:
        pass
    return os.getenv("USER") or "unknown"


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


# ── export ──────────────────────────────────────────────────────────────────

EXPORT_COLUMNS = [
    "#", "Contact Name", "Company (Form)", "Contact Email", "Super Admin", "SF Account Name", "Customer ID",
    "Country", "1000_ Tenant (Contract)", "Account name", "S1 Console/Account (from form)", "SKU Requested 1",
    "SKU Requested 2", "Wayfinder MDR Essential", "Hyperautomation", "First contacted", "Enabled (Y/N)",
    "Date Enabled", "Enabled By", "Notes", "Complete Agents", "Control Agents", "Total Agents (Complete + Control)",
]
TRACKER_COLUMNS = [
    ("#", "id"), ("Submitted", "completion_time"), ("Company", "company"), ("Contact", "contact_name"),
    ("Contact Email", "contact_email"), ("Super Admin", "super_admin_name"), ("Super Admin Email", "super_admin_email"),
    ("Requested", "products"), ("SF Account", "sf_account_name"), ("Customer ID", "customer_id"), ("Country", "country"),
    ("Tenant", "sf_tenant"), ("Tenant state", "_tenant_state"), ("Match", "match_confidence"), ("Match note", "match_note"),
    ("Status", "status"), ("Status reason", "status_reason"), ("Console", "s1_console"), ("S1 Account", "s1_account_name"),
    ("S1 Account ID", "s1_account_id"), ("Prompt on console", "_prompt"), ("Prompt tenant", "s1_prompt_tenant"),
    ("Prompt admins", "_admins"), ("Prompt SKUs", "s1_prompt_bundles"), ("Checked", "s1_checked_at"),
    ("Complete agents", "complete_agents"), ("Control agents", "control_agents"), ("Total agents", "total_agents"),
    ("Enabled (Y/N)", "_enabled"), ("Date Enabled", "date_enabled"), ("Enabled By", "enabled_by"),
    ("First contacted", "first_contacted"), ("Notes", "notes"), ("Joe status", "joe_status"), ("Joe ref", "joe_ref"),
]


def _enabled_yn(r):
    """Hand-kept Enabled (Y/N) wins; otherwise what the console says."""
    if r.get("enabled_yn"):
        return r["enabled_yn"]
    if r.get("s1_prompt_enabled") == 1:
        return "Y"
    if r.get("s1_prompt_enabled") == 0:
        return "N"
    return ""


def _tenant_state(r):
    if r.get("chosen_console") and r.get("chosen_account_id"):
        return "manual target"
    if r.get("tenant_confirmed"):
        return "confirmed by form" if str(r.get("tenant_confirmed_by") or "").startswith("form") else "confirmed"
    return "auto-selected" if r.get("sf_tenant") else ""


def export_rows(rows):
    """Rows in the "EAP - Customer List" Sheet1 layout (one list per row, EXPORT_COLUMNS order)."""
    out = []
    for r in rows:
        products = r.get("products") or []
        skus = [p for p in products if p in ("Prompt Security", "Identity Security")]
        d = r.get("details") or {}
        from_form = " ".join(x for x in [*(d.get("hosts") or []), *(d.get("account_ids") or []),
                                         *(d.get("tenants") or []), *(d.get("s1_names") or []), d.get("leftover") or ""] if x)
        out.append([
            r.get("id"), r.get("contact_name"), r.get("company"), r.get("contact_email"),
            " ".join(x for x in (r.get("super_admin_name"), r.get("super_admin_email")) if x),
            r.get("sf_account_name"), r.get("customer_id"), r.get("country"), r.get("sf_tenant"),
            r.get("s1_account_name"), from_form, skus[0] if skus else None, skus[1] if len(skus) > 1 else None,
            "Wayfinder MDR Essential" if "Wayfinder MDR Essentials" in products else None,
            "Hyperautomation" if "Hyperautomation" in products else None,
            r.get("first_contacted"), _enabled_yn(r), r.get("date_enabled"), r.get("enabled_by"), r.get("notes"),
            r.get("complete_agents"), r.get("control_agents"), r.get("total_agents"),
        ])
    return out


def summary(rows):
    """Headline numbers for sharing: how many asked, how many are enabled, and where the rest stand."""
    asked = [r for r in rows if r.get("wants_prompt")]
    enabled = [r for r in asked if _enabled_yn(r) == "Y"]
    by_status, by_console = {}, {}
    for r in asked:
        by_status[r.get("status") or "new"] = by_status.get(r.get("status") or "new", 0) + 1
    for r in enabled:
        by_console[r.get("s1_console") or "?"] = by_console.get(r.get("s1_console") or "?", 0) + 1
    return {"requests": len(rows), "asked_for_prompt": len(asked), "enabled": len(enabled),
            "pending": len(asked) - len(enabled) - by_status.get("not_eligible", 0),
            "not_eligible": by_status.get("not_eligible", 0), "by_status": by_status, "enabled_by_console": by_console}


def export_workbook(st, path):
    """Write a new .xlsx: Summary, Customer List (the old sheet's layout) and the full Tracker table."""
    import openpyxl
    from openpyxl.styles import Font
    rows = st.list()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = summary(rows)
    ws.append(["Prompt Security EAP — status", _now()])
    ws.append([])
    for label, value in (("Form responses", s["requests"]), ("Asked for Prompt Security", s["asked_for_prompt"]),
                         ("Prompt enabled", s["enabled"]), ("Still pending", s["pending"]),
                         ("Not eligible / no SentinelOne", s["not_eligible"])):
        ws.append([label, value])
    ws.append([])
    ws.append(["Status (Prompt requests)", "Count"])
    for k, v in sorted(s["by_status"].items()):
        ws.append([k, v])
    ws.append([])
    ws.append(["Enabled by console", "Count"])
    for k, v in sorted(s["enabled_by_console"].items()):
        ws.append([k, v])
    ws.append([])
    ws.append(["Data as of", f"workbook synced {st.get_meta('last_sync') or '—'}; Salesforce {st.get_meta('sf_as_of') or '—'}; "
                             f"consoles {st.get_meta('s1_as_of') or '—'}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 60

    cl = wb.create_sheet("Customer List")
    cl.append(EXPORT_COLUMNS)
    for row in export_rows(rows):
        cl.append(row)

    tr = wb.create_sheet("Tracker")
    tr.append([label for label, _ in TRACKER_COLUMNS])
    for r in rows:
        line = []
        for _, key in TRACKER_COLUMNS:
            if key == "_tenant_state":
                value = _tenant_state(r)
            elif key == "_prompt":
                value = {1: "Enabled", 0: "Not enabled"}.get(r.get("s1_prompt_enabled"), "not checked")
            elif key == "_admins":
                value = "; ".join(f"{u.get('name') or ''} <{u.get('email') or ''}>" for u in r.get("s1_prompt_users") or [])
            elif key == "_enabled":
                value = _enabled_yn(r)
            else:
                value = r.get(key)
            if isinstance(value, list):
                value = "; ".join(str(v) for v in value)
            line.append(value)
        tr.append(line)
    for sheet in (cl, tr):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        sheet.freeze_panes = "B2"
        sheet.auto_filter.ref = sheet.dimensions

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return {"path": str(path), **s}


def export_default_path():
    return Path.home() / "Downloads" / f"EAP - Customer List {datetime.datetime.now():%Y-%m-%d %H%M}.xlsx"


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
