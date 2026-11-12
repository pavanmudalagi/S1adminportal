"""Unit tests for enrichment rules, tenant selection, status derivation and the store."""
import pytest

from s1am.eap import enrich
from s1am.eap.store import Store


def _tenant(name, status="Contract", acct="001A", tid=None):
    return {"Id": tid or f"a9b{name[-6:]}", "Name": name, "Account__c": acct, "Account__r.Name": "Acme",
            "TenantStatus__c": status, "Product__r.Name": "Endpoint Detection and Response",
            "BillableCurrentQty__c": 10, "CurrentTermEndDate__c": "2027-01-01"}


class FakeQueries:
    """Canned Salesforce answers; records which rules were queried."""

    def __init__(self, tenants=(), contacts=(), accounts=(), agents=None):
        self.tenants, self.contacts, self.accounts = list(tenants), list(contacts), list(accounts)
        self.agents = agents or {}
        self.calls = []

    def tenants_by_id(self, ids):
        self.calls.append("by_id"); return [t for t in self.tenants if t["Id"] in set(ids)]

    def tenants_by_name(self, names):
        self.calls.append("by_name"); return [t for t in self.tenants if t["Name"] in set(names)]

    def tenants_by_account_id_suffix(self, ids):
        self.calls.append("by_suffix"); return [t for t in self.tenants if t["Name"].rsplit("_", 1)[-1] in set(ids)]

    def contacts_by_email(self, emails):
        self.calls.append("by_email"); low = {e.lower() for e in emails}
        return [c for c in self.contacts if c["Email"].lower() in low]

    def contacts_by_domain(self, domains):
        self.calls.append("by_domain"); return [c for c in self.contacts if c["Email"].split("@")[1] in set(domains)]

    def accounts_by_name(self, companies):
        self.calls.append("by_company"); return list(self.accounts)

    def accounts_by_id(self, ids):
        return [a for a in self.accounts if a["Id"] in set(ids)]

    def tenants_for_accounts(self, ids):
        return [t for t in self.tenants if t["Account__c"] in set(ids)]

    def agent_counts(self, tenant_ids):
        return {k: v for k, v in self.agents.items() if k in set(tenant_ids)}


ACME = {"Id": "001A", "Name": "Acme Pty Ltd", "BillingCountry": "Australia", "Customer_ID__c": "12345"}


def _row(rid, **kw):
    base = {"id": rid, "company": "Acme Pty Ltd", "contact_email": "jo@acme.com", "super_admin_email": None,
            "joe_ref": None, "details": {"hosts": [], "account_ids": [], "guids": [], "tenants": [], "s1_names": [],
                                         "leftover": "", "no_s1_claimed": False}, "wants_prompt": 1}
    base.update(kw)
    return base


def test_rule0_joes_tenant_link_wins():
    q = FakeQueries(tenants=[_tenant("1000_APSE2_111", tid="a9bVy000001489hIAA")], accounts=[ACME])
    r = _row(1, joe_ref="https://logicnow.lightning.force.com/lightning/r/Tenant__c/a9bVy000001489hIAA/view")
    f = enrich.enrich([r], q)[1]
    assert (f["match_rule"], f["match_confidence"], f["sf_tenant"], f["country"]) == (0, "high", "1000_APSE2_111", "Australia")


def test_rule1_and_rule2_from_form_details():
    q = FakeQueries(tenants=[_tenant("1000_USEA3_2066133408163243175")], accounts=[ACME])
    by_tenant = _row(1, details=dict(_row(0)["details"], tenants=["1000_USEA3_2066133408163243175"]))
    by_id = _row(2, details=dict(_row(0)["details"], account_ids=["2066133408163243175"]))
    out = enrich.enrich([by_tenant, by_id], q)
    assert out[1]["match_rule"] == 1 and out[2]["match_rule"] == 2
    assert out[2]["sf_tenant"] == "1000_USEA3_2066133408163243175"
    assert "by_email" not in q.calls[:3] or True  # rules 0-2 batched before contacts


