"""Unit tests for the console cross-check (s1am.eap.s1check) with a fake SentinelOne client."""
from s1am.eap import s1check

HOSTS = {"usea1-swprd5.sentinelone.net": "USEA3", "euce1-swprd6.sentinelone.net": "EUCE2"}


def _details(**kw):
    d = {"hosts": [], "account_ids": [], "guids": [], "tenants": [], "s1_names": [], "leftover": "", "no_s1_claimed": False}
    d.update(kw)
    return d


def test_host_codes_exact_hostname():
    assert s1check.host_codes({"USEA3": "https://usea1-swprd5.sentinelone.net/", "SF": ""}) == {
        "usea1-swprd5.sentinelone.net": "USEA3"}


def test_resolve_target_order():
    rt = s1check.resolve_target
    assert rt({"chosen_console": "USEA1", "chosen_account_id": 5, "sf_tenant": "1000_EUCE1_9"}, HOSTS)["source"] == "chosen"
    # What the customer wrote on the form outranks the Salesforce tenant …
    assert rt({"sf_tenant": "1000_EUCE1_9", "details": _details(tenants=["1000_USEA1_1"])}, HOSTS) == {
        "console": "USEA1", "account_id": "1", "source": "form-tenant"}
    assert rt({"details": _details(tenants=["1000_USEA1_1"])}, HOSTS)["source"] == "form-tenant"
    # … but a manual choice outranks both, and the SF tenant is still kept as a fallback.
    srcs = [t["source"] for t in s1check.resolve_targets(
        {"chosen_console": "A", "chosen_account_id": 1, "sf_tenant": "1000_EUCE1_9",
         "details": _details(hosts=["usea1-swprd5.sentinelone.net"], leftover="Acme")}, HOSTS)]
    assert srcs == ["chosen", "console-lookup", "sf-tenant"]
    assert rt({"details": _details(hosts=["usea1-swprd5.sentinelone.net"], account_ids=["42"])}, HOSTS) == {
        "console": "USEA3", "account_id": "42", "source": "form-id"}
    t = rt({"details": _details(hosts=["euce1-swprd6.sentinelone.net"], guids=["7ac3be2f-993f-43bc-a391-94e3b56f264c"],
                                leftover="Karibu Networks")}, HOSTS)
    assert t["source"] == "console-lookup" and t["name_hint"] == "7ac3be2f-993f-43bc-a391-94e3b56f264c"
    assert rt({"details": _details(account_ids=["42"])}, HOSTS)["source"] == "id-scan"
    assert "not a saved profile" in rt({"details": _details(hosts=["apne1-x.sentinelone.net"])}, HOSTS)["error"]
    assert "error" in rt({"details": _details()}, HOSTS)


def test_prompt_info():
    acct = {"licenses": {"bundles": [{"name": "complete"}, {"name": "prompt_employees"}]},
            "promptFields": {"promptTenantName": "acme", "promptUserName": "A", "promptUserEmail": "a@x.io",
                             "promptUsers": [{"promptUserName": "A", "promptUserEmail": "a@x.io"},
                                             {"promptUserName": "B", "promptUserEmail": "b@x.io"}]}}
    info = s1check.prompt_info(acct)
    assert info["enabled"] and info["bundles"] == ["prompt_employees"] and info["tenant"] == "acme"
    assert [u["email"] for u in info["users"]] == ["a@x.io", "b@x.io"]
    assert s1check.prompt_info({"licenses": {"bundles": [{"name": "complete"}]}}) == {
        "enabled": False, "bundles": [], "tenant": None, "users": [], "region": None}


class _FakeClient:
    def __init__(self, accounts, fail=None):
        self.accounts = {str(a["id"]): a for a in accounts}
        self.fail = fail
        self.calls = []

    def request(self, method, path, params=None, payload=None):
        self.calls.append((method, path, params))
        if self.fail:
            raise RuntimeError(self.fail)
        if path.startswith("accounts/"):
            a = self.accounts.get(path.split("/")[1])
            if not a:
                raise RuntimeError("HTTP 404")
            return {"data": a}
        if params and params.get("ids"):
            return {"data": [a for i, a in self.accounts.items() if i == str(params["ids"])]}
        if params and params.get("name"):
            return {"data": [a for a in self.accounts.values() if params["name"].lower() in a["name"].lower()]}
        return {"data": []}


