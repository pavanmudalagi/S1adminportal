"""Export workbook and summary numbers — written to a temp file, no network."""
import openpyxl

from s1am.eap import service
from s1am.eap.store import Store


def _seed(st):
    st.upsert_form_row({"id": 1, "company": "Acme", "contact_name": "Jo", "contact_email": "jo@acme.com",
                        "super_admin_name": "Jo", "super_admin_email": "jo@acme.com", "wants_prompt": 1,
                        "products": ["Prompt Security", "Identity Security", "Hyperautomation"],
                        "details": {"hosts": ["usea1-swprd5.sentinelone.net"], "account_ids": ["42"], "leftover": ""}})
    st.update(1, {"sf_account_name": "Acme Pty Ltd", "country": "Australia", "sf_tenant": "1000_USEA3_42",
                  "tenant_confirmed": 1, "tenant_confirmed_by": "form (S1 account ID on the form)",
                  "s1_console": "USEA3", "s1_account_name": "Acme", "s1_prompt_enabled": 1, "s1_prompt_tenant": "acme",
                  "s1_prompt_users": [{"name": "Jo", "email": "jo@acme.com"}], "status": "provisioned",
                  "complete_agents": 10, "control_agents": 2, "total_agents": 12})
    st.upsert_form_row({"id": 2, "company": "Beta", "wants_prompt": 1, "products": ["Prompt Security"], "details": {}})
    st.update(2, {"status": "needs_review", "s1_prompt_enabled": 0})
    st.upsert_form_row({"id": 3, "company": "Gamma", "wants_prompt": 0, "products": ["Identity Security"], "details": {}})
    st.update(3, {"status": "not_eligible"})
    st.upsert_form_row({"id": 4, "company": "Delta", "wants_prompt": 1, "products": ["Prompt Security"], "details": {}})
    st.update(4, {"enabled_yn": "Y", "date_enabled": "2026-10-01", "enabled_by": "Pavan"}, source="user")


def test_summary_counts_enabled_from_console_or_hand_kept_column(tmp_path):
    st = Store(tmp_path / "eap.db")
    _seed(st)
    s = service.summary(st.list())
    assert (s["requests"], s["asked_for_prompt"], s["enabled"], s["not_eligible"], s["pending"]) == (4, 3, 2, 0, 1)
    assert s["enabled_by_console"] == {"USEA3": 1, "?": 1}


def test_export_workbook_layouts(tmp_path):
    st = Store(tmp_path / "eap.db")
    _seed(st)
    out = service.export_workbook(st, tmp_path / "out" / "EAP.xlsx")
    assert out["enabled"] == 2 and (tmp_path / "out" / "EAP.xlsx").exists()
    wb = openpyxl.load_workbook(out["path"])
    assert wb.sheetnames == ["Summary", "Customer List", "Tracker"]
    cl = list(wb["Customer List"].iter_rows(values_only=True))
    assert list(cl[0]) == service.EXPORT_COLUMNS
    row1 = dict(zip(cl[0], cl[1]))
    assert row1["#"] == 1 and row1["SKU Requested 1"] == "Prompt Security" and row1["SKU Requested 2"] == "Identity Security"
    assert row1["Hyperautomation"] == "Hyperautomation" and row1["Wayfinder MDR Essential"] is None
    assert row1["1000_ Tenant (Contract)"] == "1000_USEA3_42" and row1["Enabled (Y/N)"] == "Y"
    assert row1["S1 Console/Account (from form)"] == "usea1-swprd5.sentinelone.net 42"
    assert row1["Total Agents (Complete + Control)"] == 12
    row4 = dict(zip(cl[0], cl[4]))
    assert (row4["Enabled (Y/N)"], row4["Date Enabled"], row4["Enabled By"]) == ("Y", "2026-10-01", "Pavan")
    tr = list(wb["Tracker"].iter_rows(values_only=True))
    t1 = dict(zip(tr[0], tr[1]))
    assert t1["Prompt on console"] == "Enabled" and t1["Prompt admins"] == "Jo <jo@acme.com>"
    assert t1["Tenant state"] == "confirmed by form" and t1["Requested"] == "Prompt Security; Identity Security; Hyperautomation"
    summary_cells = [c for row in wb["Summary"].iter_rows(values_only=True) for c in row if c]
    assert "Prompt enabled" in summary_cells and 2 in summary_cells
