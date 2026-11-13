"""Marketplace connector tracker — parsing from a saved Staging payload, per-console failure handling,
views, masking and the Flask routes. No network."""
import json
from pathlib import Path

import pytest

from s1am.api.exceptions import SentinelOneError
from s1am.marketplace import service as mp

FIX = Path(__file__).parent / "fixtures"
APPS = json.loads((FIX / "marketplace_applications.json").read_text())
CATALOG = json.loads((FIX / "marketplace_catalog.json").read_text())
SCOPES = json.loads((FIX / "marketplace_scopes.json").read_text())   # 2 accounts + 2 sites with licenses.settings


class FakeClient:
    """Serves the fixtures; can be told to fail a path with a status code or to 429 N times first."""
    def __init__(self, fail=None, throttle=0):
        self.fail, self.throttle, self.calls = fail or {}, throttle, []

    def request(self, method, path, params=None, payload=None):
        self.calls.append((path, dict(params or {})))
        assert method == "GET"
        if self.throttle:
            self.throttle -= 1
            raise SentinelOneError("HTTP 429", status_code=429, payload={"message": "slow down"})
        for prefix, code in self.fail.items():
            if path.startswith(prefix):
                raise SentinelOneError(f"HTTP {code}", status_code=code, payload={"message": "Forbidden - missing permission Singularity Marketplace.view" if code == 403 else "nope"})
        if path == mp.CATALOG_PATH:
            assert not ("cursor" in params and "limit" in params), "cursor must be sent alone"
            return CATALOG
        if path == mp.APPS_PATH:
            return APPS
        if path == "accounts":
            ids = set((params or {}).get("ids", "").split(","))
            return {"data": [a for a in SCOPES["accounts"]["data"] if a["id"] in ids]}
        if path == "sites":
            ids = set((params or {}).get("siteIds", "").split(","))
            return {"data": {"sites": [s for s in SCOPES["sites"]["data"]["sites"] if s["id"] in ids]}}
        if path.endswith("/log"):
            return {"data": [{"createdAt": "2026-11-13T06:46:08Z", "status": "Retry", "httpStatus": 500, "message": "Can't reach service", "traceId": "t1"}]}
        if path.endswith("/config"):
            return {"data": {"id": "1", "applicationInstanceName": "Mimecast", "fields": [
                {"id": "appId", "type": "text", "label": "Application ID", "value": "d303"},
                {"id": "appKey", "type": "secret", "label": "Application Key", "value": "****"},
                {"id": "client_token_thing", "type": "text", "label": "Token", "value": "abc123"},
                {"id": "is_simulation_mode", "type": "checkbox", "label": "Simulation", "value": "false"}]}}
        raise AssertionError(path)


def test_parse_applications_flattens_scopes_with_site_and_level():
    rows = mp.parse_applications(APPS, "Staging", mp.parse_catalog(CATALOG))
    assert len(rows) == 18 and {r["scope_level"] for r in rows} == {"site", "account", "global"}
    assert set(mp.INSTANCE_FIELDS) == set(rows[0].keys())
    site = next(r for r in rows if r["app_name"] == "Alien Labs OTX Enrichment")
    assert (site["scope_level"], site["site"], site["account"], site["desired_status"], site["status"]) == ("site", "Support Training", "Apolando", "disabled", "disabled")
    assert site["site_id"] == site["scope_id"]
    acct = next(r for r in rows if r["app_name"] == "Microsoft Entra ID")
    assert acct["scope_level"] == "account" and acct["site"] is None and acct["site_id"] is None
    glob = next(r for r in rows if r["scope_level"] == "global")
    assert glob["account_id"] == mp.GLOBAL_ACCOUNT_ID and glob["account"] == "Global scope"
    failed = next(r for r in rows if r["status"] == "activationfailed")
    assert failed["desired_status"] == "active" and failed["has_alert"] and failed["alert_message"]
    assert "icon" not in json.dumps(rows)
    # Categories come from the catalog.
    assert site["category"] == "Threat Intelligence" and site["app_type"]


