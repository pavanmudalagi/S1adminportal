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
    # A later enrichment must not undo the hand-picked tenant.
    st.update(3, {"sf_tenant": "1000_EUCE1_11", "match_flag": "multiple contract tenants (2)"})
    assert st.get(3)["sf_tenant"] == "1000_EUCE2_22"


def test_manual_console_target_and_notes(api):
    client, _ = api
    r = client.patch("/api/eap/request/3", json={"chosen_console": "USEA3", "chosen_account_id": "123", "notes": "spoke to Tom"})
    row = r.json["row"]
    assert (row["chosen_console"], row["chosen_account_id"], row["notes"]) == ("USEA3", "123", "spoke to Tom")
    assert client.get("/api/eap/request/3").json["history"][0]["message"].startswith("Edited: chosen_account_id, chosen_console, notes")
