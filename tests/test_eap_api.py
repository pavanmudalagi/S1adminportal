"""Flask endpoint tests for the EAP tracker, against a temporary eap.db — no network."""
import os
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


def test_candidate_tenants_get_console_account_names(api, monkeypatch):
    client, st = api
    import s1am.components.app as a
    from s1am.eap import service
    monkeypatch.setattr(service, "profiles", lambda: {"EUCE1": "https://e1"})   # EUCE2 has no profile
    consoles = {"EUCE1": _FakeConsole([{"id": 11, "name": "Cheops BE", "state": "active", "licenses": {}}])}
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: consoles[code])
    st.update(3, {"sf_tenants": st.get(3)["sf_tenants"] + [{"id": "t3", "name": "1000_EUCE1_404", "status": "Contract"}]})

    cands = client.post("/api/eap/request/3/tenant-names").json["row"]["sf_tenants"]
    by = {c["name"]: c for c in cands}
    assert (by["1000_EUCE1_11"]["s1_account_name"], by["1000_EUCE1_11"]["s1_state"]) == ("Cheops BE", "active")
    assert by["1000_EUCE2_22"]["s1_lookup"] == "no profile for console EUCE2"
    assert by["1000_EUCE1_404"]["s1_lookup"] == "account 404 not found on EUCE1"
    assert client.post("/api/eap/request/99/tenant-names").status_code == 404

    # Already-resolved candidates are not re-read unless forced.
    consoles["EUCE1"].accounts["11"]["name"] = "Renamed"
    assert {c["name"]: c.get("s1_account_name") for c in client.post("/api/eap/request/3/tenant-names").json["row"]["sf_tenants"]}["1000_EUCE1_11"] == "Cheops BE"
    assert {c["name"]: c.get("s1_account_name") for c in client.post("/api/eap/request/3/tenant-names?force=1").json["row"]["sf_tenants"]}["1000_EUCE1_11"] == "Renamed"


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


def test_sf_record_url_only_for_well_formed_ids():
    from s1am.salesforce import auth
    assert auth.record_url("https://acme.my.salesforce.com/", "0015000001G23oqAAB") == "https://acme.my.salesforce.com/0015000001G23oqAAB"
    assert auth.record_url("", "0015000001G23oqAAB") == ""
    assert auth.record_url("https://acme.my.salesforce.com", "../evil") == ""
    assert auth.ConnectionStatus(False).as_dict()["instance_url"] == ""


def test_requests_meta_carries_the_sf_instance_url(api, monkeypatch):
    client, _ = api
    from s1am.salesforce import auth
    monkeypatch.setattr(auth, "load_token", lambda: {"access_token": "x", "instance_url": "https://acme.my.salesforce.com", "id": ""})
    assert client.get("/api/eap/requests").json["meta"]["sf"]["instance_url"] == "https://acme.my.salesforce.com"


def test_manual_request_is_created_above_the_forms_id_range(api, monkeypatch):
    client, st = api
    from s1am.eap import service, store as store_mod
    monkeypatch.setattr(service, "profiles", lambda: {"USEA3": "https://u3"})
    monkeypatch.setattr(service, "actor", lambda: "pavan")
    r = client.post("/api/eap/requests", json={"company": "Zeta Ltd", "contact_name": "Zed", "contact_email": "zed@zeta.io",
                                              "products": ["Prompt Security", "Identity Security"],
                                              "details_raw": "usea1-swprd3.sentinelone.net account 123456789012345678",
                                              "sf_tenant": "1000_usea3_123456789012345678", "notes": "asked by email"})
    assert r.status_code == 201, r.json
    row = r.json["row"]
    assert row["id"] == store_mod.MANUAL_ID_BASE + 1 and row["origin"] == "manual" and row["wants_prompt"] == 1
    assert row["super_admin_email"] == "zed@zeta.io" and row["super_admin_source"] == "manual"
    assert row["details"]["hosts"] == ["usea1-swprd3.sentinelone.net"] and row["details"]["account_ids"] == ["123456789012345678"]
    assert row["sf_tenant"] == "1000_USEA3_123456789012345678" and row["tenant_confirmed"] == 1 and row["tenant_confirmed_by"] == "pavan"
    assert row["prompt_tenant_name"] and row["first_users"][0]["email"] == "zed@zeta.io" and row["notes"] == "asked by email"
    assert row["status"] == "needs_review" and "Salesforce" in row["status_reason"]  # until Enrich Salesforce runs
    hist = client.get(f"/api/eap/request/{row['id']}").json["history"]
    assert hist[0]["kind"] == "manual" and "pavan" in hist[0]["message"]
    # Second one gets the next id; the listing shows both the synced row and the manual ones.
    r2 = client.post("/api/eap/requests", json={"company": "Eta", "chosen_console": "USEA3", "chosen_account_id": "42"})
    assert r2.json["row"]["id"] == store_mod.MANUAL_ID_BASE + 2 and r2.json["row"]["chosen_console"] == "USEA3"
    assert sorted(x["id"] for x in client.get("/api/eap/requests").json["rows"]) == [3, store_mod.MANUAL_ID_BASE + 1, store_mod.MANUAL_ID_BASE + 2]


