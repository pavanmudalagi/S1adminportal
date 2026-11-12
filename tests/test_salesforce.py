"""Unit tests for s1am.salesforce (auth + client) — no network, no browser."""
import base64
import hashlib
import json
import os
import stat
from types import SimpleNamespace

import pytest

from s1am.salesforce import auth, client as sf


@pytest.fixture
def token_env(tmp_path, monkeypatch):
    """Point the token store at a temp file and hide the dashboard's cache."""
    path = tmp_path / "sf_tokens.json"
    monkeypatch.setenv("SF_TOKEN_FILE", str(path))
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN_FILE", tmp_path / "no-dashboard.json")
    monkeypatch.delenv("SF_USERNAME", raising=False)
    monkeypatch.delenv("SF_PASSWORD", raising=False)
    return path


# ── settings ────────────────────────────────────────────────────────────────

def test_setting_precedence_env_over_file_over_default(monkeypatch):
    monkeypatch.setattr(auth.Config, "section",
                        classmethod(lambda cls, name: {"login_url": "https://file.my.salesforce.com"}))
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)
    assert auth.setting("login_url") == "https://file.my.salesforce.com"
    monkeypatch.setenv("SF_LOGIN_URL", "https://env.my.salesforce.com ")
    assert auth.setting("login_url") == "https://env.my.salesforce.com"
    monkeypatch.setattr(auth.Config, "section", classmethod(lambda cls, name: {}))
    monkeypatch.delenv("SF_LOGIN_URL", raising=False)
    assert auth.setting("login_url") == auth.DEFAULTS["login_url"]


def test_empty_prompt_env_disables_prompt(monkeypatch):
    monkeypatch.setenv("SF_PROMPT", "")
    assert auth.setting("prompt") == ""


# ── token store ─────────────────────────────────────────────────────────────

def test_token_round_trip_is_private(token_env):
    assert auth.load_token() is None
    auth.save_token({"access_token": "a", "refresh_token": "r", "instance_url": "https://x", "id": "https://login/id/00D/005U"})
    assert auth.load_token()["refresh_token"] == "r"
    assert stat.S_IMODE(os.stat(token_env).st_mode) == 0o600
    assert auth.status().connected and auth.status().identity == "005U"


def test_clear_token_signs_out_without_reimport(token_env, tmp_path, monkeypatch):
    dash = tmp_path / "dash.json"
    dash.write_text(json.dumps({"salesforce": {"access_token": "d", "refresh_token": "dr", "instance_url": "https://d"}}))
    monkeypatch.setattr(auth, "DASHBOARD_TOKEN_FILE", dash)
    # First use with no s1am file imports the dashboard session once…
    assert auth.load_token()["refresh_token"] == "dr"
    assert token_env.exists()
    # …and signing out must not silently bring it back.
    auth.clear_token()
    assert auth.load_token() is None
    assert not auth.status().connected


def test_corrupt_token_file_is_ignored(token_env):
    token_env.write_text("{not json")
    assert auth.load_token() is None


def test_pkce_challenge_matches_verifier():
    verifier, challenge = auth._make_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in verifier


def test_session_refreshes_when_access_token_missing(token_env, monkeypatch):
    auth.save_token({"refresh_token": "r", "instance_url": "https://x"})
    monkeypatch.setattr(auth, "refresh", lambda: {"access_token": "new", "instance_url": "https://x"})
    assert auth.session()["access_token"] == "new"


# ── client ──────────────────────────────────────────────────────────────────

class _FakeSF:
    def __init__(self, results=None, fail_first=None):
        self.results = results or []
        self.fail_first = fail_first
        self.queries = []

    def query_all(self, q):
        self.queries.append(q)
        if self.fail_first:
            err, self.fail_first = self.fail_first, None
            raise Exception(err)
        return {"records": self.results}

    def restful(self, path, params):
        return {"username": "me@example.com", "displayName": "Me", "id": "005"}

    def __getattr__(self, name):
        if name.endswith("__c") or name in ("Account", "Contact"):
            return SimpleNamespace(describe=lambda: {"fields": [{"name": "Id"}, {"name": "Name"}]})
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _fresh_client_state():
    sf.reset_client()
    sf.clear_cache()
    yield
    sf.reset_client()
    sf.clear_cache()


