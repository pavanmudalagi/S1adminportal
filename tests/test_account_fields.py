"""Unit tests for s1am.account_fields and the Prompt bundle rules — no network calls."""
from s1am import account_fields as af
from s1am import licenses


def test_name_only_payload():
    data, errors = af.build_put_data({"name": " Acme "})
    assert (data, errors) == ({"name": "Acme"}, [])


def test_name_validation():
    assert af.build_put_data({"name": "  "})[1]
    assert af.build_put_data({"name": "a<b"})[1]


def test_nothing_locked_by_default():
    data, errors = af.build_put_data({"usageType": "mssp", "billingMode": "consumption"})
    assert (data, errors) == ({"usageType": "mssp", "billingMode": "consumption"}, [])


def test_locked_fields_rejected_when_listed():
    for key, value in (("usageType", "mssp"), ("billingMode", "consumption")):
        data, errors = af.build_put_data({key: value}, locked=[key])
        assert key not in data and errors


def test_unlocked_fields_validated_against_swagger_enums():
    assert af.build_put_data({"billingMode": "consumption"}, locked=[])[0] == {"billingMode": "consumption"}
    assert af.build_put_data({"usageType": "ir"}, locked=[])[0] == {"usageType": "ir"}
    assert af.build_put_data({"usageType": "partner"}, locked=[])[1]
    assert af.build_put_data({"accountType": "Free"})[1]
    assert af.build_put_data({"accountType": "Trial"})[0] == {"accountType": "Trial"}


def test_unlimited_expiration_sends_null_expiration():
    data, errors = af.build_put_data({"unlimitedExpiration": True, "expiration": "2030-01-01T00:00:00Z"})
    assert errors == [] and data == {"unlimitedExpiration": True, "expiration": None}


def test_expiration_required_when_not_unlimited():
    assert af.build_put_data({"unlimitedExpiration": False})[1]
    data, errors = af.build_put_data({"unlimitedExpiration": False, "expiration": "2030-01-01T23:59:59Z"})
    assert errors == [] and data["expiration"] == "2030-01-01T23:59:59Z"


def test_identifier_maps_to_salesforce_id():
    assert af.build_put_data({"salesforceId": " 0011x "})[0] == {"salesforceId": "0011x"}


def test_prompt_bundle_catalog_matches_live_staging():
    by_name = {p["name"]: p for p in licenses.PROMPT_BUNDLES}
    assert by_name["prompt_for_ai_code_assistants"]["surface"] == "Total Developers"
    assert by_name["prompt_employees"]["surface"] == "Total Employees"
    # The guide's metering names are read-aliases only, never written.
    assert licenses.is_prompt_bundle("ai_security_employees_usage")
    assert licenses.is_prompt_bundle("ai_security_developers_usage")


def test_prompt_bundles_allow_unlimited():
    for name in ("prompt_employees", "ai_security_employees_usage"):   # canonical name and alias
        bundle = {"name": name, "surfaces": [{"name": "Total Employees", "count": -1}]}
        assert licenses.validate_licenses([bundle], []) == []


def test_prompt_bundles_are_not_skus():
    assert licenses.resolve_sku("prompt_employees") is None
    assert "promptBundles" in licenses.catalog()


def test_s1_region_from_cluster_token():
    assert licenses.s1_region("https://use1-x.sentinelone.net") == "us-east-1"
    assert licenses.s1_region("https://cac1-x.sentinelone.net") == "ca-central-1"
    assert licenses.s1_region("https://euc1-x.sentinelone.net") == "eu-central-1"
    assert licenses.s1_region("https://apse1-x.sentinelone.net") == "ap-southeast-1"
    assert licenses.s1_region("https://aps1-x.sentinelone.net") == "ap-south-1"
    assert licenses.s1_region("https://apse2-x.sentinelone.net") == "ap-southeast-2"
    # Other spellings are not inferred.
    assert licenses.s1_region("https://cace1-x.sentinelone.net") is None
    assert licenses.s1_region("https://example.com") is None
    assert set(licenses.S1_REGION_BY_CLUSTER.values()) == set(licenses.PROMPT_REGION_BY_S1_REGION)


def test_prompt_region_is_api_enum_without_uswest():
    assert set(licenses.PROMPT_REGIONS) == {"useast", "eunorth", "apsouth", "apsoutheast"}
    assert set(licenses.PROMPT_REGION_BY_S1_REGION.values()) == set(licenses.PROMPT_REGIONS)
    assert licenses.prompt_region("https://use1-x.sentinelone.net") == "useast"
    assert licenses.prompt_region("https://cac1-x.sentinelone.net") == "useast"
    assert licenses.prompt_region("https://euc1-x.sentinelone.net") == "eunorth"
    assert licenses.prompt_region("https://aps1-x.sentinelone.net") == "apsouth"
    assert licenses.prompt_region("https://apse2-x.sentinelone.net") == "apsoutheast"
    assert licenses.prompt_region("https://usea1-nabl-staging.sentinelone.net") is None


