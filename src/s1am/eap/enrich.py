"""Match each EAP request to a Salesforce account and pick its SentinelOne tenant.

Rules run in order; the first hit wins and its number + confidence are stored:

  0  Joe's "Name" column links a Tenant__c record                    high
  1  the form names a 1000_ tenant                                   high
  2  the form gives a numeric S1 account ID                          high
  3  …or the account was resolved on the console from host + name    high
  4  contact / super-admin email → Contact → Account                 medium
  5  email domain → other Contacts → Account (webmail skipped)       low
  6  company name → Account.Name LIKE                                low

Low confidence always ends in needs_review. Each rule is one batched query over all the
requests (salesforce.eap_queries); nothing is queried per row. Tenant selection: only
TenantStatus__c = 'Contract' is ever auto-selected; the other statuses are shown as
candidates. A form console in a different region from the chosen tenant is flagged.
"""
import re

from . import forms

HIGH, MEDIUM, LOW = "high", "medium", "low"
RULE_LABELS = {
    0: "Salesforce tenant link in Joe's column",
    1: "1000_ tenant on the form",
    2: "S1 account ID on the form",
    3: "account resolved on the console from the form",
    4: "contact email",
    5: "email domain",
    6: "company name",
}
CONFIDENCE = {0: HIGH, 1: HIGH, 2: HIGH, 3: HIGH, 4: MEDIUM, 5: LOW, 6: LOW}

_SF_TENANT_LINK_RE = re.compile(r"/Tenant__c/([a-zA-Z0-9]{15,18})")
_TENANT_NAME_RE = re.compile(r"^1000_([A-Za-z0-9]+)_(\d+)$")


def tenant_code(name):
    m = _TENANT_NAME_RE.match(name or "")
    return m.group(1).upper() if m else None


def tenant_account_id(name):
    m = _TENANT_NAME_RE.match(name or "")
    return m.group(2) if m else None


def _domain(email):
    return email.rsplit("@", 1)[-1].lower() if email and "@" in email else None


def _candidate(t, agents):
    a = agents.get(t["Id"], {})
    return {
        "id": t["Id"], "name": t["Name"], "status": t.get("TenantStatus__c"), "product": t.get("Product__r.Name"),
        "account_id": t.get("Account__c"), "account_name": t.get("Account__r.Name"),
        "complete_agents": a.get("complete", 0), "control_agents": a.get("control", 0),
        "total_agents": a.get("total", 0), "term_end": t.get("CurrentTermEndDate__c"),
    }


def select_tenant(candidates, named=None, form_code=None):
    """Pick the tenant to provision → (candidate | None, reason | None)."""
    contract = [c for c in candidates if c["status"] == "Contract"]
    by_name = {c["name"]: c for c in candidates}
    if named:
        c = by_name.get(named)
        if c and c["status"] == "Contract":
            return c, None
        if c:
            return None, f"tenant named on the form ({named}) is {c['status']}, not Contract"
    if not contract:
        others = ", ".join(f"{c['name']} ({c['status']})" for c in candidates[:4])
        return None, "no contract tenant" + (f" — only {others}" if others else "")
    if len(contract) == 1:
        return contract[0], None
    if form_code:
        same = [c for c in contract if tenant_code(c["name"]) == form_code]
        if len(same) == 1:
            return same[0], None
    with_agents = [c for c in contract if (c.get("total_agents") or 0) > 0]
    if len(with_agents) == 1:
        return with_agents[0], None
    return None, f"multiple contract tenants ({len(contract)})"


def _empty_result(sf_as_of):
    return {"sf_updated_at": sf_as_of, "sf_account_id": None, "sf_account_name": None, "customer_id": None,
            "country": None, "sf_tenants": [], "sf_tenant": None, "sf_tenant_id": None, "match_rule": None,
            "match_confidence": None, "match_flag": None, "match_note": None,
            "complete_agents": None, "control_agents": None, "total_agents": None}


