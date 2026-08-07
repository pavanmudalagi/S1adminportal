"""Unit tests for s1am.cli — pure logic, no network calls."""
import json
import pytest

from s1am.cli import (
    _cell,
    _compare_periods,
    _extract_rows,
    _flatten_pages,
    _format_csv,
    _format_table,
    _parse_columns,
    _parse_params,
    _trunc,
    _TABLE_MAX_COL,
)


# ── _parse_params ────────────────────────────────────────────────────────────

def test_parse_params_empty():
    assert _parse_params(None) == {}
    assert _parse_params([]) == {}


def test_parse_params_single():
    assert _parse_params(["limit=100"]) == {"limit": "100"}


def test_parse_params_multiple():
    result = _parse_params(["accountId=123", "state=active"])
    assert result == {"accountId": "123", "state": "active"}


def test_parse_params_value_with_equals():
    # Value itself contains '='
    assert _parse_params(["key=a=b"]) == {"key": "a=b"}


def test_parse_params_invalid():
    with pytest.raises(ValueError, match="Invalid param"):
        _parse_params(["noequals"])


# ── _parse_columns ───────────────────────────────────────────────────────────

def test_parse_columns_none():
    assert _parse_columns(None) is None
    assert _parse_columns("") is None


def test_parse_columns_single():
    assert _parse_columns("id") == ["id"]


def test_parse_columns_multiple():
    assert _parse_columns("id,name,status") == ["id", "name", "status"]


def test_parse_columns_strips_whitespace():
    assert _parse_columns(" id , name ") == ["id", "name"]


# ── _cell ────────────────────────────────────────────────────────────────────

def test_cell_none():
    assert _cell(None) == ""


def test_cell_string():
    assert _cell("hello") == "hello"


def test_cell_int():
    assert _cell(42) == "42"


def test_cell_dict():
    result = _cell({"a": 1})
    assert result == '{"a":1}'


def test_cell_list():
    result = _cell([1, 2])
    assert result == "[1,2]"


# ── _trunc ───────────────────────────────────────────────────────────────────

def test_trunc_short():
    assert _trunc("abc", 10) == "abc"


def test_trunc_exact():
    assert _trunc("abcde", 5) == "abcde"


def test_trunc_long():
    result = _trunc("abcdef", 5)
    assert len(result) == 5
    assert result.endswith("…")


# ── _extract_rows ─────────────────────────────────────────────────────────────

def test_extract_rows_none():
    assert _extract_rows(None) == []


def test_extract_rows_flat_list():
    rows = [{"id": 1}, {"id": 2}]
    assert _extract_rows(rows) == rows


def test_extract_rows_data_key():
    data = {"data": [{"id": 1}], "pagination": {}}
    assert _extract_rows(data) == [{"id": 1}]


def test_extract_rows_data_null():
    assert _extract_rows({"data": None}) == []


def test_extract_rows_single_data_dict():
    data = {"data": {"id": 1}}
    assert _extract_rows(data) == [{"id": 1}]


def test_extract_rows_events_key():
    data = {"events": [{"ts": "2026-01-01"}]}
    assert _extract_rows(data) == [{"ts": "2026-01-01"}]


def test_extract_rows_pages():
    pages = [
        {"data": [{"id": 1}, {"id": 2}]},
        {"data": [{"id": 3}]},
    ]
    assert _extract_rows(pages) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_extract_rows_scalar_passthrough():
    assert _extract_rows({"key": "value"}) == [{"key": "value"}]


# ── _flatten_pages ────────────────────────────────────────────────────────────

def test_flatten_pages_single():
    pages = [{"data": [{"id": 1}], "pagination": {}}]
    result = _flatten_pages(pages)
    assert result == {"data": [{"id": 1}], "total": 1}


def test_flatten_pages_multi():
    pages = [
        {"data": [{"id": 1}, {"id": 2}]},
        {"data": [{"id": 3}]},
    ]
    result = _flatten_pages(pages)
    assert result["total"] == 3
    assert len(result["data"]) == 3


def test_flatten_pages_empty():
    result = _flatten_pages([{"data": []}])
    assert result == {"data": [], "total": 0}


# ── _format_table ─────────────────────────────────────────────────────────────

def test_format_table_empty():
    assert _format_table([]) == "(no data)"


def test_format_table_basic():
    rows = [{"name": "Alice", "age": "30"}]
    out = _format_table(rows)
    assert "Alice" in out
    assert "name" in out
    assert "age" in out


def test_format_table_column_selection():
    rows = [{"id": "1", "name": "Alice", "email": "a@b.com"}]
    out = _format_table(rows, columns=["id", "name"])
    assert "id" in out
    assert "name" in out
    assert "email" not in out


def test_format_table_truncation():
    long_value = "x" * (_TABLE_MAX_COL + 20)
    rows = [{"col": long_value}]
    out = _format_table(rows)
    # Each cell should be at most _TABLE_MAX_COL wide
    lines = out.split("\n")
    data_line = lines[3]  # header=lines[1], divider=lines[2], first row=lines[3]
    assert "…" in data_line


