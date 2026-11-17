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
import json
import re
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
    # A form response may describe a customer that was already added by hand. Identical pairs are merged
    # here (the manual row's workflow moves onto the form row); differing pairs are left for the user.
    result["merged"], result["duplicates"] = [], []
    for d in pending_duplicates(st):
        if d["identical"]:
            merge_manual_into_form(st, d["manual_id"], d["form_id"], keep="form", who="sync")
            result["merged"].append({"manual_id": d["manual_id"], "form_id": d["form_id"], "company": d["company"]})
        else:
            result["duplicates"].append(d)
    st.set_meta("last_sync", _now())
    st.set_meta("form_path", str(path))
    # Remembered so the UI can say "N new in the last sync" after a reload too.
    st.set_meta("last_sync_result", json.dumps({"at": _now(), "new": result["new"], "updated": result["updated"],
                                                "unchanged": result["unchanged"], "rows": result["rows"],
                                                "merged": result["merged"], "duplicates": len(result["duplicates"])}))
    return result


PRODUCT_CHOICES = ["Prompt Security", "Identity Security", "Hyperautomation", "Wayfinder MDR Essentials"]


def create_manual(st, data, who=None):
    """Add a request that did not come through the Forms workbook (email, call, partner ask).

    Same shape as a synced row so enrichment, console check, provisioning and export treat it alike;
    the id is allocated above MANUAL_ID_BASE and origin="manual" so a later sync never touches it."""
    company = (data.get("company") or "").strip()
    if not company:
        raise ValueError("Company is required")
    email = (data.get("contact_email") or "").strip()
    if email and not forms.EMAIL_RE.fullmatch(email):
        raise ValueError("Contact email does not look like an email address")
    admin_email = (data.get("super_admin_email") or "").strip() or email
    if admin_email and not forms.EMAIL_RE.fullmatch(admin_email):
        raise ValueError("Super admin email does not look like an email address")
    products = [p for p in (data.get("products") or ["Prompt Security"]) if p in PRODUCT_CHOICES]
    if not products:
        raise ValueError("Pick at least one product")
    details_raw = (data.get("details_raw") or "").strip()
    details = forms.parse_details(details_raw)
    who = who or actor()
    now = _now()
    rec = {
        "id": st.next_manual_id(), "start_time": now, "completion_time": now, "last_modified": now,
        "responder_email": who, "contact_name": (data.get("contact_name") or "").strip() or None, "company": company,
        "contact_email": email or None, "super_admin_raw": None,
        "super_admin_name": (data.get("super_admin_name") or "").strip() or (data.get("contact_name") or "").strip() or None,
        "super_admin_email": admin_email or None, "super_admin_source": "manual",
        "super_admin_needs_review": int(not admin_email), "super_admin_note": "entered by hand" if admin_email else "no super admin email given",
        "products_raw": "; ".join(products), "products": products, "wants_prompt": int("Prompt Security" in products),
        "details_raw": details_raw or None, "details": details, "no_s1_claimed": int(details["no_s1_claimed"]),
    }
    st.upsert_form_row(rec)
    rid = rec["id"]
    defaults = {"origin": "manual", "prompt_tenant_name": forms.suggest_tenant_name(company)}
    if admin_email:
        defaults["first_users"] = [{"name": rec["super_admin_name"] or "", "email": admin_email}] + [
            {"name": a["promptUserName"], "email": a["promptUserEmail"]}
            for a in account_fields.PROMPT_STANDARD_ADMINS if a["promptUserEmail"].lower() != admin_email.lower()]
    st.update(rid, defaults)
    user_fields = {}
    if (data.get("notes") or "").strip():
        user_fields["notes"] = data["notes"].strip()
    tenant = (data.get("sf_tenant") or "").strip().upper()
    if tenant:
        if not re.fullmatch(r"1000_[A-Z0-9]+_\d+", tenant):
            raise ValueError("Salesforce tenant must look like 1000_CODE_accountId")
        user_fields.update(sf_tenant=tenant, tenant_confirmed=1)
    console = (data.get("chosen_console") or "").strip()
    if console:
        if console not in profiles():
            raise ValueError(f"Unknown console code {console}")
        acct = (data.get("chosen_account_id") or "").strip()
        if not acct.isdigit():
            raise ValueError("Console account ID must be numeric")
        user_fields.update(chosen_console=console, chosen_account_id=acct)
    if user_fields:
        st.update(rid, user_fields, source="user")
        if tenant:
            st.update(rid, {"tenant_confirmed_at": now, "tenant_confirmed_by": who})
    _restatus(st, rid)
    st.add_history(rid, "manual", f"Added by hand by {who}" + (f" — tenant {tenant}" if tenant else "") + (f" — console {console}" if console else ""))
    return st.get(rid)