def test_manual_request_validation(api, monkeypatch):
    client, _ = api
    from s1am.eap import service
    monkeypatch.setattr(service, "profiles", lambda: {"USEA3": "https://u3"})
    bad = lambda body: client.post("/api/eap/requests", json=body)
    assert bad({"company": ""}).status_code == 400
    assert bad({"company": "X", "contact_email": "nope"}).status_code == 400
    assert bad({"company": "X", "products": ["Nonsense"]}).status_code == 400
    assert bad({"company": "X", "sf_tenant": "ACME"}).status_code == 400
    assert bad({"company": "X", "chosen_console": "NOPE", "chosen_account_id": "1"}).status_code == 400
    assert bad({"company": "X", "chosen_console": "USEA3", "chosen_account_id": "abc"}).status_code == 400
    ok = bad({"company": "X"}).json["row"]
    assert ok["products"] == ["Prompt Security"] and ok["super_admin_needs_review"] == 1


def test_sync_leaves_manual_rows_alone(api, tmp_path, monkeypatch):
    client, st = api
    from s1am.eap import service, store as store_mod
    monkeypatch.setattr(service, "actor", lambda: "pavan")
    rid = client.post("/api/eap/requests", json={"company": "Manual Co"}).json["row"]["id"]
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["ID", "Start time", "Completion time", "Email", "Name", "Company", "Name", "Email", "Products", "Super admin", "Details"])
    ws.append([3, None, None, "c@cheops.com", "Cheops", "Cheops", "C", "c@cheops.com", "Prompt Security", "C c@cheops.com", ""])
    path = tmp_path / "forms.xlsx"; wb.save(path)
    client.post("/api/eap/sync", json={"path": str(path)})
    row = st.get(rid)
    assert row["company"] == "Manual Co" and row["origin"] == "manual" and rid == store_mod.MANUAL_ID_BASE + 1


def _workbook(tmp_path, rows):
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["ID", "Start time", "Completion time", "Email", "Name1", "Company", "Contact Email",
               "Which products are you interested in?", "Super admin (name and email)", "Please provide your console / account details"])
    for r in rows:
        ws.append(r)
    path = tmp_path / "forms.xlsx"; wb.save(path)
    return str(path)