def test_run_soql_rejects_non_select(monkeypatch):
    monkeypatch.setattr(sf, "client", lambda: _FakeSF())
    with pytest.raises(ValueError):
        sf.run_soql("DELETE FROM Account")


def test_run_soql_flattens_and_collapses_whitespace(monkeypatch):
    fake = _FakeSF([{"attributes": {"type": "Tenant__c"}, "Id": "a1", "Name": "1000_USEA1_1",
                     "Account__r": {"attributes": {}, "Name": "Acme"}, "Product__r": None}])
    monkeypatch.setattr(sf, "client", lambda: fake)
    rows = sf.run_soql("SELECT Id, Name,\n   Account__r.Name FROM Tenant__c")
    assert rows == [{"Id": "a1", "Name": "1000_USEA1_1", "Account__r.Name": "Acme", "Product__r": None}]
    assert fake.queries == ["SELECT Id, Name, Account__r.Name FROM Tenant__c"]


def test_run_soql_caches_and_bypasses(monkeypatch):
    fake = _FakeSF([{"Id": "1"}])
    monkeypatch.setattr(sf, "client", lambda: fake)
    sf.run_soql("SELECT Id FROM Account")
    sf.run_soql("SELECT Id FROM Account")
    assert len(fake.queries) == 1
    sf.run_soql("SELECT Id FROM Account", use_cache=False)
    assert len(fake.queries) == 2


def test_run_soql_refreshes_once_on_expired_session(monkeypatch):
    fakes = [_FakeSF(fail_first="INVALID_SESSION_ID: Session expired"), _FakeSF([{"Id": "ok"}])]
    monkeypatch.setattr(sf, "client", lambda: fakes[0] if len(fakes) == 2 and not fakes[0].queries else fakes[-1])
    refreshed = []
    monkeypatch.setattr(sf.auth, "refresh", lambda: refreshed.append(1) or {"access_token": "n"})
    assert sf.run_soql("SELECT Id FROM Account") == [{"Id": "ok"}]
    assert refreshed == [1]


def test_run_soql_other_errors_propagate(monkeypatch):
    monkeypatch.setattr(sf, "client", lambda: _FakeSF(fail_first="MALFORMED_QUERY"))
    monkeypatch.setattr(sf.auth, "refresh", lambda: pytest.fail("must not refresh"))
    with pytest.raises(Exception, match="MALFORMED_QUERY"):
        sf.run_soql("SELECT Id FROM Account")


def test_client_requires_credentials(token_env, monkeypatch):
    for key in ("SF_SECURITY_TOKEN",):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(sf.SalesforceConfigError):
        sf.client()


def test_soql_quoting_and_chunking():
    assert sf.soql_in_list(["O'Brien", "a\\b"]) == r"'O\'Brien', 'a\\b'"
    assert [len(c) for c in sf.chunked(range(450), 200)] == [200, 200, 50]


def test_query_in_dedupes_and_batches(monkeypatch):
    fake = _FakeSF([{"Id": "x"}])
    monkeypatch.setattr(sf, "client", lambda: fake)
    values = [f"t{i}" for i in range(250)] + ["t0", "", None]
    rows = sf.query_in("SELECT Id FROM Tenant__c WHERE Name IN ({values})", values, size=200, use_cache=False)
    assert len(fake.queries) == 2 and len(rows) == 2
    assert fake.queries[0].count("'") == 400 and "''" not in fake.queries[0]


def test_whoami_and_describe(monkeypatch):
    monkeypatch.setattr(sf, "client", lambda: _FakeSF())
    assert sf.whoami()["username"] == "me@example.com"
    assert sf.describe_fields("Tenant__c") == {"Id", "Name"}