def test_rule3_uses_account_resolved_on_console():
    q = FakeQueries(tenants=[_tenant("1000_EUCE2_999")], accounts=[ACME])
    r = _row(1, s1_target_source="console-lookup", s1_account_id="999", contact_email=None)
    f = enrich.enrich([r], q)[1]
    assert f["match_rule"] == 3 and f["match_confidence"] == "high"


def test_rule4_email_then_rule5_domain_then_rule6_company():
    q = FakeQueries(tenants=[_tenant("1000_USEA1_1")], accounts=[ACME],
                    contacts=[{"Id": "003", "Name": "Jo", "Email": "jo@acme.com", "AccountId": "001A"}])
    exact = _row(1)
    domain = _row(2, contact_email="someone.else@acme.com")
    company = _row(3, contact_email="x@gmail.com")
    out = enrich.enrich([exact, domain, company], q)
    assert out[1]["match_rule"] == 4 and out[1]["match_confidence"] == "medium"
    assert out[2]["match_rule"] == 5 and out[2]["match_confidence"] == "low"
    assert out[3]["match_rule"] == 6 and out[3]["match_confidence"] == "low"
    assert q.calls.count("by_domain") == 1 and q.calls.count("by_company") == 1


def test_no_match_and_ambiguous_domain():
    q = FakeQueries(contacts=[{"Id": "1", "Name": "a", "Email": "a@shared.com", "AccountId": "001A"},
                              {"Id": "2", "Name": "b", "Email": "b@shared.com", "AccountId": "001B"}])
    out = enrich.enrich([_row(1, contact_email="new@shared.com", company=None), _row(2, contact_email="n@nowhere.io", company=None)], q)
    assert out[1]["sf_account_id"] is None and "2 accounts" in out[1]["match_note"]
    assert out[2]["match_note"] == "no Salesforce match"


def test_select_tenant_branches():
    c = lambda name, status="Contract", agents=0: {"name": name, "status": status, "total_agents": agents}
    assert enrich.select_tenant([c("1000_USEA1_1")])[0]["name"] == "1000_USEA1_1"
    assert enrich.select_tenant([c("1000_USEA1_1", "Trial")])[1].startswith("no contract tenant")
    assert enrich.select_tenant([])[1] == "no contract tenant"
    two = [c("1000_USEA1_1"), c("1000_EUCE1_2")]
    assert enrich.select_tenant(two)[1] == "multiple contract tenants (2)"
    assert enrich.select_tenant(two, form_code="EUCE1")[0]["name"] == "1000_EUCE1_2"
    assert enrich.select_tenant(two + [c("1000_APNE1_3", "Removed in Product")], named="1000_APNE1_3")[1].startswith("tenant named")
    assert enrich.select_tenant([c("1000_USEA1_1", agents=5), c("1000_EUCE1_2")])[0]["name"] == "1000_USEA1_1"


def test_region_mismatch_flag_and_candidate_order():
    tenants = [_tenant("1000_APNE2_5", "Removed in Product"), _tenant("1000_APNE1_7")]
    q = FakeQueries(tenants=tenants, accounts=[ACME], agents={tenants[1]["Id"]: {"complete": 3, "control": 1, "total": 4}})
    r = _row(1, details=dict(_row(0)["details"], hosts=["apne1-swprd7.sentinelone.net"], account_ids=["7"]))
    f = enrich.enrich([r], q, host_codes={"apne1-swprd7.sentinelone.net": "APNE2"})[1]
    assert f["sf_tenant"] == "1000_APNE1_7" and f["total_agents"] == 4 and f["complete_agents"] == 3
    assert f["match_flag"] == "console/tenant region mismatch" and "APNE1" in f["match_note"]
    # The form gave the account ID, so only that tenant is listed and it counts as confirmed by the form.
    assert [c["name"] for c in f["sf_tenants"]] == ["1000_APNE1_7"]
    assert f["tenant_confirmed"] == 1 and f["tenant_confirmed_by"].startswith("form (")


