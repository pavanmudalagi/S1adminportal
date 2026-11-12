"""Unit tests for the Forms workbook parser (s1am.eap.forms) — no files, no network."""
from s1am.eap import forms

JOE_HEADERS = ["Id", "Start time", "Completion time", "Email", "Name", "Provision Status", "Name1", "Company",
               "Contact Email\xa0", "Super Admin Full Name and Email address\n\nWe need you to…",
               "Which products are you interested in for early access?",
               "Please provide the following additional details:\nConsole URL:\xa0\nSentinelOne account…"]
RAW_HEADERS = ["ID", "Start time", "Completion time", "Email", "Name", "Last modified time", "Name2", "Company",
               "Contact Email\xa0", "Super Admin Full Name…", "Which products…", "Please provide…"]


def test_map_headers_joes_copy():
    m = forms.map_headers(JOE_HEADERS)
    assert m == {0: "id", 1: "start_time", 2: "completion_time", 3: "responder_email", 4: "joe_ref",
                 5: "joe_status", 6: "contact_name", 7: "company", 8: "contact_email", 9: "super_admin_raw",
                 10: "products_raw", 11: "details_raw"}


def test_map_headers_raw_export():
    m = forms.map_headers(RAW_HEADERS)
    assert m[5] == "last_modified" and m[6] == "contact_name" and m[4] == "joe_ref"
    assert "joe_status" not in m.values()


def test_normalize_products():
    raw = ("Prompt Security (Name and Email shared in this form will be used for creating the Prompt Tenant);"
           "Identity Security;HyperAutomation;Wayfinder MDR Essentials (350 unit minimum);Prompt Security;")
    assert forms.normalize_products(raw) == ["Prompt Security", "Identity Security", "Hyperautomation",
                                             "Wayfinder MDR Essentials"]
    assert forms.normalize_products("Idenity Security") == ["Identity Security"]
    assert forms.normalize_products(None) == []


def test_super_admin_shapes():
    sa = forms.parse_super_admin
    assert sa("Wey Ho who@icmtel.com.au", "Contact", "wey.ho@icmtel.com.au") == {
        "name": "Wey Ho", "email": "who@icmtel.com.au", "source": "super_admin", "needs_review": False, "note": ""}
    assert sa("David Bricca - dave@unatechnical.com")["name"] == "David Bricca"
    assert sa("Matt Canavan, matthew@infotank.com")["name"] == "Matt Canavan"
    assert sa("Network team Networkteam@computit.com.au")["name"] == "Network team"
    bare = sa("chris@mann.cloud", "Chris Mann", "chris@mann.cloud")
    assert (bare["name"], bare["source"]) == ("Chris Mann", "mixed")
    none = sa("Scott Dunseth", "Scott Dunset", "scottd@entrerock.com")
    assert none["needs_review"] and none["email"] is None and none["name"] == "Scott Dunseth"


def test_super_admin_blank_falls_back_to_contact():
    r = forms.parse_super_admin("", "Jess Walker", "jess@holocroncyber.com.au")
    assert (r["name"], r["email"], r["source"], r["needs_review"]) == (
        "Jess Walker", "jess@holocroncyber.com.au", "contact", False)
    assert forms.parse_super_admin(None, "X", "not-an-email")["needs_review"]


def test_super_admin_domain_mismatch_is_a_note_not_a_block():
    r = forms.parse_super_admin("SentinalOnePrompt@assembledintelligence.co", "Paul", "paul@assembledintelilligence.com")
    assert not r["needs_review"] and "differs" in r["note"]


def test_details_scope_id_url_and_guid_name():
    d = forms.parse_details("https://usea1-swprd5.sentinelone.net/dashboard?_scopeId=2111911476725203112"
                            "&_scopeLevel=account&_categoryId=dashboards\n\nMinds Holdings LLC (0a7d6a5c-9f57-4c61-8cf2-50f01868c7fc) ")
    assert d["hosts"] == ["usea1-swprd5.sentinelone.net"]
    assert d["account_ids"] == ["2111911476725203112"]
    assert d["guids"] == ["0a7d6a5c-9f57-4c61-8cf2-50f01868c7fc"]
    assert d["leftover"] == "Minds Holdings LLC"
    assert not d["no_s1_claimed"]


def test_details_tenant_is_not_also_an_account_id():
    d = forms.parse_details("1000_USEA3_2066133408163243175")
    assert d["tenants"] == ["1000_USEA3_2066133408163243175"] and d["account_ids"] == [] and d["leftover"] == ""


def test_details_labelled_text():
    d = forms.parse_details("Current SentinelOne details:\nConsole URL:  https://usea1-swprd2.sentinelone.net\n"
                            "Account Name:  United Network Associates, Inc. (0ddd1ada-8887-4b91-aa23-c865955406f9)\n"
                            "Account ID:  1873828903646433224")
    assert d["hosts"] == ["usea1-swprd2.sentinelone.net"]
    assert d["account_ids"] == ["1873828903646433224"]
    assert d["leftover"] == "United Network Associates Inc."


def test_details_mname_and_host_only():
    d = forms.parse_details("Account Name: 1000M-0000-0000-UK_e7df84de-5c5a-47ea-b123-605ccbe1cef7 Account ID: 1691212601751151504")
    assert d["s1_names"] == ["1000M-0000-0000-UK_e7df84de-5c5a-47ea-b123-605ccbe1cef7"]
    assert d["account_ids"] == ["1691212601751151504"] and d["guids"] == []
    assert forms.parse_details("https://euce1-swprd6.sentinelone.net/login")["hosts"] == ["euce1-swprd6.sentinelone.net"]


def test_details_no_s1_claimed():
    for text in ("Don't know", "We do not have sentinel one", "dont have one ",
                 "Not currently an SentinelOne user.  N-Central/Mail Assure only"):
        assert forms.parse_details(text)["no_s1_claimed"], text
    assert not forms.parse_details("Sentinel one account is linked to n-Able.")["no_s1_claimed"]
    assert forms.parse_details(None) == {"hosts": [], "account_ids": [], "guids": [], "tenants": [], "s1_names": [],
                                         "leftover": "", "no_s1_claimed": False}


def test_company_and_tenant_name_helpers():
    assert forms.normalize_company("Syndeticom Pty Ltd") == "Syndeticom"
    assert forms.normalize_company("United Network Associates, Inc.") == "United Network Associates"
    assert forms.normalize_company("Hudson Automotive Group") == "Hudson Automotive"
    assert forms.normalize_company("i2p") == "i2p"
    assert forms.suggest_tenant_name("Comput IT Pty Ltd") == "comput_it_pty_ltd"
    assert forms.suggest_tenant_name("Entré Computer Solutions") == "entr_computer_solutions"
