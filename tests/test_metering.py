"""Usage metering: query builder escaping, response parser, rollup rules, store/sync with a fake PowerQuery client.
No network. The integration test at the bottom runs only with S1AM_INTEGRATION=1."""
import json
import os
from datetime import date

import pytest

from s1am.metering import service as m


# ── query builder ──────────────────────────────────────────────────────────

def test_literals_are_double_quoted_and_escaped():
    assert m.q_literal('O"Brien') == '"O\\"Brien"'
    assert m.q_literal("back\\slash") == '"back\\\\slash"'
    assert m.q_literal("it's") == '"it\'s"'
    with pytest.raises(m.QueryError):
        m.q_literal("new\nline")
    for bad in ("1abc", "a-b", "a b", "a;drop", ""):
        with pytest.raises(m.QueryError):
            m.q_ident(bad)
    with pytest.raises(m.QueryError):
        m.q_date("2026-1-1")


def test_usage_query_shapes():
    q = m.usage_query("server_endpoints", "2026-10-01", "2026-10-31", "account")
    assert q == ('| datasource "metering" from "server_endpoints" | filter date >= "2026-10-01" and date <= "2026-10-31" '
                 '| group value=sum(value) by date, account_scope_id, tenant_scope_id, tenant_scope_level, organization_level_1_scope_id, '
                 'organization_level_1_scope_level, state | limit 100000')
    q = m.usage_query("xdr_ingested_bytes", "2026-10-01", "2026-10-31", "site", account_id='12"3', breakdown="source_name")
    assert 'account_scope_id="12\\"3"' in q and "site_scope_id, source_name, state" in q
    q = m.usage_query("cns_workloads", "2026-10-01", "2026-10-31", "org1")
    assert "organization_level_1_scope_id, organization_level_1_name, organization_level_1_salesforce_id, state" in q
    with pytest.raises(m.QueryError):
        m.usage_query("server_endpoints", "2026-10-01", "2026-10-31", "site", breakdown="bad col")
    with pytest.raises(m.QueryError):
        m.usage_query("server endpoints", "2026-10-01", "2026-10-31")
    # The whole query must survive JSON encoding with escaped double quotes only.
    body = json.dumps({"query": m.usage_query("server_endpoints", "2026-10-01", "2026-10-31", "site", account_id='a"b')})
    assert '\\"a\\\\\\"b\\"' in body


# ── parser ─────────────────────────────────────────────────────────────────

def test_scope_fallbacks_for_identity_reports():
    assert m._scope_ids({"account_scope_id": "A", "site_scope_id": "S"}, "site") == ("A", "S")
    assert m._scope_ids({"account_scope_id": None, "tenant_scope_id": "T", "tenant_scope_level": "account"}, "site") == ("T", "")
    assert m._scope_ids({"account_scope_id": None, "tenant_scope_id": "T", "tenant_scope_level": "site", "organization_level_1_scope_id": "O", "organization_level_1_scope_level": "account"}, "site") == ("O", "T")
    assert m._scope_ids({"account_scope_id": None, "tenant_scope_id": "T", "tenant_scope_level": "site"}, "account") == ("", "")


def test_parse_response_zips_columns_and_fixes_typo():
    payload = {"columns": [{"name": "date"}, {"name": "tentant_scope_level"}, {"name": "value"}], "values": [["2026-10-01", "site", 3], ["2026-10-02", "account"]]}
    rows = m.parse_response(payload)
    assert rows == [{"date": "2026-10-01", "tenant_scope_level": "site", "value": 3}, {"date": "2026-10-02", "tenant_scope_level": "account"}]
    assert m.parse_response({}) == [] and m.parse_response(None) == []


# ── rollups ────────────────────────────────────────────────────────────────

DAYS = [{"date": f"2026-10-{d:02d}", "value": v, "state": "archived"} for d, v in [(1, 10), (2, 12), (3, 8)]]


def test_rollup_rules():
    assert m.rollup_monthly(DAYS, "peak")["value"] == 12
    assert m.rollup_monthly(DAYS, "average")["value"] == 10
    assert m.rollup_monthly(DAYS, "month_end")["value"] == 8
    assert m.rollup_monthly(DAYS, "sum")["value"] == 30
    r = m.rollup_monthly(DAYS, "peak")
    assert (r["state"], r["days"], r["live_days"], r["first"], r["last"]) == ("final", 3, 0, "2026-10-01", "2026-10-03")
    # Several site rows on one day are summed before the rule is applied.
    two_sites = DAYS + [{"date": "2026-10-03", "value": 5, "state": "archived"}]
    assert m.rollup_monthly(two_sites, "month_end")["value"] == 13 and m.rollup_monthly(two_sites, "peak")["value"] == 13
    assert m.rollup_monthly([], "peak") == {"value": None, "rule": "peak", "days": 0, "live_days": 0, "state": "empty", "first": None, "last": None}
    with pytest.raises(ValueError):
        m.rollup_monthly(DAYS, "median")


