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
    assert c.calls[0][0] == mp.CATALOG_PATH and c.calls[-1][0] == mp.APPS_PATH


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