def test_format_table_multiple_rows():
    rows = [{"k": "a"}, {"k": "b"}, {"k": "c"}]
    out = _format_table(rows)
    assert "a" in out
    assert "b" in out
    assert "c" in out


# ── _format_csv ───────────────────────────────────────────────────────────────

def test_format_csv_empty():
    assert _format_csv([]) == ""


def test_format_csv_basic():
    rows = [{"name": "Alice", "score": "10"}]
    out = _format_csv(rows)
    lines = out.strip().split("\n")
    assert lines[0] == "name,score"
    assert lines[1] == "Alice,10"


def test_format_csv_column_selection():
    rows = [{"id": "1", "name": "Alice", "email": "a@b.com"}]
    out = _format_csv(rows, columns=["id", "name"])
    assert "id,name" in out
    assert "email" not in out


def test_format_csv_nested_value():
    rows = [{"meta": {"k": "v"}}]
    out = _format_csv(rows)
    # csv wraps the JSON string in quotes and escapes interior quotes per RFC 4180
    assert "k" in out and "v" in out


def test_format_csv_multiple_rows():
    rows = [{"x": "1"}, {"x": "2"}]
    out = _format_csv(rows)
    lines = out.strip().split("\n")
    assert len(lines) == 3  # header + 2 data rows


# ── _compare_periods ──────────────────────────────────────────────────────────

def _make_row(bundle, value, acct="acct1", site="site1"):
    return {
        "organization_level_1_scope_id": acct,
        "organization_level_2_scope_id": site,
        "endpoint_bundle": bundle,
        "value": value,
    }


def test_compare_periods_no_change():
    cur = [_make_row("CORE", 100)]
    prev = [_make_row("CORE", 100)]
    result = _compare_periods(cur, prev)
    assert len(result) == 1
    assert result[0]["delta"] == 0
    assert result[0]["delta_pct"] == "+0.0%"


def test_compare_periods_increase():
    cur = [_make_row("CORE", 120)]
    prev = [_make_row("CORE", 100)]
    result = _compare_periods(cur, prev)
    assert result[0]["delta"] == 20
    assert "+20.0%" in result[0]["delta_pct"]


def test_compare_periods_decrease():
    cur = [_make_row("CORE", 80)]
    prev = [_make_row("CORE", 100)]
    result = _compare_periods(cur, prev)
    assert result[0]["delta"] == -20
    assert "-20.0%" in result[0]["delta_pct"]


def test_compare_periods_new_bundle():
    cur = [_make_row("CORE", 100), _make_row("IDENTITY", 50)]
    prev = [_make_row("CORE", 100)]
    result = _compare_periods(cur, prev)
    bundles = {r["endpoint_bundle"] for r in result}
    assert "IDENTITY" in bundles
    identity = next(r for r in result if r["endpoint_bundle"] == "IDENTITY")
    assert identity["previous_value"] == 0.0
    assert identity["current_value"] == 50


def test_compare_periods_removed_bundle():
    cur = [_make_row("CORE", 100)]
    prev = [_make_row("CORE", 100), _make_row("OLD", 200)]
    result = _compare_periods(cur, prev)
    old = next(r for r in result if r["endpoint_bundle"] == "OLD")
    assert old["current_value"] == 0.0
    assert old["delta"] == -200


def test_compare_periods_zero_prev():
    cur = [_make_row("NEW", 50)]
    prev = [_make_row("NEW", 0)]
    result = _compare_periods(cur, prev)
    assert result[0]["delta_pct"] == "+100.0%"


# ── SentinelOneClient — URL building ─────────────────────────────────────────

from s1am.api.client import SentinelOneClient


def test_build_url_relative():
    c = SentinelOneClient("https://tenant.example.com", "tok")
    assert c._build_url("accounts") == "https://tenant.example.com/web/api/v2.1/accounts"


def test_build_url_with_leading_slash():
    c = SentinelOneClient("https://tenant.example.com", "tok")
    assert c._build_url("/accounts") == "https://tenant.example.com/web/api/v2.1/accounts"


def test_build_url_absolute_passthrough():
    c = SentinelOneClient("https://tenant.example.com", "tok")
    url = "https://other.host/path"
    assert c._build_url(url) == url


def test_build_url_trailing_slash_stripped():
    c = SentinelOneClient("https://tenant.example.com/", "tok")
    assert c._build_url("accounts") == "https://tenant.example.com/web/api/v2.1/accounts"


def test_build_url_custom_api_path():
    c = SentinelOneClient("https://tenant.example.com", "tok", api_path="/web/api/v3.0")
    assert c._build_url("sites") == "https://tenant.example.com/web/api/v3.0/sites"


# ── SentinelOneClient — headers ───────────────────────────────────────────────