def test_identical_manual_row_is_merged_into_the_form_response_on_sync(api, tmp_path, monkeypatch):
    client, st = api
    from s1am.eap import service
    monkeypatch.setattr(service, "actor", lambda: "pavan")
    monkeypatch.setattr(service, "profiles", lambda: {"USEA3": "https://u3"})
    m = client.post("/api/eap/requests", json={"company": "Dcom", "contact_name": "D", "contact_email": "d@dcom.be",
                                              "sf_tenant": "1000_USEA3_42", "notes": "called us first", "chosen_console": "USEA3", "chosen_account_id": "42"}).json["row"]
    client.post(f"/api/eap/request/{m['id']}/tenants", json={"console": "USEA3", "account_id": "77"})  # fails the check but creates the extra tenant
    path = _workbook(tmp_path, [[3, None, None, "c@cheops.com", "C", "Cheops", "c@cheops.com", "Prompt Security", "C c@cheops.com", ""],
                                 [7, None, None, "d@dcom.be", "D", "Dcom", "d@dcom.be", "Prompt Security", "D d@dcom.be", ""]])
    r = client.post("/api/eap/sync", json={"path": path}).json
    assert r["new"] == [7] and r["merged"] == [{"manual_id": m["id"], "form_id": 7, "company": "Dcom"}] and r["duplicates"] == []
    rows = {x["id"]: x for x in client.get("/api/eap/requests").json["rows"]}
    assert m["id"] not in rows  # hidden once merged
    f = rows[7]
    assert f["sf_tenant"] == "1000_USEA3_42" and f["tenant_confirmed"] == 1 and f["notes"] == "called us first"
    assert (f["chosen_console"], f["chosen_account_id"]) == ("USEA3", "42") and f["origin"] is None
    assert [t["account_id"] for t in f["extra_tenants"]] == ["77"]
    hist = client.get("/api/eap/request/7").json["history"]
    assert any(h["kind"] == "duplicate" and f"#{m['id']}" in h["message"] for h in hist) and any(h["kind"] == "manual" for h in hist)
    assert st.get(m["id"])["merged_into"] == 7 and st.get(m["id"])["status"] == "merged"
    assert client.get("/api/eap/requests").json["meta"]["last_sync_result"]["merged"][0]["form_id"] == 7


def test_differing_manual_row_is_reported_and_resolved_by_the_user(api, tmp_path, monkeypatch):
    client, st = api
    from s1am.eap import service
    monkeypatch.setattr(service, "actor", lambda: "pavan")
    m = client.post("/api/eap/requests", json={"company": "Dcom NV", "contact_name": "Daniel", "contact_email": "d@dcom.be",
                                              "products": ["Prompt Security", "Identity Security"]}).json["row"]
    path = _workbook(tmp_path, [[7, None, None, "d@dcom.be", "D", "Dcom", "d@dcom.be", "Prompt Security", "D d@dcom.be", "usea1-swprd3.sentinelone.net"]])
    r = client.post("/api/eap/sync", json={"path": path}).json
    assert r["merged"] == [] and len(r["duplicates"]) == 1
    d = r["duplicates"][0]
    assert (d["manual_id"], d["form_id"], d["identical"]) == (m["id"], 7, False) and "email: d@dcom.be" in d["matched_on"]
    assert {x["field"] for x in d["differences"]} == {"company", "contact_name", "super_admin_name", "products", "details"}
    assert client.get("/api/eap/requests").json["meta"]["duplicates"][0]["form_id"] == 7
    # Keep the hand-entered details: they overwrite the form row and survive the next sync.
    res = client.post("/api/eap/duplicates/resolve", json={"manual_id": m["id"], "form_id": 7, "action": "manual"}).json
    assert res["duplicates"] == [] and res["row"]["company"] == "Dcom NV" and res["row"]["products"] == ["Prompt Security", "Identity Security"]
    client.post("/api/eap/sync", json={"path": path})
    assert st.get(7)["company"] == "Dcom NV" and st.get(m["id"])["merged_into"] == 7
    assert client.post("/api/eap/duplicates/resolve", json={"manual_id": m["id"], "form_id": 7, "action": "bogus"}).status_code == 400
    assert client.post("/api/eap/duplicates/resolve", json={"manual_id": 999, "form_id": 7, "action": "form"}).status_code == 404


def test_not_a_duplicate_is_remembered(api, tmp_path, monkeypatch):
    client, st = api
    from s1am.eap import service
    monkeypatch.setattr(service, "actor", lambda: "pavan")
    m = client.post("/api/eap/requests", json={"company": "Cheops Technology", "contact_email": "c@cheops.com"}).json["row"]
    assert client.get("/api/eap/duplicates").json["duplicates"][0]["form_id"] == 3  # same contact email as the seeded row
    client.post("/api/eap/duplicates/resolve", json={"manual_id": m["id"], "form_id": 3, "action": "ignore"})
    assert client.get("/api/eap/duplicates").json["duplicates"] == []
    assert st.get(m["id"])["merged_into"] is None and len(client.get("/api/eap/requests").json["rows"]) == 2


# ---------------------------------------------------------------- workbook discovery

