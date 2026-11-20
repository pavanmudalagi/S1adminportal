"""Web shell routes that every page relies on."""
import pytest


@pytest.fixture
def api(monkeypatch):
    import s1am.components.app as a
    from s1am.api.config import Config
    monkeypatch.setattr(Config, "list_profiles", classmethod(lambda cls: ["Staging", "EUCE1"]))
    monkeypatch.setattr(Config, "section", classmethod(lambda cls, name: {"Staging": {"s1_base_url": "https://usea1-nabl-staging.sentinelone.net/"},
                                                                          "EUCE1": {"s1_api_token": "secret-not-returned"}}.get(name, {})))
    monkeypatch.setattr(a, "_load_labels", lambda: {"Staging": "Staging console"})
    return a.app.test_client()


def test_profiles_carry_console_url_but_never_tokens(api):
    r = api.get("/api/profiles")
    assert r.status_code == 200
    profiles = {p["id"]: p for p in r.json["profiles"]}
    assert profiles["Staging"] == {"id": "Staging", "name": "Staging console", "url": "https://usea1-nabl-staging.sentinelone.net"}
    assert profiles["EUCE1"]["url"] == "" and "secret" not in r.data.decode()


class _FakeAccountsClient:
    """GET /accounts with a cursor, two pages, the real response shape."""

    def __init__(self):
        self.calls = []

    def paginate(self, path, params=None, page_limit=None):
        pages, params = [], dict(params or {})
        while True:
            pages.append(self.request("GET", path, params=params))
            nxt = pages[-1]["pagination"]["nextCursor"]
            if not nxt:
                return pages
            params["cursor"] = nxt

    def request(self, method, path, params=None, payload=None):
        self.calls.append(dict(params or {}))
        assert path == "accounts" and params["limit"] == 1000
        page2 = params.get("cursor") == "c1"
        acc = lambda i, **kw: {"id": str(i), "name": kw.get("name", f"Acct {i}"), "state": kw.get("state", "active"), "accountType": "Paid",
                               "usageType": kw.get("usage", "customer"), "billingMode": "subscription", "activeAgents": i * 10, "numberOfSites": 1,
                               "createdAt": "2023-11-14T10:00:00.000000Z", "expiration": kw.get("exp"), "unlimitedExpiration": kw.get("exp") is None,
                               "salesforceId": "001X", "licenses": {
                                   "bundles": [{"displayName": "Endpoint Security - Complete", "name": "complete", "totalSurfaces": kw.get("surf", 2000), "surfaces": []},
                                               {"displayName": "CNS Pro", "name": "cns_pro", "totalSurfaces": 1}],
                                   "modules": [{"displayName": "Vulnerability Management", "name": "vm"}, {"displayName": "Remote Script Orchestration", "name": "rso"}] if kw.get("vm", True) else [],
                                   "settings": [{"settingGroupDisplayName": "Remote Shell", "displayName": "Enabled"},
                                                {"settingGroupDisplayName": "Deep Visibility Data Retention", "displayName": "14 Days"},
                                                {"settingGroupDisplayName": "Zzz Custom", "displayName": "x"},
                                                {"settingGroupDisplayName": "Malicious Data Retention", "displayName": "365 Days"}]}}
        data = [acc(3, usage="mssp", surf=-1, exp="2033-11-17T05:29:00Z")] if page2 else [acc(1, name="Re-Solved"), acc(2, vm=False, state="expired")]
        return {"data": data, "pagination": {"nextCursor": None if page2 else "c1", "totalItems": 3}}


@pytest.fixture
def acct_api(monkeypatch):
    import s1am.components.app as a
    fake = _FakeAccountsClient()
    monkeypatch.setattr(a, "_build_client_for_console", lambda code: fake)
    monkeypatch.setattr(a, "_load_labels", lambda: {"USEA1": "US East 1"})
    a._ACCT_CACHE.clear()
    from s1am.api.config import Config
    monkeypatch.setattr(Config, "section", classmethod(lambda cls, name: {"s1_base_url": "https://usea1-x.sentinelone.net"}))
    return a.app.test_client(), fake


def test_accounts_table_normalises_and_caches(acct_api):
    api, fake = acct_api
    assert api.get("/api/accounts/table").status_code == 400           # no active console, none named
    r = api.get("/api/accounts/table?console=USEA1")
    assert r.status_code == 200
    d = r.json
    assert d["console"] == "USEA1" and d["label"] == "US East 1" and d["console_url"] == "https://usea1-x.sentinelone.net"
    assert d["total"] == 3 and len(fake.calls) == 2 and fake.calls[1]["cursor"] == "c1" and d["cached"] is False
    rows = {x["id"]: x for x in d["rows"]}
    one = rows["1"]
    assert one["name"] == "Re-Solved" and one["skus"] == [{"name": "Endpoint Security - Complete", "qty": 2000}, {"name": "CNS Pro", "qty": 1}]
    assert one["totalSurfaces"] == 2001 and one["expiration"] is None and one["modules"] == ["Vulnerability Management", "Remote Script Orchestration"]
    # settings in our fixed order, then the rest alphabetically
    assert [s["label"] for s in one["settings"]] == ["Deep Visibility Data Retention", "Malicious Data Retention", "Remote Shell", "Zzz Custom"]
    three = rows["3"]
    assert three["totalSurfaces"] == -1 and three["skus"][0]["qty"] is None and three["expiration"] == "2033-11-17T05:29:00Z" and three["usageType"] == "mssp"
    assert rows["2"]["modules"] == [] and rows["2"]["state"] == "expired"
    # second read is served from the cache; refresh=1 re-reads
    assert api.get("/api/accounts/table?console=USEA1").json["cached"] is True and len(fake.calls) == 2
    assert api.get("/api/accounts/table?console=USEA1&refresh=1").json["cached"] is False and len(fake.calls) == 4
    assert "secret" not in r.data.decode()