def test_prompt_console_routes_us_mssp_to_uswest():
    assert licenses.prompt_console("useast", "us-east-1", "mssp") == "https://uswest.prompt.security"
    assert licenses.prompt_console("useast", "us-east-1", "customer") == "https://useast.prompt.security"
    assert licenses.prompt_console("useast", "ca-central-1", "mssp") == "https://useast.prompt.security"
    assert licenses.prompt_console("eunorth", "eu-central-1", "mssp") == "https://eunorth.prompt.security"
    assert licenses.prompt_console("useast") == "https://useast.prompt.security"
    assert licenses.prompt_console("mars") is None


# ── build_create_payload / build_prompt_fields ───────────────────────────────

_PROMPT = {"name": "prompt_employees", "surfaces": [{"name": "Total Employees", "count": 5}]}
_PF = {"promptTenantName": "example_msp", "promptUsers": [
    {"promptUserName": "Alex Admin", "promptUserEmail": "alex@example.com"}]}


def test_create_defaults_and_required_name():
    data, errors = af.build_create_payload({"name": "Acme", "unlimitedExpiration": True})
    assert errors == []
    assert data == {"inherits": True, "name": "Acme", "accountType": "Paid", "usageType": "customer",
                    "billingMode": "subscription", "unlimitedExpiration": True,
                    "licenses": {"bundles": [], "modules": [], "settings": []}}
    assert af.build_create_payload({"unlimitedExpiration": True})[1] == ["Account name is required"]


def test_create_identifier_goes_to_external_id():
    data, _ = af.build_create_payload({"name": "A", "unlimitedExpiration": True, "externalId": " 0011x "})
    assert data["externalId"] == "0011x" and "salesforceId" not in data


def test_create_expiration_required_unless_unlimited():
    assert "Expiration date is required unless the account does not expire" in af.build_create_payload({"name": "A"})[1]
    data, errors = af.build_create_payload({"name": "A", "expiration": "2030-01-01T23:59:59Z"})
    assert errors == [] and data["expiration"] == "2030-01-01T23:59:59Z" and data["unlimitedExpiration"] is False


def test_create_prompt_requires_fields_and_vice_versa():
    _, errors = af.build_create_payload({"name": "A", "unlimitedExpiration": True, "bundles": [_PROMPT]})
    assert errors == ["Prompt Security details are required when a Prompt SKU is selected"]
    _, errors = af.build_create_payload({"name": "A", "unlimitedExpiration": True, "promptFields": _PF})
    assert errors == ["Prompt Security details were given but no Prompt SKU is selected"]


def test_create_with_prompt_single_user_collapses_to_pair():
    data, errors = af.build_create_payload(
        {"name": "A", "unlimitedExpiration": True, "bundles": [_PROMPT], "promptFields": _PF})
    assert errors == []
    assert data["promptFields"] == {"promptTenantName": "example_msp",
                                    "promptUserName": "Alex Admin", "promptUserEmail": "alex@example.com"}


def test_prompt_fields_multiple_users_and_validation():
    pf = {"promptTenantName": "example_msp", "promptUsers": [
        {"promptUserName": "A", "promptUserEmail": "a@x.com"},
        {"promptUserName": "B", "promptUserEmail": "b@x.com"}]}
    out, errors = af.build_prompt_fields(pf, required=True)
    assert errors == [] and "promptUsers" in out and "promptUserName" not in out

    bad = {"promptTenantName": "Example MSP!", "promptRegion": "uswest", "promptUsers": [   # space + "!"
        {"promptUserName": "A", "promptUserEmail": "not-an-email"},
        {"promptUserName": "B", "promptUserEmail": "a@x.com"},
        {"promptUserName": "C", "promptUserEmail": "A@X.COM"}]}
    _, errors = af.build_prompt_fields(bad, required=True)
    assert any("no spaces" in e for e in errors)
    assert af.build_prompt_fields({"promptTenantName": "PavanTestAccount",
        "promptUsers": [{"promptUserName": "A", "promptUserEmail": "a@x.com"}]}, required=True)[1] == []
    assert any("Invalid" in e for e in errors)
    assert any("Duplicate" in e for e in errors)
    assert any("Prompt region must be one of" in e for e in errors)   # uswest is not a valid value
    assert af.build_prompt_fields({"promptTenantName": "t"}, required=True)[1] == [
        "At least one Prompt first user is required"]
    assert af.build_prompt_fields(None, required=False) == (None, [])