def test_retention_parsing_and_inheritance():
    nable, pavan = SCOPES["accounts"]["data"]
    r = mp.parse_retention(nable, "account")
    assert (r["value"], r["days"], r["unit"], r["level"], r["status"]) == ("90 Days", 90, "days", "account", "ok")
    assert r["ingest_days"] == [30, 90, 180, 365] and r["source"] == mp.RETENTION_SOURCE
    assert mp.parse_retention(pavan, "account")["days"] == 14
    support, apo = SCOPES["sites"]["data"]["sites"]          # Support Training has 90 Days; Apo Site has no entry
    s = mp.derive_site_inheritance(mp.parse_retention(support, "site"), r)
    assert s["display"] == "90 Days" and s["inherited"] is True and "same as account" in s["note"]
    s2 = mp.derive_site_inheritance(mp.parse_retention(support, "site"), mp.parse_retention(pavan, "account"))
    assert s2["inherited"] is False and "overrides account (14 Days)" in s2["note"]
    none = mp.derive_site_inheritance(mp.parse_retention(apo, "site"), r)
    assert none["status"] == "not_exposed" and none["display"] == "not exposed by API" and none["inherited"] is None and none["account_value"] == "90 Days"
    assert mp.parse_period("1 Year") == (365, "years") and mp.parse_period("2 weeks") == (14, "weeks") and mp.parse_period("forever") == (None, None)
    assert mp.retention_matches({"status": "ok", "days": 14}, "lt:30") and not mp.retention_matches({"status": "ok", "days": 90}, "lt:30")
    assert mp.retention_matches({"status": "not_exposed"}, "none") and not mp.retention_matches(None, "eq:90") and mp.retention_matches(None, "none")


def test_fetch_console_attaches_retention_from_accounts_and_sites():
    c = FakeClient()
    r = mp.fetch_console(c, "Staging", sleep=lambda s: None)
    assert r["ok"] and "retention_error" not in r
    acc_calls = [p for p, _ in c.calls if p == "accounts"]; site_calls = [p for p, _ in c.calls if p == "sites"]
    assert acc_calls and site_calls                                        # only the referenced ids, chunked
    by_site = {i["site"]: i for i in r["instances"] if i["site"]}
    st = by_site["Support Training"]            # site in the fixture (90 Days); its account Apolando is not
    assert st["site_retention"]["display"] == "90 Days" and st["site_retention"]["inherited"] is None
    assert st["account_retention"]["status"] == "not_exposed" and mp.effective_retention(st)["days"] == 90
    pav = next(i for i in r["instances"] if i["account"] == "PavanTestAccount")
    assert pav["account_retention"]["days"] == 14 and pav["site_retention"]["status"] == "not_exposed"   # Pavan-Site1 not in fixture
    assert mp.effective_retention(pav)["days"] == 14
    glob = next(i for i in r["instances"] if i["scope_level"] == "global")
    assert glob["account_retention"] is None and glob["site_retention"] is None
    rows = mp.account_rows(r["instances"])
    nable = next(a for a in rows if a["account"].startswith("N-ABLE"))
    assert nable["retention_display"] == "90 Days" and nable["retention_days"] == 90 and nable["ingest_retention_days"] == [30, 90, 180, 365]
    unknown = next(a for a in rows if a["account"] == "TGS")             # not in the fixture → cannot be read
    assert unknown["retention_display"] == "not exposed by API" and unknown["retention_days"] is None
    d = mp.account_detail({"consoles": [r]}, "Staging", nable["account_id"])
    assert d["retention"]["display"] == "90 Days" and all("retention" in g for g in d["groups"])


def test_fetch_console_keeps_connectors_when_scope_lookup_fails():
    c = FakeClient(fail={"accounts": 403})
    r = mp.fetch_console(c, "Staging", sleep=lambda s: None)
    assert r["ok"] and len(r["instances"]) == 18 and "permission" in r["retention_error"].lower() or "Marketplace" in r["retention_error"]
    assert all(i["account_retention"]["status"] == "not_exposed" for i in r["instances"] if i["account_id"] != mp.GLOBAL_ACCOUNT_ID)


