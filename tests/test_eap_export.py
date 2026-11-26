"""Export workbook and summary numbers — written to a temp file, no network."""
import openpyxl
from pathlib import Path

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
                  "s1_prompt_users": [{"name": "Jo", "email": "jo@acme.com"}], "status": "provisioned", "notes": "internal: call Jo",
                  "sf_team": [{"name": "Sam Seller", "email": "sam@n-able.com", "role": "Account Manager"}],
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
    assert t1["SF account team"] == "Sam Seller (Account Manager) <sam@n-able.com>" and t1["Notes"] == "internal: call Jo"
    assert t1["Tenant state"] == "confirmed by form" and t1["Requested"] == "Prompt Security; Identity Security; Hyperautomation"
    summary_cells = [c for row in wb["Summary"].iter_rows(values_only=True) for c in row if c]
    assert "Prompt enabled" in summary_cells and 2 in summary_cells


def test_match_label_follows_confirmation_and_provisioning():
    guess = {"match_confidence": "medium"}
    assert service.match_label(guess) == "medium"
    assert service.match_label({**guess, "tenant_confirmed": 1}) == "confirmed"
    assert service.match_label({**guess, "chosen_console": "USEA3", "chosen_account_id": "1"}) == "confirmed"
    assert service.match_label({**guess, "tenant_confirmed": 1, "status": "provisioned", "s1_prompt_enabled": 1}) == "verified"
    assert service.match_label({**guess, "status": "provisioned", "s1_prompt_enabled": 0}) == "medium"


def test_tracker_match_column_uses_the_settled_label(tmp_path):
    st = Store(tmp_path / "eap.db")
    _seed(st)
    st.update(2, {"match_confidence": "medium", "tenant_confirmed": 1})
    out = service.export_workbook(st, tmp_path / "EAP.xlsx")
    tr = list(openpyxl.load_workbook(out["path"])["Tracker"].iter_rows(values_only=True))
    rows = [dict(zip(tr[0], line)) for line in tr[1:]]
    assert rows[0]["Match"] == "verified" and rows[1]["Match"] == "confirmed"


def test_every_export_column_belongs_to_exactly_one_group():
    for master, key in ((service.EXPORT_COLUMNS, "list"), ([l for l, _ in service.TRACKER_COLUMNS], "tracker")):
        seen = [c for g in service.EXPORT_GROUPS for c in g[key]]
        assert sorted(seen) == sorted(master), key
    full_list, full_tr = service.export_columns(level="full")
    assert full_list == service.EXPORT_COLUMNS and full_tr == service.TRACKER_COLUMNS
    assert service.export_options()["presets"] == {"full": [g["key"] for g in service.EXPORT_GROUPS], "share": ["basics", "console", "agents"]}


def test_shareable_export_leaves_out_identifiers_emails_admins_team_and_notes(tmp_path):
    st = Store(tmp_path / "eap.db")
    _seed(st)
    out = service.export_workbook(st, tmp_path / "share.xlsx", level="share")
    wb = openpyxl.load_workbook(out["path"])
    assert wb.sheetnames == ["Summary", "Customer List", "Tracker"]
    cl = list(wb["Customer List"].iter_rows(values_only=True))
    assert list(cl[0]) == ["#", "Contact Name", "Company (Form)", "SF Account Name", "Country", "SKU Requested 1", "SKU Requested 2",
                           "Wayfinder MDR Essential", "Hyperautomation", "First contacted", "Enabled (Y/N)", "Date Enabled",
                           "Complete Agents", "Control Agents", "Total Agents (Complete + Control)"]
    row1 = dict(zip(cl[0], cl[1]))
    assert row1["Company (Form)"] == "Acme" and row1["SF Account Name"] == "Acme Pty Ltd" and row1["Enabled (Y/N)"] == "Y" and row1["Total Agents (Complete + Control)"] == 12
    tr = list(wb["Tracker"].iter_rows(values_only=True))
    assert {"Company", "Contact", "Console", "Status", "Prompt on console", "Total agents", "Enabled (Y/N)"} <= set(tr[0])
    assert {"Contact Email", "S1 Account ID", "Prompt admins", "SF account team", "Notes", "Match"}.isdisjoint(tr[0])
    text = " ".join(str(c) for sheet in wb for row in sheet.iter_rows(values_only=True) for c in row if c)
    for secret in ("jo@acme.com", "sam@n-able.com", "Sam Seller", "1000_USEA3_42", "internal: call Jo", "acme"):
        assert secret not in text.replace("Acme", ""), secret
    assert "Left out: Contact emails, Identifiers, Prompt admins, Salesforce account team, Internal workflow" in text