# ── duplicates between manual rows and form responses ───────────────────────

DUP_FIELDS = ["company", "contact_name", "contact_email", "super_admin_name", "super_admin_email", "products", "details"]
_DISMISSED_KEY = "eap_dup_dismissed"


def _norm(field, value):
    if field == "products":
        return sorted(value or [])
    if field == "details":
        d = value or {}
        return {k: sorted(x.lower() for x in (d.get(k) or [])) for k in ("hosts", "account_ids", "tenants")}
    text = (value or "").strip()
    return text.lower() if "email" in field else " ".join(text.split()).lower()


def _dup_keys(r):
    """Hashable identifiers a request can be recognised by."""
    d = r.get("details") or {}
    keys = set()
    if r.get("company"):
        keys.add(("company", forms.normalize_company(r["company"])))
    for f in ("contact_email", "super_admin_email"):
        if r.get(f):
            keys.add(("email", r[f].strip().lower()))
    for a in d.get("account_ids") or []:
        keys.add(("account", str(a)))
    for t in d.get("tenants") or []:
        keys.add(("tenant", t.upper()))
    if r.get("sf_tenant"):
        keys.add(("tenant", r["sf_tenant"].upper()))
        keys.add(("account", r["sf_tenant"].rsplit("_", 1)[-1]))
    if r.get("chosen_account_id"):
        keys.add(("account", str(r["chosen_account_id"])))
    return keys


def _dismissed(st):
    try:
        return set(json.loads(st.get_meta(_DISMISSED_KEY) or "[]"))
    except ValueError:
        return set()


def pending_duplicates(st):
    """Manual rows that look like a form response: same company, email, account ID or tenant.

    Each pair lists the fields that differ (form value vs manual value); identical=True when none do."""
    rows = st.list()
    manual = [r for r in rows if r.get("origin") == "manual"]
    if not manual:
        return []
    forms_rows = [r for r in rows if r.get("origin") != "manual"]
    dismissed = _dismissed(st)
    out = []
    for m in manual:
        mk = _dup_keys(m)
        for f in forms_rows:
            shared = mk & _dup_keys(f)
            if not shared or f"{m['id']}:{f['id']}" in dismissed:
                continue
            diffs = [{"field": fld, "form": f.get(fld), "manual": m.get(fld)} for fld in DUP_FIELDS
                     if _norm(fld, f.get(fld)) != _norm(fld, m.get(fld)) and (f.get(fld) or m.get(fld))]
            out.append({"manual_id": m["id"], "form_id": f["id"], "company": f.get("company") or m.get("company"),
                        "matched_on": sorted(f"{k}: {v}" for k, v in shared), "differences": diffs, "identical": not diffs})
    return out


def dismiss_duplicate(st, manual_id, form_id, who=None):
    keys = _dismissed(st); keys.add(f"{manual_id}:{form_id}")
    st.set_meta(_DISMISSED_KEY, json.dumps(sorted(keys)))
    st.add_history(manual_id, "duplicate", f"Marked as not a duplicate of #{form_id} by {who or actor()}")