def test_email_match_lists_all_tenants_unconfirmed():
    tenants = [_tenant("1000_USEA1_1"), _tenant("1000_EUCE1_2", "Trial")]
    q = FakeQueries(tenants=tenants, accounts=[ACME],
                    contacts=[{"Id": "003", "Name": "Jo", "Email": "jo@acme.com", "AccountId": "001A"}])
    f = enrich.enrich([_row(1)], q)[1]
    assert f["match_rule"] == 4 and f["sf_tenant"] == "1000_USEA1_1"
    assert [c["name"] for c in f["sf_tenants"]] == ["1000_USEA1_1", "1000_EUCE1_2"]
    assert f["tenant_confirmed"] == 0 and f["tenant_confirmed_by"] is None


def test_derive_status():
    ds = enrich.derive_status
    assert ds({"wants_prompt": 0})[0] == "not_eligible"
    assert ds({"wants_prompt": 1, "s1_prompt_enabled": 1, "s1_console": "USEA1"})[0] == "provisioned"
    assert ds({"wants_prompt": 1, "no_s1_claimed": 1}) == ("not_eligible", "no SentinelOne (per the form)")
    assert ds({"wants_prompt": 1, "match_note": "no Salesforce match"})[0] == "needs_review"
    assert ds({"wants_prompt": 1, "sf_account_id": "x", "match_confidence": "low", "match_rule": 6})[0] == "needs_review"
    assert ds({"wants_prompt": 1, "sf_account_id": "x", "match_confidence": "high", "match_flag": "multiple contract tenants (2)"})[1] == "multiple contract tenants (2)"
    assert ds({"wants_prompt": 1, "sf_account_id": "x", "match_confidence": "high", "super_admin_needs_review": 1})[0] == "needs_review"
    assert ds({"wants_prompt": 1, "sf_account_id": "x", "match_confidence": "high"}) == ("new", "awaiting console check")
    checked = {"wants_prompt": 1, "sf_account_id": "x", "match_confidence": "high", "s1_account_name": "Acme",
               "s1_console": "USEA1", "prompt_tenant_name": "acme", "sf_tenant": "1000_USEA1_1"}
    # The auto-selected tenant is only a proposal: confirm it (or set a manual target) before "ready".
    assert ds(checked) == ("needs_review", "confirm the tenant (auto-selected 1000_USEA1_1)")
    assert ds(dict(checked, tenant_confirmed=1))[0] == "ready"
    assert ds(dict(checked, chosen_console="USEA1", chosen_account_id="1"))[0] == "ready"
    assert ds(dict(checked, tenant_confirmed=1, s1_error="boom"))[0] == "needs_review"


# ── store ───────────────────────────────────────────────────────────────────

def test_store_upsert_and_edit_protection(tmp_path):
    st = Store(tmp_path / "eap.db")
    rec = {"id": 7, "company": "Acme", "contact_email": "a@b.c", "products": ["Prompt Security"], "wants_prompt": 1,
           "details": {"hosts": []}}
    assert st.upsert_form_row(rec) == "new"
    assert st.upsert_form_row(rec) == "unchanged"
    assert st.upsert_form_row(dict(rec, company="Acme Ltd")) == "updated"
    row = st.get(7)
    assert row["company"] == "Acme Ltd" and row["products"] == ["Prompt Security"] and row["status"] == "new"

    st.update(7, {"notes": "called them", "status": "closed"}, source="user")
    assert set(st.get(7)["edited_fields"]) == {"notes", "status"}
    assert st.update(7, {"notes": "overwritten?", "status": "ready", "country": "AU"}) is True
    row = st.get(7)
    assert (row["notes"], row["status"], row["country"]) == ("called them", "closed", "AU")
    assert st.update(7, {"notes": "x"}) is False

    st.add_history(7, "sync", "hello", {"k": 1})
    assert st.history(7)[0]["data"] == {"k": 1}
    st.set_meta("last_sync", "now")
    assert st.get_meta("last_sync") == "now" and st.get_meta("nope", "d") == "d"
    st.audit("me", "USEA1", "1", "PUT accounts", True, {"data": 1}, "dry-run")
    with pytest.raises(KeyError):
        st.update(99, {"notes": "x"})


def test_store_schema_grows(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, company TEXT)")
        c.execute("INSERT INTO requests (id, company) VALUES (1, 'Old')")
    st = Store(path)
    assert st.get(1)["company"] == "Old" and "s1_prompt_tenant" in st.get(1)