def test_views_and_totals():
    rows = mp.parse_applications(APPS, "Staging")
    scan = {"fetched_at": "x", "consoles": [{"console": "Staging", "ok": True, "instances": rows}]}
    accounts = mp.account_rows(rows)
    apolando = next(a for a in accounts if a["account"] == "Apolando")
    assert apolando["instances"] == 5 and apolando["sites"] == ["Support Training"] and apolando["account_level"] == 2 and apolando["alerts"] == 1
    assert apolando["last_installed_at"] == max(r["created_at"] for r in rows if r["account"] == "Apolando")
    conns = mp.connector_rows(rows)
    gw = next(c for c in conns if c["app_name"].startswith("Google Workspace"))
    assert gw["instances"] == 3 and gw["account_count"] == 3 and gw["site_count"] == 2 and gw["consoles"] == ["Staging"]
    t = mp.totals(scan)
    assert t == {"consoles_scanned": 1, "consoles_failed": 0, "accounts_with_connectors": len(accounts), "instances": 18,
                 "instances_with_alerts": 3, "connectors": 12}
    detail = mp.account_detail(scan, "Staging", apolando["account_id"])
    assert [g["level"] for g in detail["groups"]] == ["account", "site"] and detail["groups"][1]["site"] == "Support Training"
    assert mp.account_detail(scan, "Staging", "nope")["instances"] == 0


def test_fetch_console_pages_and_backs_off_on_429():
    c = FakeClient(throttle=2)
    sleeps = []
    r = mp.fetch_console(c, "Staging", sleep=sleeps.append)
    assert r["ok"] and len(r["instances"]) == 18 and r["catalog_size"] == 12
    assert sleeps[:2] == [2.0, 4.0]              # two 429s → growing back-off
    paths = [p for p, _ in c.calls]
    assert paths[0] == mp.CATALOG_PATH and mp.APPS_PATH in paths and paths[-1] in ("accounts", "sites")


def test_fetch_console_classifies_failures_without_raising():
    r = mp.fetch_console(FakeClient(fail={mp.CATALOG_PATH: 403}), "USEA1", sleep=lambda s: None)
    assert not r["ok"] and r["error_kind"] == "permission" and "Marketplace view" in r["error"] and r["instances"] == []
    r = mp.fetch_console(FakeClient(fail={mp.APPS_PATH: 401}), "USEA1", sleep=lambda s: None)
    assert r["error_kind"] == "auth"
    # A catalog hiccup that is not auth-related must not hide the instances.
    r = mp.fetch_console(FakeClient(fail={mp.CATALOG_PATH: 500}), "USEA1", sleep=lambda s: None)
    assert r["ok"] and len(r["instances"]) == 18 and r["catalog_size"] == 0


def test_scan_returns_good_consoles_and_flags_the_bad_ones():
    clients = {"Staging": FakeClient(), "USEA1": FakeClient(fail={mp.APPS_PATH: 403})}
    def client_for(code):
        if code == "EUCE1":
            raise RuntimeError("Console 'EUCE1' has no base URL or API token configured.")
        return clients[code]
    res = mp.scan(client_for, ["Staging", "USEA1", "EUCE1"], sleep=lambda s: None)
    by = {c["console"]: c for c in res["consoles"]}
    assert by["Staging"]["ok"] and len(by["Staging"]["instances"]) == 18
    assert by["USEA1"]["error_kind"] == "permission" and by["EUCE1"]["error_kind"] == "config"
    assert mp.totals(res)["consoles_failed"] == 2 and mp.totals(res)["instances"] == 18


def test_cache_serves_until_forced(monkeypatch):
    mp.clear_cache()
    calls = []
    def client_for(code):
        calls.append(code); return FakeClient()
    a = mp.cached_scan(client_for, ["Staging"], sleep=lambda s: None)
    b = mp.cached_scan(client_for, ["Staging"], sleep=lambda s: None)
    assert not a["from_cache"] and b["from_cache"] and calls == ["Staging"]
    c = mp.cached_scan(client_for, ["Staging"], force=True, sleep=lambda s: None)
    assert not c["from_cache"] and calls == ["Staging", "Staging"]
    mp.clear_cache()