def test_export_picks_content_groups_and_sheets(tmp_path):
    st = Store(tmp_path / "eap.db")
    _seed(st)
    out = service.export_workbook(st, tmp_path / "pick.xlsx", groups=["ids", "admins"], sheets=["tracker"])
    wb = openpyxl.load_workbook(out["path"])
    assert wb.sheetnames == ["Tracker"]
    head = list(next(wb["Tracker"].iter_rows(values_only=True)))
    assert "S1 Account ID" in head and "Prompt admins" in head and "Company" in head      # basics always come along
    assert "Contact Email" not in head and "SF account team" not in head and "Notes" not in head
    import pytest
    with pytest.raises(ValueError):
        service.export_columns(["basics", "secrets"])
    with pytest.raises(ValueError):
        service.export_workbook(st, tmp_path / "x.xlsx", sheets=["pivot"])
    assert service.export_default_path("share").name.endswith("(selected content).xlsx")
    assert service.export_default_path(groups=["ids", "admins"]).name.endswith("(selected content).xlsx")
    assert "(selected" not in service.export_default_path().name and "(selected" not in service.export_default_path(groups=[g["key"] for g in service.EXPORT_GROUPS]).name


# ── backup / restore ────────────────────────────────────────────────────────

def test_backup_restores_the_whole_tracker_exactly(tmp_path, monkeypatch):
    from s1am.eap import email as email_mod
    monkeypatch.setattr(email_mod, "CUSTOM_TEMPLATE", tmp_path / "tpl.html")
    monkeypatch.setattr(service, "setting", lambda key: {"backups_dir": str(tmp_path / "backups")}.get(key, ""))
    st = Store(tmp_path / "eap.db")
    _seed(st)
    st.add_tenant(1, {"tenant_name": "1000_USEA3_43", "console": "USEA3", "account_id": "43", "status": "pending", "first_users": [{"name": "Al"}]})
    st.add_history(1, "note", "called Jo", {"by": "me"})
    st.audit("pavan", "USEA3", "42", "enable_prompt", True, {"x": 1}, "dry run")
    st.set_meta("last_sync", "2026-11-26 10:00:00")
    email_mod.save_template("Subject: Hi {company}\n\nBody {contact_name}")
    before = st.dump_tables()
    data = service.backup_data(st)
    assert data["format"] == service.BACKUP_FORMAT and data["counts"] == {"requests": 4, "request_tenants": 1, "history": 1, "audit": 1, "meta": 1}
    assert data["files"]["email_template"].startswith("Subject: Hi")
    out = service.write_backup(st)
    assert out["path"].startswith(str(tmp_path / "backups")) and out["bytes"] > 100
    # wreck the tracker, then restore
    st.update(1, {"notes": "changed"}, source="user"); st.delete_tenant(st.tenants(1)[0]["id"]); st.set_meta("last_sync", "later")
    st.upsert_form_row({"id": 9, "company": "Intruder", "wants_prompt": 1, "products": [], "details": {}})
    email_mod.save_template("")
    import json
    saved = json.loads(Path(out["path"]).read_text())
    preview = service.restore_backup(st, saved)
    assert preview["applied"] is False and preview["file"]["requests"] == 4 and preview["current"]["requests"] == 5 and preview["email_template"] == "custom"
    assert st.counts()["requests"] == 5                                  # a preview changes nothing
    res = service.restore_backup(st, saved, apply=True)
    assert res["applied"] and res["restored"] == data["counts"] and res["skipped_columns"] == {} and "(before restore)" in res["safety_copy"]
    after = st.dump_tables()
    assert {t: after[t] for t in ("requests", "request_tenants", "audit", "meta")} == {t: before[t] for t in ("requests", "request_tenants", "audit", "meta")}
    assert after["history"][:1] == before["history"] and after["history"][-1]["kind"] == "restore"   # plus the restore line itself
    assert st.get(9) is None and st.get(1)["notes"] == "internal: call Jo" and st.tenants(1)[0]["first_users"] == [{"name": "Al"}]
    assert email_mod.load_template()["custom"] and "Hi {company}" in email_mod.load_template()["text"]
    # the safety copy holds the wrecked state, so a restore can itself be undone
    safety = json.loads(Path(res["safety_copy"]).read_text())
    assert safety["counts"]["requests"] == 5 and safety["files"]["email_template"] is None
    # new ids after a restore continue past the restored ones
    st.add_history(2, "note", "again")
    assert st.history(2)[0]["id"] > before["history"][-1]["id"]


def test_restore_rejects_bad_files_and_skips_unknown_columns(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(service, "setting", lambda key: {"backups_dir": str(tmp_path / "backups")}.get(key, ""))
    st = Store(tmp_path / "eap.db")
    _seed(st)
    for bad in ({"format": "other"}, {"format": service.BACKUP_FORMAT, "version": 99, "tables": {"requests": []}},
                {"format": service.BACKUP_FORMAT, "version": 1, "tables": {"requests": "nope"}}, [], "text"):
        with pytest.raises(ValueError):
            service.restore_backup(st, bad, apply=True)
    assert st.counts()["requests"] == 4
    data = service.backup_data(st)
    data["tables"]["requests"][0]["future_column"] = "x"
    res = service.restore_backup(st, data, apply=True)
    assert res["skipped_columns"] == {"requests": ["future_column"]} and st.counts()["requests"] == 4