def enrich(rows, q, host_codes=None, sf_as_of=None):
    """rows: request dicts from the store. q: salesforce.eap_queries (or a fake with the same
    functions). host_codes: {console hostname: profile code}. → {request id: fields to store}."""
    host_codes = host_codes or {}
    rows_by_id = {r["id"]: r for r in rows}

    # ── gather keys across all rows ─────────────────────────────────────
    per, link_ids, tenant_names, account_ids, emails = {}, set(), set(), set(), set()
    for r in rows:
        d = r.get("details") or {}
        info = {"link_id": None, "tenants": list(d.get("tenants") or []), "ids": list(d.get("account_ids") or []),
                "console_id": None, "emails": [], "domains": set(), "company": r.get("company")}
        m = _SF_TENANT_LINK_RE.search(r.get("joe_ref") or "")
        if m:
            info["link_id"] = m.group(1)
            link_ids.add(m.group(1))
        if r.get("s1_target_source") == "console-lookup" and r.get("s1_account_id"):
            info["console_id"] = str(r["s1_account_id"])
            info["ids"].append(info["console_id"])
        for e in (r.get("contact_email"), r.get("super_admin_email")):
            if e and "@" in e:
                info["emails"].append(e.strip())
                info["domains"].add(_domain(e))
        tenant_names.update(info["tenants"])
        account_ids.update(info["ids"])
        emails.update(info["emails"])
        per[r["id"]] = info

    # ── batched lookups for rules 0–4 ───────────────────────────────────
    t_by_id = {t["Id"]: t for t in (q.tenants_by_id(sorted(link_ids)) if link_ids else [])}
    t_by_name = {t["Name"].upper(): t for t in (q.tenants_by_name(sorted(tenant_names)) if tenant_names else [])}
    t_by_suffix = {}
    for t in (q.tenants_by_account_id_suffix(sorted(account_ids)) if account_ids else []):
        aid = tenant_account_id(t["Name"])
        if aid:
            t_by_suffix.setdefault(aid, []).append(t)
    c_by_email = {}
    for c in (q.contacts_by_email(sorted(emails)) if emails else []):
        if c.get("AccountId"):
            c_by_email.setdefault((c.get("Email") or "").lower(), c)

    matches, ambiguous = {}, {}   # id → (account id, rule, named tenant | None)
    for rid, info in per.items():
        hit = None
        t = t_by_id.get(info["link_id"]) if info["link_id"] else None
        if t:
            hit = (t["Account__c"], 0, t["Name"])
        for n in ([] if hit else info["tenants"]):
            t = t_by_name.get(n.upper())
            if t:
                hit = (t["Account__c"], 1, t["Name"])
                break
        for aid in ([] if hit else info["ids"]):
            ts = t_by_suffix.get(aid) or []
            if ts:
                rule = 3 if aid == info["console_id"] else 2
                hit = (ts[0]["Account__c"], rule, ts[0]["Name"] if len(ts) == 1 else None)
                break
        for e in ([] if hit else info["emails"]):
            c = c_by_email.get(e.lower())
            if c:
                hit = (c["AccountId"], 4, None)
                break
        if hit:
            matches[rid] = hit

    # ── rule 5: email domain ────────────────────────────────────────────
    rest = [rid for rid in per if rid not in matches]
    domains = {d for rid in rest for d in per[rid]["domains"] if d}
    c_by_domain = {}
    for c in (q.contacts_by_domain(sorted(domains)) if domains else []):
        d = _domain(c.get("Email"))
        if d and c.get("AccountId"):
            c_by_domain.setdefault(d, set()).add(c["AccountId"])
    for rid in rest:
        accts = set()
        for d in per[rid]["domains"]:
            accts |= c_by_domain.get(d, set())
        if len(accts) == 1:
            matches[rid] = (next(iter(accts)), 5, None)
        elif len(accts) > 1:
            ambiguous[rid] = f"email domain matches {len(accts)} accounts"

    # ── rule 6: company name ────────────────────────────────────────────
    rest = [rid for rid in per if rid not in matches]
    companies = {per[rid]["company"] for rid in rest if per[rid]["company"]}
    a_by_core = {}
    for a in (q.accounts_by_name(sorted(companies)) if companies else []):
        a_by_core.setdefault(forms.normalize_company(a["Name"]).lower(), []).append(a)
    for rid in rest:
        core = forms.normalize_company(per[rid]["company"] or "").lower()
        if len(core) < 3:
            continue
        exact = a_by_core.get(core, [])
        partial = [a for k, lst in a_by_core.items() if core in k for a in lst]
        ids = {a["Id"] for a in (exact or partial)}
        if len(ids) == 1:
            matches[rid] = (next(iter(ids)), 6, None)
        elif len(ids) > 1:
            ambiguous[rid] = f"company name matches {len(ids)} accounts"

    # ── account + tenant details for every match ────────────────────────
    acct_ids = sorted({m[0] for m in matches.values()})
    accounts = {a["Id"]: a for a in (q.accounts_by_id(acct_ids) if acct_ids else [])}
    tenants = {}
    for t in (q.tenants_for_accounts(acct_ids) if acct_ids else []):
        tenants.setdefault(t["Account__c"], []).append(t)
    all_tenant_ids = [t["Id"] for ts in tenants.values() for t in ts]
    agents = q.agent_counts(all_tenant_ids) if all_tenant_ids else {}

    # ── per-row result ──────────────────────────────────────────────────
    out = {}
    for rid, r in rows_by_id.items():
        d = r.get("details") or {}
        form_code = next((host_codes[h] for h in (d.get("hosts") or []) if h in host_codes), None)
        f = _empty_result(sf_as_of)
        if rid not in matches:
            f["match_note"] = ambiguous.get(rid) or "no Salesforce match"
            out[rid] = f
            continue
        aid, rule, named = matches[rid]
        a = accounts.get(aid, {})
        f.update(sf_account_id=aid, sf_account_name=a.get("Name"), customer_id=a.get("Customer_ID__c"),
                 country=a.get("BillingCountry"), match_rule=rule, match_confidence=CONFIDENCE[rule])
        cands = [_candidate(t, agents) for t in tenants.get(aid, [])]
        cands.sort(key=lambda c: (c["status"] != "Contract", -(c["total_agents"] or 0), c["name"]))
        f["sf_tenants"] = cands
        notes = [f"Matched by {RULE_LABELS[rule]}"]
        sel, reason = select_tenant(cands, named=named, form_code=form_code)
        if sel:
            f.update(sf_tenant=sel["name"], sf_tenant_id=sel["id"], complete_agents=sel["complete_agents"],
                     control_agents=sel["control_agents"], total_agents=sel["total_agents"])
            code = tenant_code(sel["name"])
            if form_code and code and form_code != code:
                f["match_flag"] = "console/tenant region mismatch"
                notes.append(f"form console {form_code} but tenant {code} — verify")
        else:
            f["match_flag"] = reason
            notes.append(reason)
        f["match_note"] = "; ".join(n for n in notes if n)
        out[rid] = f
    return out