class TestFormPathDiscovery:
    def _touch(self, folder, name, mtime):
        p = folder / name
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))
        return p

    def test_download_number_parses_forms_export_names(self):
        from s1am.eap import service as svc
        base = "N-able Endpoint SecuritySentinelOne Early Access"
        assert svc.download_number(f"{base}(1-53).xlsx") == 53
        assert svc.download_number(f"{base} (1-7).xlsx") == 7
        assert svc.download_number(f"{base}(1-53) (1).xlsx") == 53   # browser re-download suffix
        assert svc.download_number(f"{base}.xlsx") == 0
        assert svc.download_number(f"~${base}(1-53).xlsx") is None     # Excel lock file
        assert svc.download_number("EAP - Customer List.xlsx") is None
        assert svc.download_number(f"{base}(1-53).csv") is None

    def test_highest_response_number_wins_over_newer_mtime(self, tmp_path, monkeypatch):
        from s1am.eap import service as svc
        monkeypatch.setenv("S1AM_EAP_DOWNLOADS_DIR", str(tmp_path))
        monkeypatch.delenv("S1AM_EAP_FORM_PATH", raising=False)
        base = "N-able Endpoint SecuritySentinelOne Early Access"
        self._touch(tmp_path, f"{base}(1-41).xlsx", 1_700_000_000)
        best = self._touch(tmp_path, f"{base}(1-53).xlsx", 1_700_000_100)
        self._touch(tmp_path, f"{base}(1-43).xlsx", 1_700_000_900)      # newer file, lower count
        self._touch(tmp_path, f"~${base}(1-53).xlsx", 1_700_001_000)     # lock file ignored
        self._touch(tmp_path, "Other Early Access(1-99).xlsx", 1_700_001_000)
        assert [p.name for p in svc.download_candidates()] == [
            f"{base}(1-53).xlsx", f"{base}(1-43).xlsx", f"{base}(1-41).xlsx"]
        assert svc.form_path() == best

    def test_same_number_falls_back_to_mtime(self, tmp_path, monkeypatch):
        from s1am.eap import service as svc
        monkeypatch.setenv("S1AM_EAP_DOWNLOADS_DIR", str(tmp_path))
        monkeypatch.delenv("S1AM_EAP_FORM_PATH", raising=False)
        base = "N-able Endpoint SecuritySentinelOne Early Access"
        self._touch(tmp_path, f"{base}(1-53).xlsx", 1_700_000_000)
        again = self._touch(tmp_path, f"{base}(1-53) (1).xlsx", 1_700_000_500)
        assert svc.form_path() == again

    def test_explicit_form_path_beats_downloads(self, tmp_path, monkeypatch):
        from s1am.eap import service as svc
        monkeypatch.setenv("S1AM_EAP_DOWNLOADS_DIR", str(tmp_path))
        self._touch(tmp_path, "N-able Endpoint SecuritySentinelOne Early Access(1-53).xlsx", 1_700_000_000)
        monkeypatch.setenv("S1AM_EAP_FORM_PATH", str(tmp_path / "pinned.xlsx"))
        assert svc.form_path() == tmp_path / "pinned.xlsx"

    def test_empty_downloads_falls_back_to_synced_copy(self, tmp_path, monkeypatch):
        from s1am.eap import service as svc
        monkeypatch.setenv("S1AM_EAP_DOWNLOADS_DIR", str(tmp_path / "missing"))
        monkeypatch.delenv("S1AM_EAP_FORM_PATH", raising=False)
        synced = tmp_path / "synced.xlsx"
        monkeypatch.setattr(svc, "SYNCED_COPY", synced)
        assert svc.form_path() is None
        synced.write_bytes(b"x")
        assert svc.form_path() == synced


# ---------------------------------------------------------------- customer email draft

