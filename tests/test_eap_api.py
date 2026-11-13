"""Flask endpoint tests for the EAP tracker, against a temporary eap.db — no network."""
import pytest


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("S1AM_EAP_DB_PATH", str(tmp_path / "eap.db"))
    import s1am.components.app as a
    from s1am.eap import service
    st = service.open_store()
    st.upsert_form_row({"id": 3, "company": "Cheops", "contact_email": "c@cheops.com", "wants_prompt": 1,
                        "products": ["Prompt Security"], "details": {"hosts": [], "tenants": []}})
    st.update(3, {"sf_account_id": "001A", "sf_account_name": "Cheops", "match_rule": 4, "match_confidence": "medium",
                  "match_flag": "multiple contract tenants (2)", "status": "needs_review",
                  "sf_tenants": [{"id": "t1", "name": "1000_EUCE1_11", "status": "Contract", "complete_agents": 5,
                                  "control_agents": 0, "total_agents": 5},
                                 {"id": "t2", "name": "1000_EUCE2_22", "status": "Contract", "complete_agents": 0,
                                  "control_agents": 9, "total_agents": 9}]})
    return a.app.test_client(), st


def test_requests_listing_and_detail(api):
    client, _ = api
    data = client.get("/api/eap/requests").json
    assert [r["id"] for r in data["rows"]] == [3] and "sf" in data["meta"] and "consoles" in data["meta"]
    detail = client.get("/api/eap/request/3").json
    assert detail["row"]["company"] == "Cheops" and detail["history"] == []
    assert client.get("/api/eap/request/99").status_code == 404


def test_patch_rejects_non_editable_fields(api):
    client, _ = api
    assert client.patch("/api/eap/request/3", json={"sf_account_name": "Hacked"}).status_code == 400


def test_picking_a_tenant_resolves_the_review(api):
    client, st = api
    r = client.patch("/api/eap/request/3", json={"sf_tenant": "1000_EUCE2_22"})
    assert r.status_code == 200
    row = r.json["row"]
    assert row["sf_tenant"] == "1000_EUCE2_22" and row["sf_tenant_id"] == "t2" and row["total_agents"] == 9
    assert row["match_flag"] is None and row["status"] == "new" and row["status_reason"] == "awaiting console check"
    assert "sf_tenant" in row["edited_fields"]
    assert row["tenant_confirmed"] == 1 and row["tenant_confirmed_at"] and row["tenant_confirmed_by"]
    # A later enrichment must not undo the hand-picked tenant.
    st.update(3, {"sf_tenant": "1000_EUCE1_11", "match_flag": "multiple contract tenants (2)"})
    assert st.get(3)["sf_tenant"] == "1000_EUCE2_22"


def test_confirming_the_auto_selected_tenant_pins_it(api):
    client, st = api
    st.update(3, {"sf_tenant": "1000_EUCE1_11", "match_flag": None, "s1_account_name": "Cheops", "s1_console": "EUCE1",
                  "prompt_tenant_name": "cheops"})
    assert client.get("/api/eap/request/3").json["row"]["sf_tenant"] == "1000_EUCE1_11"
    row = client.patch("/api/eap/request/3", json={"tenant_confirmed": 1}).json["row"]
    assert row["tenant_confirmed"] == 1 and "sf_tenant" in row["edited_fields"]
    # A later enrichment proposing the other tenant must not win over the confirmed one.
    st.update(3, {"sf_tenant": "1000_EUCE2_22"})
    assert st.get(3)["sf_tenant"] == "1000_EUCE1_11"
    hist = client.get("/api/eap/request/3").json["history"]
    assert any(h["kind"] == "confirm" and "1000_EUCE1_11" in h["message"] for h in hist)
    # Withdrawing the confirmation frees the tenant again.
    row = client.patch("/api/eap/request/3", json={"tenant_confirmed": 0}).json["row"]
    assert not row["tenant_confirmed"] and "sf_tenant" not in row["edited_fields"] and row["tenant_confirmed_by"] is None


def test_manual_console_target_and_notes(api):
    client, _ = api
    r = client.patch("/api/eap/request/3", json={"chosen_console": "USEA3", "chosen_account_id": "123", "notes": "spoke to Tom"})
    row = r.json["row"]
    assert (row["chosen_console"], row["chosen_account_id"], row["notes"]) == ("USEA3", "123", "spoke to Tom")
    assert client.get("/api/eap/request/3").json["history"][0]["message"].startswith("Edited: chosen_account_id, chosen_console, notes")


class _FakeConsole:
    def __init__(self, accounts):
        self.accounts = {str(a["id"]): a for a in accounts}

    def request(self, method, path, params=None, payload=None):
        if path.startswith("accounts/"):
            a = self.accounts.get(path.split("/")[1])
            if not a:
                raise RuntimeError("HTTP 404")
            return {"data": a}
        return {"data": [a for i, a in self.accounts.items() if params and i == str(params.get("ids"))]}