def derive_status(r):
    """Workflow status from the stored facts → (status, reason). The store keeps a status the
    user set by hand, so this only ever fills the automatic one."""
    if not r.get("wants_prompt"):
        return "not_eligible", "Prompt Security not requested"
    if r.get("s1_prompt_enabled"):
        return "provisioned", f"Prompt already on {r.get('s1_console')} account {r.get('s1_account_id')}"
    if not r.get("sf_account_id"):
        if r.get("no_s1_claimed"):
            return "not_eligible", "no SentinelOne (per the form)"
        return "needs_review", r.get("match_note") or "no Salesforce match"
    if r.get("match_confidence") == LOW:
        return "needs_review", f"low-confidence match ({RULE_LABELS.get(r.get('match_rule'), 'unknown rule')})"
    if r.get("match_flag"):
        return "needs_review", r["match_flag"]
    if r.get("super_admin_needs_review"):
        return "needs_review", r.get("super_admin_note") or "super admin email missing"
    if r.get("s1_error"):
        return "needs_review", f"console check: {r['s1_error']}"
    if not r.get("s1_account_name"):
        return "new", "awaiting console check"
    if not r.get("prompt_tenant_name"):
        return "needs_review", "prompt tenant name missing"
    return "ready", f"{r.get('s1_console')} account {r.get('s1_account_name')} confirmed, no Prompt yet"
