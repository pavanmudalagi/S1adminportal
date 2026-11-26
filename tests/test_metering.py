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
    # Prompt seat reports are daily snapshots: rows of one scope on one day take the max, never a sum.
    q = m.usage_query("ai_security_employees_usage", "2026-10-01", "2026-10-31", "site", daily="max")
    assert "| group value=max(value) by date, account_scope_id" in q and "site_scope_id, state" in q
    with pytest.raises(m.QueryError):
        m.usage_query("ai_security_employees_usage", "2026-10-01", "2026-10-31", daily="median")
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
    # Prompt meters: seats are max per day / peak per month, usage counts are summed; every product has a daily rule.
    prompt = {p["report"]: p for p in cfg["products"] if p["report"].startswith("ai_security_")}
    assert set(prompt) == {"ai_security_employees_usage", "ai_security_developers_usage", "ai_security_homegrown_app_calls",
                           "ai_security_agentic_ai_usage", "ai_security_red_teaming_usage"}
    assert all((p["daily"], p["rollup"]) == ("max", "peak") for r, p in prompt.items() if r.endswith("_usage") and "seat" in p["unit"])
    assert all((p["daily"], p["rollup"]) == ("sum", "sum") for r, p in prompt.items() if p["unit"] in ("calls", "events", "attacks"))
    assert all(p["daily"] in m.DAILY_AGGS for p in cfg["products"])
    bad = tmp_path / "d.json"; bad.write_text(json.dumps({"products": [{"report": "x", "daily": "median"}]}))
    with pytest.raises(ValueError):
        m.load_products(bad)
    bad = tmp_path / "p.json"; bad.write_text(json.dumps({"products": [{"report": "x", "rollup": "median"}]}))
    with pytest.raises(ValueError):
        m.load_products(bad)


# ── fake console ───────────────────────────────────────────────────────────

class FakePQ:
    """Answers the discovery, tenants, names and usage queries from an in-memory table of daily rows. ``cap`` mimics
    the API's 100,000-record cut: a usage answer with more rows raises the ``truncated`` error the real client does."""
    def __init__(self, reports, rows, fail=None, cap=None):
        self.reports, self.rows, self.fail, self.queries, self.cap = reports, rows, fail, [], cap

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
        if self.cap and len(out) > self.cap:
            raise m.MeteringError("PowerQuery answer truncated at 100,000 records", "truncated")
        return out