def test_additional_tenants_lifecycle(api, monkeypatch):
    client, st = api
    import s1am.components.app as a
    from s1am.eap import service
    monkeypatch.setattr(service, "profiles", lambda: {"EUCE1": "https://e1", "EUCE2": "https://e2"})
    consoles = {"EUCE1": _FakeConsole([{"id": 11, "name": "Cheops BE", "state": "active", "licenses": {"bundles": [{"name": "complete"}]}}]),
                "EUCE2": _FakeConsole([{"id": 22, "name": "Cheops NL", "state": "active",
                                       "licenses": {"bundles": [{"name": "prompt_employees"}]},
                                       "promptFields": {"promptTenantName": "cheops_nl", "promptUserName": "T", "promptUserEmail": "t@cheops.com"}}])}
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: consoles[code])

    # Primary: pick the first; additional: add the second by name → checked immediately, already provisioned.
    client.patch("/api/eap/request/3", json={"sf_tenant": "1000_EUCE1_11"})
    r = client.post("/api/eap/request/3/tenants", json={"tenant_name": "1000_EUCE2_22"})
    assert r.status_code == 200
    extra = r.json["row"]["extra_tenants"][0]
    assert (extra["console"], extra["account_id"], extra["tenant_id"]) == ("EUCE2", "22", "t2")
    assert extra["prompt_tenant_name"] == "cheops_euce2" and extra["status"] == "provisioned"
    assert extra["s1_prompt_tenant"] == "cheops_nl" and extra["s1_prompt_users"] == [{"name": "T", "email": "t@cheops.com"}]

    # Duplicates and junk are refused; a console + account ID works too.
    assert client.post("/api/eap/request/3/tenants", json={"tenant_name": "1000_EUCE2_22"}).status_code == 400
    assert client.post("/api/eap/request/3/tenants", json={"tenant_name": "1000_EUCE1_11"}).status_code == 400
    assert client.post("/api/eap/request/3/tenants", json={"console": "EUCE1", "account_id": "abc"}).status_code == 400
    assert client.post("/api/eap/request/3/tenants", json={"console": "USEA9", "account_id": "5"}).status_code == 400
    r = client.post("/api/eap/request/3/tenants", json={"console": "EUCE1", "account_id": "999"})
    missing = r.json["row"]["extra_tenants"][1]
    assert missing["status"] == "needs_review" and "not found on EUCE1" in missing["s1_error"]

    # Edit the provisioning details of an additional tenant, then remove it.
    r = client.patch(f"/api/eap/tenants/{missing['id']}", json={"prompt_tenant_name": "cheops_be2", "first_users": [{"name": "A", "email": "a@x.io"}]})
    assert r.json["row"]["extra_tenants"][1]["prompt_tenant_name"] == "cheops_be2"
    assert client.patch(f"/api/eap/tenants/{missing['id']}", json={"console": "X"}).status_code == 400
    r = client.delete(f"/api/eap/tenants/{missing['id']}")
    assert [t["tenant_name"] for t in r.json["row"]["extra_tenants"]] == ["1000_EUCE2_22"]
    assert client.delete("/api/eap/tenants/4242").status_code == 404

    # The full console check covers additional tenants, and history records the steps.
    checked = client.post(f"/api/eap/tenants/{extra['id']}/check").json["row"]["extra_tenants"][0]
    assert checked["s1_checked_at"] and checked["status"] == "provisioned"
    kinds = [h["kind"] for h in client.get("/api/eap/request/3").json["history"]]
    assert "tenant" in kinds and "s1-check" in kinds

    # Summary and export count the extra tenant.
    s = service.summary(st.list())
    assert s["tenants"] == 2 and s["tenants_enabled"] == 1
    assert "1000_EUCE1_11 | 1000_EUCE2_22" in service.export_rows(st.list())[0]


def test_requests_meta_remembers_the_last_sync(api, tmp_path):
    client, st = api
    from s1am.eap import service
    assert client.get("/api/eap/requests").json["meta"]["last_sync_result"] is None
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["ID", "Start time", "Completion time", "Email", "Name", "Company", "Name", "Email", "Products", "Super admin", "Details"])
    ws.append([3, None, None, "c@cheops.com", "Cheops", "Cheops", "C", "c@cheops.com", "Prompt Security", "C c@cheops.com", ""])
    ws.append([7, None, None, "d@dcom.be", "Dcom", "Dcom", "D", "d@dcom.be", "Prompt Security", "D d@dcom.be", ""])
    path = tmp_path / "forms.xlsx"; wb.save(path)
    r = client.post("/api/eap/sync", json={"path": str(path)}).json
    assert r["new"] == [7] and r["rows"] == 2
    meta = client.get("/api/eap/requests").json["meta"]["last_sync_result"]
    assert meta["new"] == [7] and meta["rows"] == 2 and meta["at"]