def test_config_masking_and_log_shape():
    cfg = mp.instance_config(FakeClient(), "1")
    by = {f["id"]: f for f in cfg["fields"]}
    assert by["appKey"]["secret"] and by["appKey"]["value"] == "••••••••"
    assert by["client_token_thing"]["secret"] and by["client_token_thing"]["value"] == "••••••••"   # id looks secret
    assert not by["appId"]["secret"] and by["appId"]["value"] == "d303"
    log = mp.instance_log(FakeClient(), "1")
    assert log[0]["status"] == "Retry" and log[0]["http_status"] == 500


def test_csv():
    rows = mp.account_rows(mp.parse_applications(APPS, "Staging"))
    out = mp.to_csv(rows, [("Console", "console"), ("Account", "account"), ("Connectors", "connectors")])
    lines = out.strip().splitlines()
    assert lines[0] == "Console,Account,Connectors" and len(lines) == len(rows) + 1 and " | " in out


@pytest.fixture
def api(monkeypatch):
    import s1am.components.app as a
    mp.clear_cache()
    clients = {"Staging": FakeClient(), "USEA1": FakeClient(fail={mp.APPS_PATH: 403})}
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: clients[code])
    monkeypatch.setattr(a, "_mp_consoles", lambda: ["Staging", "USEA1"])
    monkeypatch.setattr(mp.time, "sleep", lambda s: None)
    yield a.app.test_client()
    mp.clear_cache()


def test_summary_route_reports_failed_console_and_views(api):
    d = api.get("/api/marketplace/summary").json
    assert d["totals"]["consoles_scanned"] == 1 and d["totals"]["consoles_failed"] == 1
    usea1 = next(c for c in d["consoles"] if c["console"] == "USEA1")
    assert usea1["error_kind"] == "permission" and "instances" in usea1 and isinstance(usea1["instances"], int)
    assert len(d["instances"]) == 18 and len(d["accounts"]) == d["totals"]["accounts_with_connectors"]
    assert any(c["app_name"] == "Okta" for c in d["connectors"])
    assert api.get("/api/marketplace/summary").json["from_cache"] is True
    assert api.get("/api/marketplace/summary?refresh=1").json["from_cache"] is False


def test_account_detail_and_instance_routes(api):
    d = api.get("/api/marketplace/summary").json
    acc = next(a for a in d["accounts"] if a["account"] == "Apolando")
    det = api.get(f"/api/marketplace/accounts/Staging/{acc['account_id']}").json
    assert det["instances"] == 5 and det["groups"][1]["site"] == "Support Training"
    assert api.get("/api/marketplace/accounts/Staging/0").status_code == 404
    log = api.get("/api/marketplace/instances/Staging/1/log").json
    assert log["entries"][0]["status"] == "Retry"
    cfg = api.get("/api/marketplace/instances/Staging/1/config").json
    assert all(f["value"] == "••••••••" for f in cfg["fields"] if f["secret"])
    assert api.get("/api/marketplace/instances/USEA1/1/log").status_code == 502


def test_export_matches_filters(api):
    import csv, io
    api.get("/api/marketplace/summary")
    rows = lambda qs: list(csv.reader(io.StringIO(api.get("/api/marketplace/export.csv?" + qs).data.decode())))
    assert len(rows("view=instances")) == 19                      # header + 18 (alert messages may contain newlines)
    assert len(rows("view=instances&status=alert")) == 4
    conn = rows("view=connectors&connector=Okta")
    assert len(conn) == 2 and conn[1][0] == "Okta"
    acc = rows("view=accounts&q=apolando")
    assert len(acc) == 2 and "Support Training" in acc[1]
    hdr = acc[0]; assert "Retention (DV)" in hdr and "Retention source" in hdr
    assert acc[1][hdr.index("Retention (DV)")] == "not exposed by API"      # Apolando is not in the fixture accounts
    lt30 = rows("view=instances&retention=lt:30")
    assert len(lt30) >= 2 and all("14 Days" in line for line in lt30[1:])
    assert len(rows("view=instances&retention=eq:90")) == 1 + 4                # Support Training (3 site-level) + N-ABLE account-level (90 Days)