def test_rollup_month_with_live_and_archived_rows_is_provisional():
    mixed = DAYS + [{"date": "2026-10-04", "value": 15, "state": "live"}, {"date": "2026-10-04", "value": 1, "state": "archived"}]
    r = m.rollup_monthly(mixed, "peak")
    assert r["value"] == 16 and r["state"] == "provisional" and r["live_days"] == 1 and r["days"] == 4


def test_format_bytes_and_month_helpers():
    assert m.format_bytes(1_500_000_000) == "1.50 GB" and m.format_bytes(1_500_000_000, 1024) == "1.40 GB"
    assert m.format_bytes(2.5e12) == "2.50 TB" and m.format_bytes(None) == ""
    assert m.month_bounds("2026-02") == ("2026-02-01", "2026-02-28") and len(m.month_days("2026-10")) == 31
    assert m.months_to_sync(date(2026, 11, 13)) == ["2026-10", "2026-11"] and m.months_to_sync(date(2026, 1, 2)) == ["2025-12", "2026-01"]


def test_products_config_loads_and_validates(tmp_path):
    cfg = m.load_products()
    assert {p["report"] for p in cfg["products"]} >= {"cns_workloads", "server_endpoints", "xdr_ingested_bytes", "singularity_identity_users"}
    assert m.product_by_key(cfg["products"], "xdr_ingested_bytes")["rollup"] == "sum" and cfg["bytes_base"] in (1000, 1024)
    bad = tmp_path / "p.json"; bad.write_text(json.dumps({"products": [{"report": "x", "rollup": "median"}]}))
    with pytest.raises(ValueError):
        m.load_products(bad)


# ── fake console ───────────────────────────────────────────────────────────

class FakePQ:
    """Answers the discovery, tenants, names and usage queries from an in-memory table of daily rows."""
    def __init__(self, reports, rows, fail=None):
        self.reports, self.rows, self.fail, self.queries = reports, rows, fail, []

    def query(self, query, start, end):
        self.queries.append((query, start, end))
        if self.fail:
            raise m.MeteringError(*self.fail)
        if 'from "reports"' in query:
            return [{"report_name": r, "product_category": "c", "product_name": r, "unit": "u", "description": ""} for r in self.reports]
        if 'from "tenants"' in query:
            return [{"tenant_scope_id": "A1", "tenant_scope_name": "Acme"}, {"tenant_scope_id": "S1", "tenant_scope_name": "Acme HQ"}, {"tenant_scope_id": "S2", "tenant_scope_name": "Acme Lab"}]
        if "n=count()" in query:
            return [{"account_scope_id": "A1", "account_scope_name": "Acme Ltd", "site_scope_id": "S1", "site_scope_name": "Acme HQ", "organization_level_1_salesforce_id": "001X"}]
        rep = query.split('from "')[1].split('"')[0]
        lo = query.split('date >= "')[1][:10]; hi = query.split('date <= "')[1][:10]
        out = []
        for r in self.rows:
            if r["report"] == rep and lo <= r["date"] <= hi:
                row = {"date": r["date"], "account_scope_id": r["account"], "site_scope_id": r["site"], "value": r["value"], "state": r["state"]}
                if "endpoint_bundle" in query:
                    row["endpoint_bundle"] = r.get("bundle", "Complete")
                out.append(row)
        return out