def test_headers_no_prefix():
    c = SentinelOneClient("https://x.com", "mytoken")
    h = c._headers()
    assert h["Authorization"] == "mytoken"
    assert "Content-Type" not in h


def test_headers_with_prefix():
    c = SentinelOneClient("https://x.com", "mytoken", auth_prefix="APIToken")
    h = c._headers()
    assert h["Authorization"] == "APIToken mytoken"


def test_headers_json_content_type():
    c = SentinelOneClient("https://x.com", "tok")
    h = c._headers(has_json=True)
    assert h["Content-Type"] == "application/json"


# ── SentinelOneClient — load_json_payload ─────────────────────────────────────

def test_load_json_payload_none():
    assert SentinelOneClient.load_json_payload(None) is None


def test_load_json_payload_valid():
    result = SentinelOneClient.load_json_payload('{"key": "value"}')
    assert result == {"key": "value"}


def test_load_json_payload_invalid():
    with pytest.raises(ValueError, match="Invalid JSON payload"):
        SentinelOneClient.load_json_payload("{not json}")


# ── SentinelOneError ───────────────────────────────────────────────────────────

from s1am.api.exceptions import SentinelOneError


def test_error_no_payload():
    e = SentinelOneError("something went wrong")
    assert str(e) == "something went wrong"


def test_error_with_dict_payload():
    e = SentinelOneError("HTTP 401", status_code=401, payload={"error": "Unauthorized"})
    text = str(e)
    assert "HTTP 401" in text
    assert "Unauthorized" in text


def test_error_with_text_payload():
    e = SentinelOneError("HTTP 500", status_code=500, payload="Internal Server Error")
    assert "Internal Server Error" in str(e)


def test_error_status_code_stored():
    e = SentinelOneError("msg", status_code=403)
    assert e.status_code == 403


# ── Config — profile file loading ────────────────────────────────────────────

import os
import tempfile
from s1am.api.config import Config, _CONFIG_FILE


def test_config_env_vars(monkeypatch):
    monkeypatch.setenv("S1_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("S1_API_TOKEN", "envtoken")
    monkeypatch.setenv("S1_POWERQUERY_TOKEN", "pqtoken")
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", "/nonexistent/path")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config()
    assert c.base_url == "https://env.example.com"
    assert c.api_token == "envtoken"
    assert c.powerquery_token == "pqtoken"


def test_config_defaults(monkeypatch):
    for k in ["S1_BASE_URL", "S1_API_TOKEN", "S1_POWERQUERY_TOKEN",
               "S1_AUTH_PREFIX", "S1_API_PATH", "S1_TIMEOUT", "S1_VERIFY_SSL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", "/nonexistent/path")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config()
    assert c.api_path == "/web/api/v2.1"
    assert c.timeout == 30
    assert c.verify_ssl is True


def test_config_file_profile(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(
        "[myprofile]\n"
        "s1_base_url = https://file.example.com\n"
        "s1_api_token = filetoken\n"
    )
    for k in ["S1_BASE_URL", "S1_API_TOKEN"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", str(cfg))
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config(profile="myprofile")
    assert c.base_url == "https://file.example.com"
    assert c.api_token == "filetoken"


def test_config_env_overrides_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("[default]\ns1_api_token = filetoken\ns1_base_url = https://file.example.com\n")
    monkeypatch.setenv("S1_API_TOKEN", "envtoken")
    monkeypatch.delenv("S1_BASE_URL", raising=False)
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", str(cfg))
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config()
    assert c.api_token == "envtoken"   # env wins
    assert c.base_url == "https://file.example.com"  # file used when no env


def test_config_missing_profile_raises(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("[default]\ns1_base_url = https://x.com\n")
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", str(cfg))
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    with pytest.raises(ValueError, match="Profile 'ghost' not found"):
        Config(profile="ghost")


def test_config_validate_missing_mgmt(monkeypatch):
    monkeypatch.delenv("S1_BASE_URL", raising=False)
    monkeypatch.delenv("S1_API_TOKEN", raising=False)
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", "/nonexistent")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config()
    with pytest.raises(ValueError, match="S1_BASE_URL"):
        c.validate(require_mgmt=True)


def test_config_as_display_dict(monkeypatch):
    monkeypatch.setenv("S1_BASE_URL", "https://x.com")
    monkeypatch.setenv("S1_API_TOKEN", "supersecrettoken")
    monkeypatch.delenv("S1_POWERQUERY_TOKEN", raising=False)
    monkeypatch.setattr("s1am.api.config._CONFIG_FILE", "/nonexistent")
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    c = Config()
    d = c.as_display_dict()
    assert d["S1_BASE_URL"] == "https://x.com"
    assert "supersecrettoken" not in d["S1_API_TOKEN"]
    assert "*" in d["S1_API_TOKEN"]
    assert d["S1_POWERQUERY_TOKEN"] == "(not set)"