class TestCustomerEmail:
    TEAM = [
        {"AccountId": "001A", "TeamMemberRole": "Technical Account Owner", "User.Name": "Brett Bell", "User.Email": "bb@n-able.com", "User.IsActive": True},
        {"AccountId": "001A", "TeamMemberRole": "Customer Success", "User.Name": "Pat Conley", "User.Email": "pc@n-able.com", "User.IsActive": True},
        {"AccountId": "001A", "TeamMemberRole": "Growth Strategist", "User.Name": "Rick C", "User.Email": "rc@n-able.com", "User.IsActive": True},
        {"AccountId": "001A", "TeamMemberRole": "Backup PSE", "User.Name": "Andre B", "User.Email": "ab@n-able.com", "User.IsActive": True},
        {"AccountId": "001A", "TeamMemberRole": "Tools AE", "User.Name": "Gone", "User.Email": "gone@n-able.com", "User.IsActive": False},
        {"AccountId": "001A", "TeamMemberRole": "Security AE", "User.Name": "No Mail", "User.Email": None, "User.IsActive": True},
    ]

    def test_team_keeps_active_members_with_email(self):
        from s1am.eap import email as em
        team = em.team_from_salesforce(self.TEAM)
        assert [t["role"] for t in team] == ["Technical Account Owner", "Customer Success", "Growth Strategist", "Backup PSE"]

    def test_recipients_defaults(self):
        from s1am.eap import email as em
        row = {"contact_name": "Leslie Rudolph", "contact_email": "l@ballast.com", "super_admin_name": "D J",
               "super_admin_email": "dj@ballast.com", "sf_account_id": "001A"}
        rec = em.recipients(row, em.team_from_salesforce(self.TEAM))
        assert [t["email"] for t in rec["to"]] == ["l@ballast.com"]
        by_role = {c["role"]: c for c in rec["cc"]}
        assert by_role["Super admin"]["checked"] and by_role["Always copied"]["email"] == "Joe.Kern@n-able.com"
        assert by_role["Partner Success"]["checked"]           # label for "Customer Success"
        assert by_role["Technical Account Owner"]["checked"] and by_role["Growth Strategist"]["checked"]
        assert by_role["Backup PSE"]["checked"] is False      # offered, not ticked
        assert [c["role"] for c in rec["cc"]][-1] == "Backup PSE"  # unticked roles sort last
        assert rec["missing"] == []

    def test_recipients_dedupe_and_warnings(self):
        from s1am.eap import email as em
        row = {"contact_email": "joe.kern@n-able.com", "super_admin_email": "JOE.KERN@n-able.com"}
        rec = em.recipients(row, [])
        assert rec["cc"] == []                                 # contact == Joe == super admin
        assert rec["missing"] == ["No Salesforce account matched yet"]
        assert em.recipients({"sf_account_id": "001A"}, [])["missing"] == [
            "The request has no contact email", "No Salesforce account team found"]

    def test_html_to_text_and_escaping(self):
        from s1am.eap import email as em
        markup = ("<div><p>Hello {contact_name},</p><p><b>Guide:</b> <a href=\"https://x.test/g\">https://x.test/g</a> (login)</p>"
                  "<ul><li>One</li><li>Two <a href=\"https://x.test/z\">zip</a>.</li></ul><ol><li>First<ul><li>Nested</li></ul></li></ol><p>Bye</p></div>")
        rendered = em.render(markup, {"contact_name": "Smith & <Sons>"}, escape=True)
        assert "Smith &amp; &lt;Sons&gt;" in rendered
        text = em.html_to_text(rendered)
        assert text == ("Hello Smith & <Sons>,\n\nGuide: https://x.test/g (login)\n\n- One\n- Two zip (https://x.test/z).\n\n"
                        "1. First\n   - Nested\n\nBye\n")

    def test_render_and_context(self):
        from s1am.eap import email as em
        row = {"company": "Ballast", "contact_name": "Leslie Rudolph", "sf_tenant": "1000_USEA2_9",
               "prompt_tenant_name": "ballast", "first_users": [{"name": "L R", "email": "l@b.com"}], "products": ["Prompt Security"]}
        ctx = em.context(row, {"console": "USEA2", "account_id": "9", "host": "usea1-swprd2.sentinelone.net"}, sender="Pavan")
        assert ctx["contact_first_name"] == "Leslie" and ctx["tenant_clause"] == " (tenant 1000_USEA2_9)"
        assert ctx["console_url"] == "https://usea1-swprd2.sentinelone.net" and ctx["first_users"] == "L R <l@b.com>"
        assert em.render("Hi {contact_first_name}, {nope} {console}", ctx) == "Hi Leslie,  USEA2"
        assert em.unknown_placeholders("{company} {nope}") == ["nope"]
        with pytest.raises(ValueError):
            em.split_template("no subject line")
        assert em.split_template("Subject: S\n\nBody {x}") == ("S", "Body {x}")

    def test_links(self):
        from s1am.eap import email as em
        link = em.outlook_link(["a@b.com"], ["c@d.com", "e@f.com"], "Hi & bye", "line 1\nline 2")
        assert link.startswith("https://outlook.office.com/mail/deeplink/compose?to=a%40b.com&cc=c%40d.com%2Ce%40f.com&subject=Hi%20%26%20bye&body=line%201%0Aline%202")
        assert em.mailto_link(["a@b.com"], [], "S", "B") == "mailto:a@b.com?subject=S&body=B"

    def test_draft_route_uses_cached_team_and_records_the_send(self, api, monkeypatch):
        client, st = api
        from s1am.eap import service as svc, email as em
        st.update(3, {"contact_name": "Chris Cheops", "sf_team": em.team_from_salesforce(self.TEAM), "sf_team_at": "2026-11-18 10:00:00",
                      "sf_tenant": "1000_EUCE1_11", "enabled_yn": "Y"})
        monkeypatch.setattr(svc, "refresh_team", lambda st_, rid: st_.get(rid)["sf_team"])  # no Salesforce in tests
        monkeypatch.setattr(svc, "sender_name", lambda st_: "Pavan")
        monkeypatch.setattr(svc, "sender_email", lambda st_: "pavan@example.com")
        d = client.get("/api/eap/request/3/email").json
        assert d["to"][0]["email"] == "c@cheops.com"
        assert d["subject"] == "SentinelOne Prompt Security Access Activated"
        assert d["body"].startswith("Hello Chris Cheops,\n") and "{" not in d["body"]
        assert d["body"].rstrip().endswith("Thank you,")   # the mail client appends the sender's own signature
        assert d["body_html"].startswith("<div") and "<b>Prompt Security</b>" in d["body_html"]
        assert 'href="https://me.n-able.com/s/edr-sa-documentation"' in d["body_html"]
        assert "Prompt+Security+EDR+install.zip" in d["body_html"] and "remoteops-scripts" in d["body_html"]
        assert d["warnings"] == [] and d["template"]["custom"] is False
        assert [c["email"] for c in d["cc"] if c["checked"]] == ["Joe.Kern@n-able.com", "bb@n-able.com", "pc@n-able.com", "rc@n-able.com"]

        bad = client.post("/api/eap/request/3/email", json={"to": ["nope"], "cc": [], "subject": "x"})
        assert bad.status_code == 400 and "Not an email address" in bad.json["error"]
        ok = client.post("/api/eap/request/3/email", json={"to": ["c@cheops.com"], "cc": ["Joe.Kern@n-able.com"], "subject": "S", "via": "Outlook on the web"})
        assert ok.status_code == 200 and ok.json["row"]["email_drafted_at"]
        hist = ok.json["history"][0]
        assert hist["kind"] == "email" and "Outlook on the web" in hist["message"] and hist["data"]["cc"] == ["Joe.Kern@n-able.com"]
        assert client.get("/api/eap/request/99/email").status_code == 404

    def test_template_route_saves_custom_copy(self, api, tmp_path, monkeypatch):
        client, _ = api
        from s1am.eap import email as em
        monkeypatch.setattr(em, "CUSTOM_TEMPLATE", tmp_path / "tpl.txt")
        t = client.get("/api/eap/email-template").json
        assert t["custom"] is False and "company" in t["placeholders"] and t["text"].startswith("Subject:")
        assert client.put("/api/eap/email-template", json={"text": "no subject"}).status_code == 400
        t = client.put("/api/eap/email-template", json={"text": "Subject: Hello {company}\n\n<p>Body <b>{bogus}</b></p>"}).json
        assert t["custom"] is True and t["unknown"] == ["bogus"] and (tmp_path / "tpl.txt").exists()
        d = client.get("/api/eap/request/3/email?refresh=0").json
        assert d["subject"] == "Hello Cheops" and d["body_html"] == "<p>Body <b></b></p>" and d["body"] == "Body\n"
        t = client.put("/api/eap/email-template", json={"text": ""}).json
        assert t["custom"] is False and not (tmp_path / "tpl.txt").exists()