def _rows():
    rows = []
    for d in range(1, 11):
        st = "archived" if d <= 8 else "live"
        rows.append({"report": "server_endpoints", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S1", "value": 10 + d, "state": st, "bundle": "Complete"})
        rows.append({"report": "server_endpoints", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S2", "value": 5, "state": st, "bundle": "Control"})
        rows.append({"report": "xdr_ingested_bytes", "date": f"2026-10-{d:02d}", "account": "A1", "site": "S1", "value": 1e9, "state": st})
    return rows


def _assets():
    def mk(acc, site, cat, sub, cov=("CWS",), dec=False):
        return {"s1AccountId": acc, "s1SiteId": site, "category": cat, "subCategory": sub, "activeCoverage": list(cov), "agent": {"decommissioned": dec}}
    return [mk("A1", "S1", "Server", "Virtual Machine"), mk("A1", "S1", "Server", "Virtual Machine", ("CWS", "IDR")), mk("A1", "S1", "Server", "Container Host"),
            mk("A1", "S1", "Server", "Virtual Machine", dec=True),          # decommissioned: not counted
            mk("A1", "S1", "Container", "Namespace"),                        # cloud object, not an agent
            mk("A1", "S2", "Workstation", "Laptop", ("EPP",))]               # EPP: never returned for activeCoverage=CWS


class FakeInventory:
    """GET /xdr/assets?activeCoverage=… in two pages."""
    def __init__(self, assets, fail=None):
        self.assets, self.fail, self.calls = assets, fail, []

    def paginate(self, path, params=None, page_limit=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise RuntimeError(self.fail)
        data = [a for a in self.assets if (params or {}).get("activeCoverage") in a["activeCoverage"]]
        return [{"data": data[:2], "pagination": {"nextCursor": "c1"}}, {"data": data[2:], "pagination": {"nextCursor": None}}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    # no console profile is ever read in tests: the inventory snapshot needs an explicit mgmt_client_for
    monkeypatch.setattr(m, "mgmt_client_for_profile", lambda code: (_ for _ in ()).throw(m.MeteringError(f"no profile {code} in tests", "config")))
    store = m.Store(tmp_path / "usage.db")
    products = m.load_products()["products"]
    return store, products


def test_sync_fetches_only_available_reports_and_refetches_live_days(world):
    store, products = world
    pq = FakePQ(["server_endpoints", "xdr_ingested_bytes", "ai_security_employees_usage"], _rows())
    st = m.sync_console(pq, store, "Staging", "2026-10", products, today="2026-10-10")
    assert st["ok"] and set(st["fetched"]) == {"server_endpoints", "xdr_ingested_bytes", "ai_security_employees_usage"}
    assert "cns_workloads" in st["skipped"] and "singularity_identity_users" in st["skipped"] and "ai_security_developers_usage" in st["skipped"]
    assert st["fetched"]["server_endpoints"] == 20 and len(store.daily("2026-10-01", "2026-10-31")) == 30
    usage_queries = [q for q, _, _ in pq.queries if "(value) by date" in q]
    assert len(usage_queries) == 3 and all('date <= "2026-10-10"' in q for q in usage_queries)
    assert [q for q in usage_queries if 'from "ai_security_employees_usage"' in q][0].count("value=max(value)") == 1
    assert all("value=sum(value)" in q for q in usage_queries if "ai_security" not in q)
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
    srv = a["products"]["servers"]
    assert srv["rule"] == "peak" and srv["value"] == 25 and srv["display"] == "25" and srv["state"] == "provisional"  # day 10: 20 + 5
    ing = a["products"]["data_ingest"]
    assert ing["rule"] == "sum" and ing["value"] == 1e10 and ing["display"] == "10.00 GB"
    assert s["totals"]["servers"]["value"] == 25 and s["rules"]["servers"] == "peak"
    s2 = m.summary(store, "2026-10", products, rules={"servers": "average"})
    assert s2["accounts"][0]["products"]["servers"]["value"] == pytest.approx(20.5)
    d = m.account_detail(store, "2026-10", products, "Staging", "A1")
    assert len(d["products"]["servers"]["series"]) == 10 and d["products"]["servers"]["series"][-1]["state"] == "live"
    assert {b["key"] for b in d["products"]["servers"]["breakdown"]} == {"Complete", "Control"}
    assert [x["site"] for x in d["sites"]] == ["Acme HQ", "Acme Lab"] and d["sites"][1]["products"]["servers"]["value"] == 5
    sd = m.site_detail(store, "2026-10", products, "Staging", "S1")
    assert sd["site"] == "Acme HQ" and sd["account"] == "Acme Ltd" and sd["products"]["data_ingest"]["display"] == "10.00 GB"


def test_exports(world):
    store, products = world
    m.sync_console(FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10")
    rows, rules = m.export_rows(store, "2026-10", products, "account")
    csv_text = m.to_csv(rows, products, "account", "2026-10", rules)
    assert csv_text.startswith("# SentinelOne usage 2026-10 — account level — rules:") and "Endpoint – Servers (agents, peak)" in csv_text
    assert "Acme Ltd" in csv_text and ",provisional," in csv_text
    rows_s, _ = m.export_rows(store, "2026-10", products, "site")
    assert [r["site"] for r in rows_s] == ["Acme HQ", "Acme Lab"]
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(m.to_xlsx(rows_s, products, "site", "2026-10", rules)))
    assert wb.sheetnames[0] == "Summary" and {"Srv", "CWS Srv", "Ingest"} <= set(wb.sheetnames)
    assert "rule: peak" in wb["Srv"]["A1"].value and wb["Srv"].max_row == 4  # header rows + 2 sites


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
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: FakeInventory(_assets()))
    clients = {"Staging": FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), "USEA1": FakePQ([], [], fail=("Token lacks Metering Reports – View (or is invalid)", "permission", 403))}
    monkeypatch.setattr(m, "client_for_profile", lambda code, sleep=None: clients[code])
    yield a.app.test_client(), a
    a._usage_store = None


def test_sync_route_then_summary_health_and_drilldowns(api):
    client, a = api
    r = client.post("/api/usage/sync", json={"month": "2026-10", "wait": True}).json
    assert {c["console"]: c["ok"] for c in r["consoles"]} == {"Staging": True, "USEA1": False}
    s = client.get("/api/usage/summary?month=2026-10").json
    assert s["accounts"][0]["account"] == "Acme Ltd" and s["accounts"][0]["products"]["servers"]["value"] == 25
    health = {h["console"]: h["state"] for h in s["health"]}
    assert health == {"Staging": "ok", "USEA1": "permission"} and s["last_sync_month"] == "2026-10"
    assert [p["key"] for p in s["products"]][:3] == ["cns", "workstations", "servers"]
    s2 = client.get("/api/usage/summary?month=2026-10&products=servers&rules=servers=month_end").json
    # a filtered summary still lists every product so the toggle row can re-enable the ones filtered out
    assert [p["key"] for p in s2["products"]] == ["servers"] and len(s2["all_products"]) == len(s["products"]) > 1
    assert list(s2["accounts"][0]["products"]) == ["servers"] and s2["accounts"][0]["products"]["servers"]["value"] == 25 and s2["rules"]["servers"] == "month_end"
    assert client.get("/api/usage/summary?month=2026-1").status_code == 400
    acc = client.get("/api/usage/accounts/Staging/A1?month=2026-10").json
    assert len(acc["sites"]) == 2 and acc["products"]["data_ingest"]["display"] == "10.00 GB"
    assert client.get("/api/usage/accounts/Staging/ZZ?month=2026-10").status_code == 404
    site = client.get("/api/usage/sites/Staging/S2?month=2026-10").json
    assert site["site"] == "Acme Lab" and site["products"]["servers"]["value"] == 5
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


# ── Prompt Security: tenant details behind the Prompt meters ───────────────

def _prompt_rows():
    return [{"report": "ai_security_employees_usage", "date": f"2026-10-{d:02d}", "account": acc, "site": "", "value": v, "state": "archived"}
            for d in range(1, 6) for acc, v in (("A1", 8), ("A2", 0))]


class FakeMgmt:
    """GET /accounts/{id} with promptFields; A2 has no tenant, Z9 is unknown."""
    accounts = {
        "A1": {"id": "A1", "name": "Acme Ltd", "state": "active", "usageType": "mssp",
               "licenses": {"bundles": [{"name": "complete"}, {"name": "prompt_employees", "displayName": "Prompt for Employees"}]},
               "promptFields": {"promptTenantName": "acme_ltd", "promptRegion": None,
                                "promptUsers": [{"promptUserName": "Alex Admin", "promptUserEmail": "alex@acme.example"},
                                                {"promptUserName": "N-able Prompt Security EAP Admin", "promptUserEmail": "promptsecurity-eap-admin@n-able.com"}]}},
        "A2": {"id": "A2", "name": "Beta", "state": "active", "usageType": "customer", "licenses": {"bundles": [{"name": "control"}]}, "promptFields": None},
    }

    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None):
        self.calls.append(path)
        if path.startswith("accounts/"):
            a = self.accounts.get(path.split("/")[1])
            if a is None:
                raise RuntimeError("404 not found")
            return {"data": a}
        return {"data": [a for a in self.accounts.values() if a["id"] == (params or {}).get("ids")]}


def test_prompt_tenant_fields_reads_tenant_admins_and_hosting_console():
    f = m.prompt_tenant_fields(FakeMgmt.accounts["A1"], "https://usea1-nabl11.sentinelone.net")
    assert f["tenant"] == "acme_ltd" and f["bundles"] == ["Prompt for Employees"] and f["usage_type"] == "mssp"
    assert [u["email"] for u in f["users"]] == ["alex@acme.example", "promptsecurity-eap-admin@n-able.com"]
    # a US MSSP partner tenant is hosted in US West; the region itself is the console's (promptRegion is null on older accounts)
    assert f["region"] == "useast" and f["console_url"] == "https://uswest.prompt.security"
    eu = m.prompt_tenant_fields(FakeMgmt.accounts["A1"], "https://euce1-swprd2.sentinelone.net")
    assert eu["console_url"] == "https://eunorth.prompt.security"
    none = m.prompt_tenant_fields(FakeMgmt.accounts["A2"], "https://usea1-nabl11.sentinelone.net")
    assert none["tenant"] is None and none["users"] == [] and none["bundles"] == []
    # the single-admin pair is read when promptUsers is absent
    single = dict(FakeMgmt.accounts["A1"], promptFields={"promptTenantName": "t", "promptUserName": "Solo", "promptUserEmail": "solo@x.example"})
    assert m.prompt_tenant_fields(single, "https://apne1-swprd7.sentinelone.net")["users"] == [{"name": "Solo", "email": "solo@x.example"}]
    assert m.prompt_tenant_fields(single, "https://apne1-swprd7.sentinelone.net")["console_url"] is None   # Tokyo has no Prompt region in S1's table


def test_refresh_prompt_tenants_reads_each_account_once_and_keeps_fresh_rows(world):
    store, products = world
    store.replace_days("Staging", "ai_security_employees_usage", [f"2026-10-{d:02d}" for d in range(1, 6)],
                       [{"date": r["date"], "account_id": r["account"], "site_id": "", "value": r["value"], "state": r["state"]} for r in _prompt_rows()])
    store.replace_days("Staging", "ai_security_employees_usage", ["2026-10-07"], [{"date": "2026-10-07", "account_id": "Z9", "site_id": "", "value": 1, "state": "archived"}])
    assert m.prompt_accounts(store, "2026-10", products) == [("Staging", "A1"), ("Staging", "A2"), ("Staging", "Z9")]
    mgmt = FakeMgmt()
    r = m.refresh_prompt_tenants(store, m.prompt_accounts(store, "2026-10", products), lambda code: mgmt, {"Staging": "https://usea1-nabl-staging.sentinelone.net"})
    assert r == {"checked": 3, "skipped": 0, "errors": 1}
    t = store.prompt_tenants()
    assert t[("Staging", "A1")]["tenant"] == "acme_ltd" and t[("Staging", "A1")]["console_url"] == "https://uswest.prompt.security"
    assert t[("Staging", "A2")]["tenant"] is None and t[("Staging", "A2")]["error"] is None
    assert "not found" in t[("Staging", "Z9")]["error"]
    # a second pass keeps the fresh rows and retries only the failed one; force re-reads everything
    n = len(mgmt.calls)
    assert m.refresh_prompt_tenants(store, m.prompt_accounts(store, "2026-10", products), lambda code: mgmt, {})["checked"] == 1
    assert m.refresh_prompt_tenants(store, m.prompt_accounts(store, "2026-10", products), lambda code: mgmt, {}, force=True)["checked"] == 3
    assert len(mgmt.calls) > n
    s = m.prompt_summary(store, "2026-10", products, hide_zero=False)
    assert [p for p in s["rules"]] == [p["key"] for p in products if p["key"].startswith("prompt_")]
    by = {a["account_id"]: a for a in s["accounts"]}
    assert by["A1"]["prompt"]["tenant"] == "acme_ltd" and by["A1"]["products"]["prompt_employees"]["value"] == 8
    assert by["A2"]["prompt"]["tenant"] is None and s["tenants_missing"] == 1   # Z9 failed; A2 is read but has no tenant
    assert len(m.prompt_summary(store, "2026-10", products)["accounts"]) == 2   # hide_zero drops A2


def test_prompt_routes(api, monkeypatch):
    client, a = api
    mgmt = FakeMgmt()
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: mgmt)
    from s1am.eap import service as eap_service
    monkeypatch.setattr(eap_service, "profiles", lambda: {"Staging": "https://usea1-nabl-staging.sentinelone.net"})
    svc, store, _ = a._usage()
    store.replace_days("Staging", "ai_security_employees_usage", ["2026-10-01"], [{"date": "2026-10-01", "account_id": "A1", "site_id": "", "value": 8, "state": "archived"}])
    s = client.get("/api/usage/prompt?month=2026-10").json
    assert s["view"] == "prompt" and s["tenants_missing"] == 1 and s["accounts"][0]["prompt"] is None
    assert [p["key"] for p in s["all_products"]] == ["prompt_employees", "prompt_developers", "prompt_homegrown", "prompt_agentic", "prompt_redteam"]
    r = client.post("/api/usage/prompt/refresh", json={"month": "2026-10"}).json
    assert r == {"accounts": 1, "checked": 1, "skipped": 0, "errors": 0}
    s = client.get("/api/usage/prompt?month=2026-10&products=prompt_employees").json
    p = s["accounts"][0]["prompt"]
    assert p["tenant"] == "acme_ltd" and p["console_url"] == "https://uswest.prompt.security" and p["users"][0]["name"] == "Alex Admin"
    assert [q["key"] for q in s["products"]] == ["prompt_employees"] and s["tenants_missing"] == 0 and s["tenants_checked"]
    assert client.post("/api/usage/prompt/refresh", json={"month": "nope"}).status_code == 400
    assert client.get("/api/commands").json and any(c["id"] == "prompt-security" and c.get("view") == "prompt" for c in client.get("/api/commands").json)


# ── CWS vs EPP: inventory snapshots ────────────────────────────────────────

def test_product_rows_subtracts_same_day_same_site_counts_from_the_largest_bundle_row():
    p = {"report": "server_endpoints", "subtract": ["inventory_cws_servers", "inventory_cws_container_hosts"]}
    rows = [{"report": "server_endpoints", "account_id": "A", "site_id": "S", "date": "2026-10-10", "breakdown": "Complete", "value": 10, "state": "archived"},
            {"report": "server_endpoints", "account_id": "A", "site_id": "S", "date": "2026-10-10", "breakdown": "Control", "value": 1, "state": "archived"},
            {"report": "server_endpoints", "account_id": "A", "site_id": "S", "date": "2026-10-09", "breakdown": "Complete", "value": 10, "state": "archived"},
            {"report": "server_endpoints", "account_id": "A", "site_id": "T", "date": "2026-10-10", "breakdown": "Complete", "value": 4, "state": "archived"},
            {"report": "inventory_cws_servers", "account_id": "A", "site_id": "S", "date": "2026-10-10", "breakdown": "Virtual Machine", "value": 9, "state": "archived"},
            {"report": "inventory_cws_container_hosts", "account_id": "A", "site_id": "S", "date": "2026-10-10", "breakdown": "Container Host", "value": 3, "state": "archived"},
            {"report": "inventory_cws_servers", "account_id": "A", "site_id": "T", "date": "2026-10-10", "breakdown": "Virtual Machine", "value": 9, "state": "archived"}]
    out = {(r["site_id"], r["date"], r["breakdown"]): r["value"] for r in m.product_rows(p, rows)}
    assert out[("S", "2026-10-10", "Complete")] == 0 and out[("S", "2026-10-10", "Control")] == 0     # 12 off 11: the whole day, never negative
    assert out[("S", "2026-10-09", "Complete")] == 10                                                 # no snapshot that day: as reported
    assert out[("T", "2026-10-10", "Complete")] == 0
    assert all(r["value"] == 10 for r in m.product_rows({"report": "server_endpoints", "subtract": []}, rows) if r["date"] == "2026-10-09")
    assert m.reports_for([p, {"report": "inventory_cws_servers", "subtract": []}]) == ["server_endpoints", "inventory_cws_servers", "inventory_cws_container_hosts"]


def test_inventory_snapshot_splits_cws_from_epp_servers(world):
    store, products = world
    mgmt = FakeInventory(_assets())
    st = m.sync_console(FakePQ(["server_endpoints", "xdr_ingested_bytes"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10",
                        mgmt_client_for=lambda code: mgmt)
    assert st["ok"] and st["inventory"]["ok"] and st["inventory"]["assets"] == 4           # one read per coverage value, decommissioned dropped
    assert [c[1]["activeCoverage"] for c in mgmt.calls] == ["CWS"]
    assert st["fetched"]["inventory_cws_servers"] == 1 and st["fetched"]["inventory_cws_container_hosts"] == 1
    assert {"inventory_cws_servers", "inventory_cws_container_hosts"} <= set(st["reports"])
    inv = {r["report"]: r for r in store.daily("2026-10-10", "2026-10-10", reports=["inventory_cws_servers", "inventory_cws_container_hosts"])}
    assert inv["inventory_cws_servers"]["value"] == 2 and inv["inventory_cws_servers"]["breakdown"] == "Virtual Machine" and inv["inventory_cws_servers"]["state"] == "archived"
    assert inv["inventory_cws_container_hosts"]["value"] == 1 and inv["inventory_cws_container_hosts"]["site_id"] == "S1"
    s = m.summary(store, "2026-10", products, rules={"servers": "month_end"})
    a = s["accounts"][0]["products"]
    assert a["servers"]["value"] == 22            # day 10: S1 20 agents − 3 CWS, S2 5
    assert a["cws_servers"]["value"] == 2 and a["cws_container_hosts"]["value"] == 1 and "workstations" not in a
    assert m.summary(store, "2026-10", products)["accounts"][0]["products"]["servers"]["value"] == 24   # peak moves to day 9 (19 + 5)
    # the Srv column alone still needs the snapshot rows to subtract
    only = [p for p in products if p["key"] == "servers"]
    assert m.summary(store, "2026-10", only, rules={"servers": "month_end"})["accounts"][0]["products"]["servers"]["value"] == 22
    d = m.account_detail(store, "2026-10", products, "Staging", "A1")
    assert d["products"]["cws_servers"]["breakdown"] == [{"key": "Virtual Machine", "value": 2}]
    assert d["sites"][0]["products"]["servers"]["value"] == 19 and d["sites"][0]["products"]["cws_servers"]["value"] == 2   # Acme HQ, peak: day 9 (19), day 10 is 20 − 3
    assert m.site_detail(store, "2026-10", products, "Staging", "S1", rules={"servers": "month_end"})["products"]["servers"]["value"] == 17
    rows, _ = m.export_rows(store, "2026-10", products, "site", rules={"servers": "month_end"})
    hq = next(r for r in rows if r["site_id"] == "S1")
    assert hq["servers"] == 17 and hq["cws_servers"] == 2 and hq["cws_container_hosts"] == 1
    h = m.health(store, ["Staging"], products)[0]
    assert h["state"] == "ok" and "inventory_cws_servers" in h["reports"] and "workstation_endpoints" in h["missing"]
    # a second sync the same day replaces the snapshot instead of adding to it
    mgmt.assets = _assets()[1:]
    m.sync_console(FakePQ(["server_endpoints"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10", mgmt_client_for=lambda code: mgmt)
    assert [r["value"] for r in store.daily("2026-10-10", "2026-10-10", reports=["inventory_cws_servers"])] == [1]


def test_inventory_snapshot_only_for_the_current_month_and_failures_are_soft(world):
    store, products = world
    mgmt = FakeInventory(_assets())
    st = m.sync_console(FakePQ(["server_endpoints"], _rows()), store, "Staging", "2026-10", products, today="2026-11-03", mgmt_client_for=lambda code: mgmt)
    assert st["ok"] and st["inventory"] is None and mgmt.calls == []
    st = m.sync_console(FakePQ(["server_endpoints"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10",
                        mgmt_client_for=lambda code: FakeInventory([], fail="HTTP 403: inventory permission"))
    assert st["ok"] and st["inventory"]["ok"] is False and "403" in st["inventory"]["message"]
    assert "inventory_cws_servers" not in st["reports"] and store.daily("2026-10-10", "2026-10-10", reports=["inventory_cws_servers"]) == []
    st = m.sync_console(FakePQ(["server_endpoints"], _rows()), store, "Staging", "2026-10", products, today="2026-10-10",
                        mgmt_client_for=lambda code: (_ for _ in ()).throw(RuntimeError("Console 'Staging' not found")))
    assert st["ok"] and st["inventory"]["ok"] is False and "not found" in st["inventory"]["message"]
    assert m.health(store, ["Staging"], products)[0]["state"] == "ok"


def test_products_config_sources(tmp_path):
    cfg = m.load_products()
    by = {p["key"]: p for p in cfg["products"]}
    assert by["servers"]["source"] == "metering" and by["servers"]["subtract"] == ["inventory_cws_servers", "inventory_cws_container_hosts"]
    assert by["cws_servers"]["source"] == "inventory" and by["cws_servers"]["inventory"]["coverage"] == "CWS"
    assert by["cws_container_hosts"]["inventory"]["sub_categories"] == ["Container Host"] and by["cws_servers"]["inventory"]["exclude_sub_categories"] == ["Container Host"]
    assert by["workstations"]["report"] == "workstation_endpoints" and "container_hosts" not in {p["report"] for p in cfg["products"]}
    bad = tmp_path / "p.json"; bad.write_text(json.dumps({"products": [{"report": "x", "source": "inventory"}]}))
    with pytest.raises(ValueError):
        m.load_products(bad)
    bad.write_text(json.dumps({"products": [{"report": "x", "source": "ldap"}]}))
    with pytest.raises(ValueError):
        m.load_products(bad)


def test_fetch_days_splits_windows_the_api_truncates(world):
    store, products = world
    pq = FakePQ(["server_endpoints"], _rows(), cap=12)        # 20 rows for 10 days: the month in one query is cut
    st = m.sync_console(pq, store, "Staging", "2026-10", products, today="2026-10-10")
    assert st["ok"] and st["fetched"]["server_endpoints"] == 20
    assert sorted({r["date"] for r in store.daily("2026-10-01", "2026-10-31", reports=["server_endpoints"])}) == [f"2026-10-{d:02d}" for d in range(1, 11)]
    windows = [(q.split('date >= "')[1][:10], q.split('date <= "')[1][:10]) for q, _, _ in pq.queries if 'from "server_endpoints"' in q and "sum(value)" in q]
    assert windows[0] == ("2026-10-01", "2026-10-10") and len(windows) >= 3 and all(a <= b for a, b in windows)
    # a small answer is never split
    pq2 = FakePQ(["xdr_ingested_bytes"], _rows())
    m.sync_console(pq2, store, "USEA1", "2026-10", products, today="2026-10-10")
    assert len([q for q, _, _ in pq2.queries if 'from "xdr_ingested_bytes"' in q and "sum(value)" in q]) == 1
    # a single day over the cap cannot be split: the console fails with a clear message instead of storing a partial day
    st = m.sync_console(FakePQ(["server_endpoints"], _rows(), cap=1), store, "EUCE1", "2026-10", products, today="2026-10-10")
    assert not st["ok"] and st["kind"] == "truncated"


def test_client_turns_the_record_cap_warning_into_a_truncated_error():
    class Resp:
        status_code, ok, text = 200, True, ""
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
    class Http:
        def __init__(self, payload): self.payload = payload
        def post(self, *a, **k): return Resp(self.payload)
    full = {"status": "success", "columns": [{"name": "date"}], "values": [["2026-10-01"]],
            "warnings": ["Returned number of records has reached the limit of 100000. You can define a higher 'limit' parameter or use the 'offset' parameter to get more records."]}
    c = m.PowerQueryClient("https://x.sentinelone.net", "tok", sleep=lambda s: None, session=Http(full))
    with pytest.raises(m.MeteringError) as e:
        c.query("| datasource x", "2026-10-01", "2026-10-02")
    assert e.value.kind == "truncated"
    c = m.PowerQueryClient("https://x.sentinelone.net", "tok", sleep=lambda s: None, session=Http({**full, "warnings": []}))
    assert c.query("| datasource x", "2026-10-01", "2026-10-02") == [{"date": "2026-10-01"}]