def test_create_applies_license_rules():
    _, errors = af.build_create_payload({"name": "A", "unlimitedExpiration": True,
                                         "bundles": [{"name": "unified_identity", "surfaces": []}]})
    assert any("Identity Security requires" in e for e in errors)


def test_bundle_not_in_scope_error_is_explained():
    from s1am.components.app import _explain_api_error, _api_error_details
    from s1am.api.exceptions import SentinelOneError
    exc = SentinelOneError("HTTP 400", 400, {"errors": [{"code": 4000010, "title": "Validation Error",
        "detail": "prompt_for_ai_code_assistants Bundle is not available in your scope because it is not available in the parent scope."}]})
    msg = _explain_api_error(exc)
    assert msg.startswith("prompt_for_ai_code_assistants is not enabled on this console")
    assert "channel team" in msg
    # Same generic code 4000010 with an unrelated detail must NOT be classified as "not enabled".
    other = SentinelOneError("HTTP 400", 400, {"errors": [{"code": 4000010, "title": "Validation Error",
        "detail": "data: promptFields: promptTenantName: Invalid value"}]})
    assert _explain_api_error(other) is None
    assert _api_error_details(other) == ["Validation Error: data: promptFields: promptTenantName: Invalid value"]
    assert _explain_api_error(SentinelOneError("boom")) is None


def test_prompt_region_is_not_sent():
    pf = {"promptTenantName": "t", "promptUsers": [{"promptUserName": "A", "promptUserEmail": "a@x.com"}]}
    out, errors = af.build_prompt_fields(pf, required=True)
    assert errors == [] and "promptRegion" not in out
    # Even when given (or derivable), it is validated but dropped: the API rejects the field.
    out, errors = af.build_prompt_fields({**pf, "promptRegion": "useast"}, required=True, default_region="eunorth")
    assert errors == [] and "promptRegion" not in out
    assert af.build_prompt_fields({**pf, "promptRegion": "uswest"}, required=True)[1] == [
        "Prompt region must be one of: useast, eunorth, apsouth, apsoutheast"]
    body = {"name": "A", "unlimitedExpiration": True, "usageType": "mssp", "bundles": [_PROMPT], "promptFields": pf}
    data, errors = af.build_create_payload(body, base_url="https://usea1-nabl-staging.sentinelone.net")
    assert errors == [] and data["promptFields"] == {"promptTenantName": "t", "promptUserName": "A", "promptUserEmail": "a@x.com"}


def test_prompt_region_sent_only_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(af, "PROMPT_SEND_REGION", True)
    pf = {"promptTenantName": "t", "promptUsers": [{"promptUserName": "A", "promptUserEmail": "a@x.com"}]}
    out, errors = af.build_prompt_fields(pf, required=True, default_region="useast")
    assert errors == [] and out["promptRegion"] == "useast"
    assert "Prompt region is required" in af.build_prompt_fields(pf, required=True)[1][0]


def test_account_save_sends_prompt_fields_only_when_given():
    import s1am.components.app as a
    from s1am.api.exceptions import SentinelOneError
    calls = []

    class C:
        def request(self, m, path, params=None, payload=None):
            calls.append((m, path, payload)); return {"data": {}}

    a._build_client_for_console = lambda c: C()
    a._editor_target = lambda b: ("x", "1")
    client = a.app.test_client()
    bundles = [{"name": "prompt_employees", "surfaces": [{"name": "Total Employees", "count": 5}]}]

    r = client.post("/api/account-save", json={"name": "N", "bundles": bundles, "modules": [], "settings": [],
        "promptFields": {"promptTenantName": "n_tenant",
                         "promptUsers": [{"promptUserName": "P", "promptUserEmail": "p@x.com"}]}})
    assert r.status_code == 200
    assert calls[-1][2]["data"]["promptFields"] == {
        "promptTenantName": "n_tenant", "promptUserName": "P", "promptUserEmail": "p@x.com"}

    r = client.post("/api/account-save", json={"name": "N", "bundles": bundles, "modules": [], "settings": []})
    assert r.status_code == 200 and "promptFields" not in calls[-1][2]["data"]

    r = client.post("/api/account-save", json={"name": "N",
        "promptFields": {"promptTenantName": "bad name", "promptUsers": []}})
    assert r.status_code == 400 and "no spaces" in r.json["error"]