def merge_manual_into_form(st, manual_id, form_id, keep="form", who=None):
    """Fold a manual row into the form response that describes the same customer.

    keep="form": the workbook's details stand; keep="manual": the hand-entered details overwrite the form's
    and are marked as user edits so later syncs leave them alone. Either way the manual row's workflow
    (notes, confirmed tenant, console target, provisioning edits, extra tenants, console check, history)
    moves to the form row, and the manual row is hidden as merged."""
    m, f = st.get(manual_id), st.get(form_id)
    if not m or not f:
        raise KeyError("Unknown request")
    if m.get("origin") != "manual" or f.get("origin") == "manual":
        raise ValueError("Merge goes from a manual row into a form response")
    if keep not in ("form", "manual"):
        raise ValueError("keep must be form or manual")
    who = who or actor()
    from .store import USER_FIELDS
    edited = set(m.get("edited_fields") or [])
    carry = {}
    for fld in USER_FIELDS:
        if fld in edited or (m.get(fld) not in (None, "", [], 0) and f.get(fld) in (None, "", [])):
            carry[fld] = m.get(fld)
    if m.get("notes") and f.get("notes") and m["notes"] != f["notes"]:
        carry["notes"] = f["notes"] + "\n" + m["notes"]
    if carry:
        st.update(form_id, carry, source="user")
    plain = {k: m.get(k) for k in ("tenant_confirmed_at", "tenant_confirmed_by", "sf_tenant_id") if m.get(k) and not f.get(k)}
    plain.update({k: m.get(k) for k in m if k.startswith("s1_") and m.get(k) not in (None, "", []) and f.get(k) in (None, "", [])})
    if plain:
        st.update(form_id, plain)
    if keep == "manual":
        st.update(form_id, {k: m.get(k) for k in DUP_FIELDS + ["details_raw", "products_raw", "wants_prompt", "no_s1_claimed"]}, source="user")
    st.repoint(manual_id, form_id)
    st.update(manual_id, {"merged_into": form_id, "status": "merged", "status_reason": f"merged into #{form_id}"})
    _restatus(st, form_id)
    st.add_history(form_id, "duplicate", f"Merged manual request #{manual_id} into this form response, kept {keep} details ({who})")
    return st.get(form_id)


def last_sync_result(st):
    raw = st.get_meta("last_sync_result")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


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
        for t in st.tenants(r["id"]):
            check_tenant(st, t["id"], client_for)
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
    ("Tenant", "sf_tenant"), ("Tenant state", "_tenant_state"), ("Match", "_match"), ("Match note", "match_note"),
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


def match_label(r):
    """What the Match column should say once a human or the console has settled the tenant.

    The Salesforce confidence (high/medium/low) describes the automatic guess. Once the tenant is
    confirmed, or Prompt is verified enabled on that very account, the guess no longer matters.
    """
    if r.get("status") == "provisioned" and r.get("s1_prompt_enabled") == 1:
        return "verified"
    if r.get("tenant_confirmed") or (r.get("chosen_console") and r.get("chosen_account_id")):
        return "confirmed"
    return r.get("match_confidence") or ""


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
            r.get("sf_account_name"), r.get("customer_id"), r.get("country"),
            " | ".join(x for x in [r.get("sf_tenant"), *(t.get("tenant_name") for t in r.get("extra_tenants") or [])] if x) or None,
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
    extra = [t for r in asked for t in r.get("extra_tenants") or []]
    for t in extra:
        if t.get("s1_prompt_enabled"):
            by_console[t.get("console") or "?"] = by_console.get(t.get("console") or "?", 0) + 1
    return {"requests": len(rows), "asked_for_prompt": len(asked), "enabled": len(enabled),
            "pending": len(asked) - len(enabled) - by_status.get("not_eligible", 0),
            "not_eligible": by_status.get("not_eligible", 0), "by_status": by_status, "enabled_by_console": by_console,
            "tenants": len(asked) + len(extra),
            "tenants_enabled": len(enabled) + sum(1 for t in extra if t.get("s1_prompt_enabled"))}


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
                         ("Tenants to provision (incl. additional)", s["tenants"]),
                         ("Tenants with Prompt enabled", s["tenants_enabled"]),
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

    def tracker_line(r, tenant_state):
        line = []
        for _, key in TRACKER_COLUMNS:
            if key == "_tenant_state":
                value = tenant_state
            elif key == "_match":
                value = match_label(r)
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
        return line

    for r in rows:
        tr.append(tracker_line(r, _tenant_state(r)))
        for t in r.get("extra_tenants") or []:
            # One line per additional tenant: the request's columns, with the tenant's own console data.
            merged = dict(r, sf_tenant=t.get("tenant_name"), s1_console=t.get("console"), s1_account_id=t.get("account_id"),
                          status=t.get("status"), status_reason=t.get("status_reason"), enabled_yn=None, notes=None,
                          **{k: t.get(k) for k in ("s1_account_name", "s1_prompt_enabled", "s1_prompt_tenant",
                                                   "s1_prompt_users", "s1_prompt_bundles", "s1_checked_at")})
            tr.append(tracker_line(merged, "additional tenant"))
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