def _rows():
    rows = []
    for d in range(1, 11):
        st = "archived" if d <= 8 else "live"
        rows.append({"report": "server_endpoints", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S1", "value": 10 + d, "state": st, "bundle": "Complete"})
        rows.append({"report": "server_endpoints", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S2", "value": 5, "state": st, "bundle": "Control"})
        rows.append({"report": "xdr_ingested_bytes", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S1", "value": 1e9, "state": st})
    return rows


@pytest.fixture
def world(tmp_path):
    store = m.Store(tmp_path / "usage.db")
    products = m.load_products()["products"]
    return store, products


def test_sync_fetches_only_available_reports_and_refetches_live_days(world):
    store, products = world
    pq = FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows())
    st = m.sync_console(pq, store, "Staging", "2026-10", products, today="2026-10-10")
    assert st["ok"] and set(st["fetched"]) == {"server_endpoints", "xdr_ingested_bytes"}
    assert "cns_workloads" in st["skipped"] and "singularity_identity_users" in st["skipped"]
    assert st["fetched"]["server_endpoints"] == 20 and len(store.daily("2026-10-01", "2026-10-31")) == 30
    usage_queries = [q for q, _, _ in pq.queries if "sum(value)" in q]
    assert len(usage_queries) == 2 and all('date <= "2026-10-10"' in q for q in usage_queries)
    assert store.cached_reports("Staging") and store.names("Staging")[("Staging", "A1")]["salesforce_id"] == "001X"
    assert store.names("Staging")[("Staging", "S2")]["name"] == "Acme Lab"       # from the tenants table

    # Second sync: archived days 1-8 are kept, only the live days 9-10 (and new day 11) are asked for.
    pq.rows = [dict(r, state="archived") for r in _rows()] + [{"report": "server_endpoints", "date": "2026-10-11", "account": "A1", "site": "S1", "value": 99, "state": "live"}]
    pq.queries.clear()
    st2 = m.sync_console(pq, store, "Staging", "2026-10", products, today="2026-10-11")
    q = next(q for q, _, _ in pq.queries if 'from "server_endpoints"' in q and "sum(value)" in q)
    assert 'date >= "2026-10-09"' in q and 'date <= "2026-10-11"' in q
    states = store.day_states("Staging", "server_endpoints", "2026-10-01", "2026-10-31")
    assert states["2026-10-09"] == "archived" and states["2026-10-11"] == "live" and len(states) == 11
    assert store.cached_reports("Staging") is not None and sum(1 for q, _, _ in pq.queries if 'from "reports"' in q) == 0


def test_sync_reports_permission_failure_without_raising(world):
    store, products = world
    st = m.sync_console(FakePQ([], [], fail=("Token lacks Metering Reports – View (or is invalid)", "permission", 403)), store, "USEA1", "2026-10", products)
    assert not st["ok"] and st["kind"] == "permission"
    h = m.health(store, ["USEA1", "EUCE1"], products)
    assert h[0]["state"] == "permission" and "Metering Reports" in h[0]["message"] and h[1]["state"] == "never"


def test_sync_all_isolates_console_failures(world):
    store, products = world
    clients = {"Staging": FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), "USEA1": FakePQ([], [], fail=("nope", "unsupported", 404))}
    def client_for(code):
        if code == "EUCE1":
            raise m.MeteringError("Console 'EUCE1' has no base URL or token configured", "config")
        return clients[code]
    res = m.sync_all(store, ["Staging", "USEA1", "EUCE1"], "2026-10", products, client_for=client_for)
    by = {c["console"]: c for c in res["consoles"]}
    assert by["Staging"]["ok"] and by["USEA1"]["kind"] == "unsupported" and by["EUCE1"]["kind"] == "config"
    h = {x["console"]: x["state"] for x in m.health(store, ["Staging", "USEA1", "EUCE1"], products)}
    assert h == {"Staging": "ok", "USEA1": "not_enabled", "EUCE1": "config"}
    assert store.get_meta("last_sync") and store.get_meta("last_sync_month") == "2026-10"


def test_summary_account_and_site_views(world):
    store, products = world
    m.sync_console(FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10")
    s = m.summary(store, "2026-10", products)
    a = s["accounts"][0]
    assert a["account"] == "Acme Ltd" and a["salesforce_id"] == "001X" and a["sites"] == 2 and a["state"] == "provisional"
    srv = a["products"]["cws_servers"]
    assert srv["rule"] == "peak" and srv["value"] == 25 and srv["display"] == "25" and srv["state"] == "provisional"  # day 10: 20 + 5
    ing = a["products"]["data_ingest"]
    assert ing["rule"] == "sum" and ing["value"] == 1e10 and ing["display"] == "10.00 GB"
    assert s["totals"]["cws_servers"]["value"] == 25 and s["rules"]["cws_servers"] == "peak"
    s2 = m.summary(store, "2026-10", products, rules={"cws_servers": "average"})
    assert s2["accounts"][0]["products"]["cws_servers"]["value"] == pytest.approx(20.5)
    d = m.account_detail(store, "2026-10", products, "Staging", "A1")
    assert len(d["products"]["cws_servers"]["series"]) == 10 and d["products"]["cws_servers"]["series"][-1]["state"] == "live"
    assert {b["key"] for b in d["products"]["cws_servers"]["breakdown"]} == {"Complete", "Control"}
    assert [x["site"] for x in d["sites"]] == ["Acme HQ", "Acme Lab"] and d["sites"][1]["products"]["cws_servers"]["value"] == 5
    sd = m.site_detail(store, "2026-10", products, "Staging", "S1")
    assert sd["site"] == "Acme HQ" and sd["account"] == "Acme Ltd" and sd["products"]["data_ingest"]["display"] == "10.00 GB"


def test_exports(world):
    store, products = world
    m.sync_console(FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10")
    rows, rules = m.export_rows(store, "2026-10", products, "account")
    csv_text = m.to_csv(rows, products, "account", "2026-10", rules)
    assert csv_text.startswith("# SentinelOne usage 2026-10 — account level — rules:") and "CWS – Servers (agents, peak)" in csv_text
    assert "Acme Ltd" in csv_text and ",provisional," in csv_text
    rows_s, _ = m.export_rows(store, "2026-10", products, "site")
    assert [r["site"] for r in rows_s] == ["Acme HQ", "Acme Lab"]
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(m.to_xlsx(rows_s, products, "site", "2026-10", rules)))
    assert wb.sheetnames[0] == "Summary" and "CWS Srv" in wb.sheetnames and "Ingest" in wb.sheetnames
    assert "rule: peak" in wb["CWS Srv"]["A1"].value and wb["CWS Srv"].max_row == 4  # header rows + 2 sites


# ── integration (opt-in) ───────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get("S1AM_INTEGRATION") != "1", reason="set S1AM_INTEGRATION=1 to hit the staging console")
def test_integration_staging_reports_and_one_day():
    client = m.client_for_profile(os.environ.get("S1AM_INTEGRATION_PROFILE", "Staging"))
    reports = m.list_reports(client)
    assert any(r["report"] == "server_endpoints" for r in reports)
    names = m.list_tenants(client)
    assert names and all(n["scope_id"] for n in names)
    rows = m.get_usage(client, "server_endpoints", "2026-10-15", "2026-10-15", level="account")
    assert rows and all(r["date"] == "2026-10-15" and r["state"] in ("archived", "live") for r in rows)


# ── Flask routes ───────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("S1AM_USAGE_DB_PATH", str(tmp_path / "usage.db"))
    import s1am.components.app as a
    a._usage_store = None
    monkeypatch.setattr(a, "_usage_consoles", lambda: ["Staging", "USEA1"])
    clients = {"Staging": FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), "USEA1": FakePQ([], [], fail=("Token lacks Metering Reports – View (or is invalid)", "permission", 403))}
    monkeypatch.setattr(m, "client_for_profile", lambda code, sleep=None: clients[code])
    yield a.app.test_client(), a
    a._usage_store = None


def test_sync_route_then_summary_health_and_drilldowns(api):
    client, a = api
    r = client.post("/api/usage/sync", json={"month": "2026-10", "wait": True}).json
    assert {c["console"]: c["ok"] for c in r["consoles"]} == {"Staging": True, "USEA1": False}
    s = client.get("/api/usage/summary?month=2026-10").json
    assert s["accounts"][0]["account"] == "Acme Ltd" and s["accounts"][0]["products"]["cws_servers"]["value"] == 25
    health = {h["console"]: h["state"] for h in s["health"]}
    assert health == {"Staging": "ok", "USEA1": "permission"} and s["last_sync_month"] == "2026-10"
    assert [p["key"] for p in s["products"]][:2] == ["cns", "cws_servers"]
    s2 = client.get("/api/usage/summary?month=2026-10&products=cws_servers&rules=cws_servers=month_end").json
    assert list(s2["accounts"][0]["products"]) == ["cws_servers"] and s2["accounts"][0]["products"]["cws_servers"]["value"] == 25 and s2["rules"]["cws_servers"] == "month_end"
    assert client.get("/api/usage/summary?month=2026-1").status_code == 400
    acc = client.get("/api/usage/accounts/Staging/A1?month=2026-10").json
    assert len(acc["sites"]) == 2 and acc["products"]["data_ingest"]["display"] == "10.00 GB"
    assert client.get("/api/usage/accounts/Staging/ZZ?month=2026-10").status_code == 404
    site = client.get("/api/usage/sites/Staging/S2?month=2026-10").json
    assert site["site"] == "Acme Lab" and site["products"]["cws_servers"]["value"] == 5
    assert client.post("/api/usage/sync", json={"month": "bad"}).status_code == 400


def test_export_route_formats_and_final_guard(api):
    client, _ = api
    client.post("/api/usage/sync", json={"month": "2026-10", "wait": True})
    csv_r = client.get("/api/usage/export?month=2026-10&level=account&format=csv")
    assert csv_r.status_code == 200 and csv_r.mimetype == "text/csv" and b"Acme Ltd" in csv_r.data
    x = client.get("/api/usage/export?month=2026-10&level=site&format=xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert client.get("/api/usage/export?month=2026-10&level=account&format=csv&final=1").status_code == 409   # live days present
    assert client.get("/api/usage/export?month=2026-10&level=nope").status_code == 400
