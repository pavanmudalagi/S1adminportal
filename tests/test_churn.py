"""Churn tracker: parsing, incremental sync, dedupe, rollups, exports — against a saved Staging payload, no network."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from s1am.api.exceptions import SentinelOneError
from s1am.churn import service as svc

FIX = json.loads((Path(__file__).parent / "fixtures" / "churn_activities.json").read_text())
OWN = {"usea1-nabl-staging.sentinelone.net"}


class FakeClient:
    """Serves the fixture like GET /activities (createdAt__gt + cursor paging) and GET /accounts?ids=."""

    def __init__(self, activities=None, accounts=None, page=2, fail_429_once=False):
        self.activities = sorted(activities if activities is not None else FIX["activities"], key=lambda a: a["createdAt"])
        self.accounts = accounts if accounts is not None else FIX["accounts"]
        self.page, self.calls, self.fail_429 = page, [], fail_429_once
        self._cursors = {}

    def request(self, method, path, params=None, payload=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        if path == "activities":
            if self.fail_429:
                self.fail_429 = False
                raise SentinelOneError("HTTP 429", status_code=429)
            if "cursor" in params:
                types, since, start = self._cursors[params["cursor"]]
            else:
                types = {int(t) for t in params["activityTypes"].split(",")}
                since = params["createdAt__gt"]; start = 0
            rows = [a for a in self.activities if a["activityType"] in types and a["createdAt"] > since]
            chunk = rows[start:start + self.page]
            nxt = None
            if start + self.page < len(rows):
                nxt = f"c{len(self._cursors)}"; self._cursors[nxt] = (types, since, start + self.page)
            return {"data": chunk, "pagination": {"nextCursor": nxt, "totalItems": len(rows)}}
        if path == "accounts":
            ids = set(params["ids"].split(","))
            return {"data": [a for a in self.accounts if a["id"] in ids], "pagination": {"nextCursor": None}}
        raise AssertionError(path)


@pytest.fixture
def cfg():
    return svc.load_indicators()


@pytest.fixture
def store(tmp_path):
    return svc.Store(tmp_path / "churn.db")


NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def _sync(store, cfg, client=None, **kw):
    return svc.sync_console(store, "Staging", client or FakeClient(), cfg, OWN, now=NOW, sleep=lambda s: None, **kw)


# ── parsing & classification ───────────────────────────────────────────────

def test_host_and_destination_classification(cfg):
    assert svc.host_of("https://euce1-100.sentinelone.net/path") == "euce1-100.sentinelone.net"
    assert svc.host_of("Console.Rival-EDR.example.com") == "console.rival-edr.example.com"
    assert svc.host_of(None) == ""
    rules = cfg["destinations"]
    assert svc.classify_destination("usea1-nabl-staging.sentinelone.net", OWN, rules) == ("internal", "N-able console (internal move)")
    assert svc.classify_destination("usea1-nabl-prod.sentinelone.net", set(), rules)[0] == "internal"   # pattern rule
    assert svc.classify_destination("euce1-100.sentinelone.net", OWN, rules) == ("sentinelone", "Other SentinelOne console")
    assert svc.classify_destination("console.rival-edr.example.com", OWN, rules) == ("other", "Unknown destination")


def test_parse_event_keeps_the_ticket_fields(cfg):
    by_type = {i["type"]: i for i in cfg["indicators"]}
    move = svc.parse_event(FIX["activities"][2], "Staging", by_type)
    assert (move["indicator"], move["kind"], move["destination_host"]) == ("move_commanded", "migration", "euce1-100.sentinelone.net")
    assert move["device_key"] == "aaaa-1" and move["username"].startswith("joe@beta") and move["created_at"] == "2026-09-10 08:00:00"
    assert move["account_id"] == "200" and move["site_id"] == "2001" and move["group_id"] == "20001" and move["agent_id"] == "6001"
    moved = svc.parse_event(FIX["activities"][4], "Staging", by_type)
    assert moved["indicator"] == "move_completed" and moved["destination_host"] == "euce1-100.sentinelone.net"
    unin = svc.parse_event(FIX["activities"][0], "Staging", by_type)
    assert unin["kind"] == "uninstall" and unin["destination_host"] == "" and unin["device_key"] == "5001"  # no uuid → agent id
    assert svc.parse_event({"activityType": 1, "id": "x"}, "Staging", by_type) is None


# ── fetching & sync ────────────────────────────────────────────────────────

def test_fetch_follows_the_cursor_and_retries_429():
    c = FakeClient(page=3, fail_429_once=True)
    rows = svc.fetch_activities(c, [51, 97, 99, 54], "2026-01-01T00:00:00Z", sleep=lambda s: None)
    assert [r["id"] for r in rows] == [a["id"] for a in sorted(FIX["activities"], key=lambda a: a["createdAt"])]
    pages = [p for p in c.calls if p[0] == "activities"]
    assert len(pages) == 4 and "cursor" in pages[-1][1] and "activityTypes" not in pages[-1][1]
    assert pages[1][1]["createdAt__gt"] == "2026-01-01T00:00:00Z" and pages[1][1]["limit"] == 1000


def test_first_sync_backfills_then_incremental_sync_adds_only_new_rows(store, cfg):
    r = _sync(store, cfg, backfill_days=365)
    assert r["ok"] and r["fetched"] == 9 and r["new"] == 9 and r["since"] == "2025-11-16T00:00:00Z"
    acc = store.accounts()
    assert acc[("Staging", "100")]["salesforce_id"] == "0015000000ACME1AAA"
    assert acc[("Staging", "300")]["name"] == "Gamma IT" and not acc[("Staging", "300")]["salesforce_id"]  # not returned by /accounts
    dests = store.destinations()
    assert dests["usea1-nabl-staging.sentinelone.net"]["kind"] == "internal"
    assert dests["console.rival-edr.example.com"]["kind"] == "other"
    # Second run: since = last event − overlap, nothing new, no duplicates.
    later = FIX["activities"] + [{**FIX["activities"][6], "id": "9010", "createdAt": "2026-11-10T00:00:00.000000Z", "agentId": "7009", "data": {**FIX["activities"][6]["data"], "computerName": "GAMMA-9"}}]
    c = FakeClient(activities=later)
    r2 = _sync(store, cfg, client=c)
    assert r2["since"] == "2026-11-02T01:00:00Z" and r2["fetched"] == 2 and r2["new"] == 1  # overlap re-reads 9009, id key dedupes it
    assert len(store.events()) == 10
    st = store.status()["Staging"]
    assert st["ok"] == 1 and st["last_event"] == "2026-11-10 00:00:00"


def test_console_failures_are_isolated_and_classified(store, cfg):
    class Bad:
        def request(self, *a, **k):
            raise SentinelOneError("HTTP 403", status_code=403)
    res = svc.sync_all(store, ["Staging", "USEA1"], cfg, client_for=lambda code: FakeClient() if code == "Staging" else Bad(), own_hosts=OWN, sleep=lambda s: None)
    by = {r["console"]: r for r in res["consoles"]}
    assert by["Staging"]["ok"] and by["Staging"]["new"] == 9
    assert not by["USEA1"]["ok"] and by["USEA1"]["kind"] == "permission"
    h = {x["console"]: x for x in svc.health(store, ["Staging", "USEA1", "EUCE1"])}
    assert (h["Staging"]["state"], h["USEA1"]["state"], h["EUCE1"]["state"]) == ("ok", "permission", "never")
    assert store.get_meta("last_sync")


def test_edited_destination_labels_survive_a_resync(store, cfg):
    _sync(store, cfg)
    d = store.set_destination("console.rival-edr.example.com", label="Rival EDR", kind="competitor", note="confirmed by AM")
    assert d["label"] == "Rival EDR" and d["kind"] == "competitor" and d["edited"] == 1
    with pytest.raises(ValueError):
        store.set_destination("console.rival-edr.example.com", kind="bogus")
    _sync(store, cfg)
    assert store.destinations()["console.rival-edr.example.com"]["label"] == "Rival EDR"
    assert store.set_destination("never-seen.example", label="x") is None


# ── rollups ────────────────────────────────────────────────────────────────

def test_periods():
    assert svc.periods_between("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert svc.periods_between("2025-11", "2026-05", "quarter") == ["2025-Q4", "2026-Q1", "2026-Q2"]
    assert svc.periods_between("2024-06", "2026-01", "year") == ["2024", "2025", "2026"]
    assert svc.month_range("2026-11", "2026-12") == ("2026-11-01 00:00:00", "2027-01-01 00:00:00")
    assert svc.default_range(12, datetime(2026, 11, 16).date()) == ("2025-12", "2026-11")


def test_summary_counts_devices_once_per_period_and_splits_destinations(store, cfg):
    _sync(store, cfg)
    s = svc.summary(store, "2026-08", "2026-11", "month", cfg=cfg)
    assert s["periods"] == ["2026-08", "2026-09", "2026-10", "2026-11"]
    sep = s["trend"][1]
    # BETA-SRV-1 had two move commands and one completion in September → one device, three events.
    assert (sep["devices"], sep["events"], sep["accounts"]) == (1, 3, 1)
    assert sep["by_indicator"] == {"move_commanded": 1, "move_completed": 1, "uninstalled": 0}
    assert sep["by_destination"] == {"sentinelone": 1}
    octo = s["trend"][2]
    assert octo["by_destination"] == {"internal": 1} and octo["by_indicator"]["uninstalled"] == 1  # decommission (54) is disabled
    assert s["totals"]["devices"] == 6 and s["totals"]["migrated_devices"] == 3 and s["totals"]["migrated_external"] == 2
    assert s["totals"]["uninstalled_devices"] == 3 and s["totals"]["accounts"] == 3
    acme = next(a for a in s["accounts"] if a["account_id"] == "100")
    assert acme["salesforce_id"] == "0015000000ACME1AAA" and acme["devices"] == 3 and acme["series"] == [2, 0, 0, 1]
    assert acme["destinations"][0]["host"] == "console.rival-edr.example.com" and acme["destinations"][0]["kind"] == "other"
    assert [d["host"] for d in s["destinations"]][0] == "euce1-100.sentinelone.net"
    assert s["totals"]["top_destination"]["devices"] == 1


def test_summary_filters_and_granularity(store, cfg):
    _sync(store, cfg)
    q = svc.summary(store, "2026-07", "2026-12", "quarter", cfg=cfg)
    assert q["periods"] == ["2026-Q3", "2026-Q4"] and q["trend"][0]["devices"] == 3  # Aug ×2 + Sep ×1
    only_moves = svc.summary(store, "2026-08", "2026-11", indicators=["move_commanded"], cfg=cfg)
    assert only_moves["totals"]["devices"] == 3 and all(a["by_indicator"].keys() == {"move_commanded"} for a in only_moves["accounts"])
    ext = svc.summary(store, "2026-08", "2026-11", dest_kind="other", cfg=cfg)
    assert ext["totals"]["devices"] == 1 and ext["accounts"][0]["account_id"] == "100"
    none = svc.summary(store, "2026-08", "2026-11", consoles=["USEA1"], cfg=cfg)
    assert none["totals"]["devices"] == 0 and none["accounts"] == []


def test_account_detail_and_device_rows_keep_the_latest_record_per_device(store, cfg):
    _sync(store, cfg)
    d = svc.account_detail(store, "Staging", "200", "2026-08", "2026-11", cfg=cfg)
    assert d["account"] == "Beta Networks" and d["devices"] == 2 and d["events"] == 4
    srv = next(r for r in d["device_rows"] if r["device_key"] == "aaaa-1")
    assert srv["indicator"] == "move_completed" and srv["destination_label"] == "Other SentinelOne console"
    assert d["sites"][0]["site"] in ("Main", "Branch") and sum(s["devices"] for s in d["sites"]) == 2
    rows = svc.device_rows(store, "2026-08", "2026-11", cfg=cfg)
    assert len(rows) == 6 and rows[0]["created_at"] >= rows[-1]["created_at"]
    assert all(r["salesforce_id"] for r in rows if r["account_id"] in ("100", "200"))


def test_salesforce_enrichment_only_asks_for_missing_ids(store, cfg):
    _sync(store, cfg)
    asked = []
    def lookup(ids):
        asked.append(sorted(ids)); return [{"Id": i, "Name": f"SF {i[-5:]}", "Customer_ID__c": "1", "BillingCountry": "UK"} for i in ids]
    assert svc.enrich_salesforce(store, lookup) == 2 and asked == [["0015000000ACME1AAA", "0015000000BETA1AAA"]]
    assert svc.enrich_salesforce(store, lookup) == 0 and len(asked) == 1
    s = svc.summary(store, "2026-08", "2026-11", cfg=cfg)
    acme = next(a for a in s["accounts"] if a["account_id"] == "100")
    assert acme["sf_account"] == "SF E1AAA" and acme["customer_id"] == "1"


# ── exports ────────────────────────────────────────────────────────────────

def test_exports(store, cfg):
    _sync(store, cfg)
    s = svc.summary(store, "2026-08", "2026-11", cfg=cfg)
    devices = svc.device_rows(store, "2026-08", "2026-11", cfg=cfg)
    csv_text = svc.to_csv(devices, svc.DEVICE_COLUMNS, note="test")
    lines = csv_text.splitlines()
    assert lines[0] == "# test" and lines[1].startswith("Console,Account ID,Account,Salesforce ID") and len(lines) == 2 + 6
    assert "console.rival-edr.example.com" in csv_text and "Token" not in csv_text
    trend_csv = svc.to_csv(svc.flatten_trend(s["trend"], [i["key"] for i in s["indicators"]]), svc.trend_columns([i["key"] for i in s["indicators"]]))
    assert trend_csv.splitlines()[0] == "Period,Devices,Events,Accounts,move_commanded,move_completed,uninstalled"
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(svc.to_xlsx(s, devices)))
    assert wb.sheetnames == ["Trend", "Accounts", "Destinations", "Devices"]
    assert wb["Trend"].max_row == 2 + 4 and wb["Devices"].max_row == 1 + 6


# ── Flask routes ───────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("S1AM_CHURN_DB_PATH", str(tmp_path / "churn.db"))
    monkeypatch.setenv("S1AM_CHURN_CONSOLES", "Staging")
    import s1am.components.app as a
    a._churn_store = None
    monkeypatch.setattr(svc, "client_for_profile", lambda code: FakeClient())
    monkeypatch.setattr(svc, "own_console_hosts", lambda: OWN)
    monkeypatch.setattr(svc, "time", type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(__import__("time").time)}))
    from s1am.salesforce import client as sf_client
    monkeypatch.setattr(sf_client, "is_connected", lambda: False)
    return a.app.test_client()


def test_routes_sync_summary_detail_export(api):
    r = api.post("/api/churn/sync", json={"wait": True}).json
    assert r["consoles"][0]["ok"] and r["consoles"][0]["new"] == 9
    s = api.get("/api/churn/summary?start=2026-08&end=2026-11").json
    assert s["totals"]["devices"] == 6 and s["health"][0]["state"] == "ok" and s["consoles"] == ["Staging"]
    assert [i["key"] for i in s["indicators"]] == ["move_commanded", "move_completed", "uninstalled"] and len(s["all_indicators"]) == 6
    assert api.get("/api/churn/summary?start=2026-13&end=2026-11").status_code == 400
    assert api.get("/api/churn/summary?granularity=week").status_code == 400
    q = api.get("/api/churn/summary?start=2026-07&end=2026-12&granularity=quarter").json
    assert q["periods"] == ["2026-Q3", "2026-Q4"]
    d = api.get("/api/churn/accounts/Staging/200?start=2026-08&end=2026-11").json
    assert d["devices"] == 2 and d["device_rows"][0]["indicator"] in ("move_completed", "move_commanded")
    assert api.get("/api/churn/accounts/Staging/999?start=2026-08&end=2026-11").status_code == 404
    csv_text = api.get("/api/churn/export?start=2026-08&end=2026-11&view=devices").data.decode()
    assert csv_text.count("\n") == 2 + 6 and "ACME-PC-3" in csv_text
    acc_csv = api.get("/api/churn/export?start=2026-08&end=2026-11&view=accounts").data.decode()
    assert "Beta Networks" in acc_csv and "Other SentinelOne console (1)" in acc_csv
    x = api.get("/api/churn/export?start=2026-08&end=2026-11&format=xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert api.get("/api/churn/export?view=nope").status_code == 400


def test_routes_destination_edit(api):
    api.post("/api/churn/sync", json={"wait": True})
    r = api.patch("/api/churn/destinations/console.rival-edr.example.com", json={"label": "Rival EDR", "kind": "competitor"})
    assert r.status_code == 200 and r.json["label"] == "Rival EDR"
    assert api.patch("/api/churn/destinations/console.rival-edr.example.com", json={"kind": "x"}).status_code == 400
    assert api.patch("/api/churn/destinations/nope.example", json={"label": "x"}).status_code == 404
    s = api.get("/api/churn/summary?start=2026-08&end=2026-11&destination=competitor").json
    assert s["totals"]["devices"] == 1 and s["destinations"][0]["label"] == "Rival EDR" and s["destinations"][0]["edited"]