PROMPTED = {"id": 42, "name": "Acme", "state": "active",
            "licenses": {"bundles": [{"name": "complete", "displayName": "Endpoint Security - Complete"},
                                     {"name": "prompt_employees", "displayName": "Prompt for Employees"}]},
            "promptFields": {"promptTenantName": "acme", "promptUsers": [{"promptUserName": "Jo", "promptUserEmail": "jo@acme.com"}]}}
PLAIN = {"id": 77, "name": "Karibu Networks 7ac3be2f-993f-43bc-a391-94e3b56f264c", "state": "active",
         "licenses": {"bundles": [{"name": "control", "displayName": "Endpoint Security - Control"}]}}


def test_check_request_prompt_enabled():
    client = _FakeClient([PROMPTED])
    r = {"details": _details(hosts=["usea1-swprd5.sentinelone.net"], account_ids=["42"])}
    f = s1check.check_request(r, lambda code: client, HOSTS)
    assert f["s1_console"] == "USEA3" and f["s1_prompt_enabled"] == 1 and f["s1_prompt_tenant"] == "acme"
    assert f["s1_prompt_users"] == [{"name": "Jo", "email": "jo@acme.com"}]
    assert f["s1_skus"] == ["Endpoint Security - Complete", "Prompt for Employees"] and f["s1_error"] is None


def test_check_request_console_lookup_by_guid_and_ambiguity():
    client = _FakeClient([PLAIN, dict(PLAIN, id=78, name="Karibu Networks old")])
    r = {"details": _details(hosts=["euce1-swprd6.sentinelone.net"], guids=["7ac3be2f-993f-43bc-a391-94e3b56f264c"],
                             leftover="Karibu Networks")}
    f = s1check.check_request(r, lambda code: client, HOSTS)
    assert f["s1_target_source"] == "console-lookup" and f["s1_account_id"] == "77" and f["s1_prompt_enabled"] == 0
    byname = _FakeClient([dict(PLAIN, id=79, name="Comput IT Pty Ltd")])
    r3 = {"details": _details(hosts=["euce1-swprd6.sentinelone.net"], guids=["f92295bd-585e-4425-ac80-a80ff2c96098"],
                              leftover="Comput IT Pty Ltd")}
    assert s1check.check_request(r3, lambda code: byname, HOSTS)["s1_account_id"] == "79"
    r2 = {"details": _details(hosts=["euce1-swprd6.sentinelone.net"], leftover="Karibu Networks")}
    f2 = s1check.check_request(r2, lambda code: client, HOSTS)
    assert "2 accounts match" in f2["s1_error"] and f2["s1_account_id"] is None


def test_check_request_id_scan_and_not_found():
    clients = {"A": _FakeClient([]), "B": _FakeClient([PROMPTED])}
    f = s1check.check_request({"details": _details(account_ids=["42"])}, lambda c: clients[c], {}, all_codes=["A", "B"])
    assert f["s1_console"] == "B" and f["s1_prompt_enabled"] == 1
    f = s1check.check_request({"details": _details(account_ids=["1"])}, lambda c: clients[c], {}, all_codes=["A", "B"])
    assert "not found on any saved console" in f["s1_error"]
    f = s1check.check_request({"sf_tenant": "1000_A_5"}, lambda c: clients["A"], {})
    assert f["s1_error"] == "account 5 not found on A"


def test_check_request_falls_back_to_sf_tenant_when_form_lookup_fails():
    clients = {"EUCE2": _FakeClient([]), "USEA1": _FakeClient([PROMPTED])}
    r = {"sf_tenant": "1000_USEA1_42",
         "details": _details(hosts=["euce1-swprd6.sentinelone.net"], leftover="Nobody Here")}
    f = s1check.check_request(r, lambda c: clients[c], HOSTS)
    assert f["s1_target_source"] == "sf-tenant" and f["s1_console"] == "USEA1" and f["s1_prompt_enabled"] == 1
    none = s1check.check_request({"sf_tenant": "1000_USEA1_7", "details": _details(hosts=["euce1-swprd6.sentinelone.net"], leftover="Nobody")},
                                 lambda c: clients[c], HOSTS)
    assert "0 accounts match" in none["s1_error"] and "not found on USEA1" in none["s1_error"]


def test_check_request_console_error_is_captured():
    f = s1check.check_request({"sf_tenant": "1000_A_5"}, lambda c: _FakeClient([], fail="HTTP 401 unauthorized"), {})
    assert "401" in f["s1_error"] and f["s1_console"] == "A"