# ── additional tenants ──────────────────────────────────────────────────────

def tenant_status(t):
    """Status of one additional tenant row → (status, reason). Added by hand, so it counts as confirmed."""
    if t.get("s1_error"):
        return "needs_review", f"console check: {t['s1_error']}"
    if t.get("s1_prompt_enabled"):
        return "provisioned", f"Prompt already on {t.get('console')} account {t.get('account_id')}"
    if not t.get("s1_account_name"):
        return "new", "awaiting console check"
    if not t.get("prompt_tenant_name"):
        return "needs_review", "prompt tenant name missing"
    return "ready", f"{t.get('console')} account {t.get('s1_account_name')} confirmed, no Prompt yet"


def add_tenant(st, rid, tenant_name=None, console=None, account_id=None, client_for=None):
    """Attach another tenant (a 1000_ name, or console + account ID) to a request and check it."""
    row = st.get(rid)
    if row is None:
        raise KeyError(f"No request {rid}")
    tenant_name = (tenant_name or "").strip() or None
    if tenant_name:
        code, aid = enrich_mod.tenant_code(tenant_name), enrich_mod.tenant_account_id(tenant_name)
        if not (code and aid):
            raise ValueError("Tenant must look like 1000_<CODE>_<account id>")
        console, account_id = code, aid
    account_id = str(account_id or "").strip()
    console = (console or "").strip()
    if not (console and account_id.isdigit()):
        raise ValueError("Give a 1000_ tenant, or a console and a numeric account ID")
    if console not in profiles():
        raise ValueError(f"Console '{console}' is not a saved profile")
    primary = (enrich_mod.tenant_code(row.get("sf_tenant")), enrich_mod.tenant_account_id(row.get("sf_tenant")))
    taken = {primary, (row.get("chosen_console"), str(row.get("chosen_account_id") or "")),
             *((t["console"], str(t["account_id"])) for t in row.get("extra_tenants") or [])}
    if (console, account_id) in taken:
        raise ValueError(f"{console} account {account_id} is already on this request")
    cand = next((c for c in row.get("sf_tenants") or [] if c.get("name") == tenant_name), {})
    fields = {
        "tenant_name": tenant_name or f"{console} account {account_id}", "tenant_id": cand.get("id"),
        "console": console, "account_id": account_id, "source": "user",
        "confirmed_at": _now(), "confirmed_by": actor(),
        # Prompt tenant names are unique: suffix the console so two tenants of one customer don't collide.
        "prompt_tenant_name": f"{forms.suggest_tenant_name(row.get('company'))}_{console.lower()}"[:64],
        "first_users": row.get("first_users") or [],
        "status": "new", "status_reason": "awaiting console check",
    }
    tid = st.add_tenant(rid, fields)
    st.add_history(rid, "tenant", f"Added tenant {fields['tenant_name']} ({console} account {account_id})")
    if client_for:
        check_tenant(st, tid, client_for)
    return st.get_tenant(tid)


def check_tenant(st, tid, client_for):
    t = st.get_tenant(tid)
    if t is None:
        raise KeyError(f"No tenant row {tid}")
    fields = s1check.check_target(t["console"], t["account_id"], client_for)
    for key in ("s1_target_source", "s1_console", "s1_account_id"):   # fixed on the tenant row
        fields.pop(key, None)
    st.update_tenant(tid, fields)
    status, reason = tenant_status(st.get_tenant(tid))
    st.update_tenant(tid, {"status": status, "status_reason": reason})
    st.add_history(t["request_id"], "s1-check", f"{t['tenant_name']}: {reason}")
    return st.get_tenant(tid)


def remove_tenant(st, tid):
    t = st.get_tenant(tid)
    if t is None:
        raise KeyError(f"No tenant row {tid}")
    st.delete_tenant(tid)
    st.add_history(t["request_id"], "tenant", f"Removed tenant {t['tenant_name']}")
    return t


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
