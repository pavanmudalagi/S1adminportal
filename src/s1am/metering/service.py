"""Usage metering — SDL PowerQuery ``metering`` datasource, aggregated per console → account → site.

Verified on the Staging console (2026-11-13):

* ``POST https://<console>/sdl/api/powerQuery`` with ``Authorization: Bearer <token>``. The MGMT API token
  works as the bearer; a separate PowerQuery token is used when the profile has one.
* Always send ``endTime``. With only ``startTime`` the API returned a two-day window regardless of range.
  ``startTime``/``endTime`` accept ``YYYY-MM-DD``, ISO timestamps, epoch ms and relative forms (``30d``).
* Inside the JSON body both ``"`` and ``'`` string literals work; we always emit double quotes with
  backslash escaping and refuse control characters.
* ``| filter date >= "…" and date <= "…"`` works; ``group value=sum(value) by …`` and ``contains`` work.
* ``xdr_ingested_bytes`` and ``cns_workloads`` rows carry scope **ids** but not scope names, so names are
  joined from ``| datasource "metering" from "tenants"`` (tenant_scope_id → tenant_scope_name) and from
  the endpoint reports.
* ``state`` is ``archived`` (final) or ``live`` (still changing). Data lags about two days.
* Prompt (``ai_security_*``) reports ride the same datasource (2026-11-20). Usage is reported against the S1
  Account or Site that owns the Prompt tenant (``account_scope_id`` / ``site_scope_id``; the tenant id is not in the
  data). Seat reports (employees, developers) are daily active counts → aggregated with ``max`` per day and ``peak``
  per month; usage counts (homegrown app calls, agentic events, red-team attacks) are summed. Day X lands on day
  X+1 about 15 minutes after 12:00 UTC, so the daily sync defaults to 14:00 local.

Everything is read-only.
"""
import calendar
import csv
import io
import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from ..api.config import Config

PRODUCTS_FILE = Path(__file__).with_name("products.json")
DEFAULT_DB = Path.home() / ".config" / "s1am" / "usage.db"
REPORTS_TTL = 24 * 3600
CALL_DELAY = 0.05            # ≤ 20 req/s per console; the global limit is 50 req/s burst 100
MAX_RETRIES = 4
MAX_WORKERS = 6
TIMEOUT = 90
ROLLUPS = ("peak", "average", "month_end", "sum")
DAILY_AGGS = ("sum", "max")      # how several rows of one scope on one day combine
DEFAULT_SYNC_HOUR = 14           # local; S1 publishes day X about 12:15 UTC on day X+1 (Prompt meters), loaders ≥ 13:00 UTC
ROW_LIMIT = 100000           # the API silently caps at 1000 rows unless the query sets a limit


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── product config ─────────────────────────────────────────────────────────

def load_products(path=None):
    cfg = json.loads(Path(path or PRODUCTS_FILE).read_text())
    products = [p for p in cfg.get("products", []) if p.get("report")]
    for p in products:
        p.setdefault("key", p["report"]); p.setdefault("label", p["report"]); p.setdefault("short", p["label"])
        p.setdefault("unit", ""); p.setdefault("rollup", "peak"); p.setdefault("alt_rollups", []); p.setdefault("breakdown", None)
        p.setdefault("daily", "sum"); p.setdefault("group", None)
        if p["rollup"] not in ROLLUPS:
            raise ValueError(f"{p['key']}: unknown rollup {p['rollup']!r}")
        if p["daily"] not in DAILY_AGGS:
            raise ValueError(f"{p['key']}: unknown daily aggregation {p['daily']!r}")
    return {"bytes_base": int(cfg.get("bytes_base", 1000)), "products": products}


def product_by_key(products, key):
    for p in products:
        if p["key"] == key or p["report"] == key:
            return p
    return None


# ── query builder ──────────────────────────────────────────────────────────

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class QueryError(ValueError):
    pass


def q_literal(value):
    """A PowerQuery string literal: double-quoted, backslash-escaped, no control characters."""
    s = str(value)
    if any(ord(c) < 32 for c in s):
        raise QueryError("control characters are not allowed in a query literal")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def q_ident(name):
    if not _IDENT.match(name or ""):
        raise QueryError(f"invalid identifier {name!r}")
    return name


def q_date(value):
    if not _DATE.match(str(value)):
        raise QueryError(f"invalid date {value!r} (expected YYYY-MM-DD)")
    return str(value)


def build_query(report, start=None, end=None, group_by=(), filters=None, agg="value=sum(value)", columns=None, limit=None):
    """Compose ``| datasource "metering" from "<report>" | filter … | group … by …``."""
    parts = [f'| datasource "metering" from {q_literal(q_ident(report))}']
    conds = []
    if start:
        conds.append(f"date >= {q_literal(q_date(start))}")
    if end:
        conds.append(f"date <= {q_literal(q_date(end))}")
    for col, val in (filters or {}).items():
        conds.append(f"{q_ident(col)}={q_literal(val)}")
    if conds:
        parts.append("| filter " + " and ".join(conds))
    if columns:
        parts.append("| columns " + ", ".join(q_ident(c) for c in columns))
    if group_by:
        parts.append(f"| group {agg} by " + ", ".join(q_ident(c) for c in group_by))
    if limit:
        parts.append(f"| limit {int(limit)}")
    return " ".join(parts)


def usage_query(report, start, end, level="site", account_id=None, breakdown=None, daily="sum"):
    """Account- or site-level daily values, always split by ``state`` so provisional days are visible. ``daily`` is how
    several rows of one scope on one day combine: ``sum`` (usage counts, bytes) or ``max`` (daily seat snapshots)."""
    if daily not in DAILY_AGGS:
        raise QueryError(f"unknown daily aggregation {daily!r}")
    # Identity reports leave account_scope_id empty and put the scope in tenant_scope_id/level, so those ride along.
    dims = ["date", "account_scope_id", "tenant_scope_id", "tenant_scope_level", "organization_level_1_scope_id", "organization_level_1_scope_level"]
    if level == "site":
        dims += ["site_scope_id"]
    elif level == "org1":
        dims = ["date", "organization_level_1_scope_id", "organization_level_1_name", "organization_level_1_salesforce_id"]
    elif level == "org2":
        dims = ["date", "organization_level_2_scope_id", "organization_level_2_name", "organization_level_2_salesforce_id"]
    elif level != "account":
        raise QueryError(f"unknown level {level!r}")
    if breakdown:
        dims.append(q_ident(breakdown))
    dims.append("state")
    filters = {"account_scope_id": account_id} if account_id else None
    return build_query(report, start, end, group_by=dims, filters=filters, agg=f"value={daily}(value)", limit=ROW_LIMIT)


REPORTS_QUERY = build_query("reports", columns=["report_name", "product_category", "product_name", "unit", "description"])
TENANTS_QUERY = build_query("tenants", limit=ROW_LIMIT)


# ── response parser ────────────────────────────────────────────────────────

def parse_response(payload):
    """``{"columns": [{"name": …}], "values": [[…]]}`` → list of dicts. Also fixes the documented misspelling."""
    cols = [(c.get("name") if isinstance(c, dict) else str(c)) for c in (payload or {}).get("columns") or []]
    cols = ["tenant_scope_level" if c == "tentant_scope_level" else c for c in cols]
    out = []
    for row in (payload or {}).get("values") or []:
        out.append({cols[i]: v for i, v in enumerate(row) if i < len(cols)})
    return out


# ── PowerQuery client ──────────────────────────────────────────────────────

class MeteringError(Exception):
    def __init__(self, message, kind="error", status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


class PowerQueryClient:
    """One console. Sequential, throttled, backs off on 429. The token never appears in messages."""

    def __init__(self, base_url, token, verify=True, timeout=TIMEOUT, sleep=time.sleep, session=None):
        self.url = f"{base_url.rstrip('/')}/sdl/api/powerQuery"
        self._token = token
        self.verify, self.timeout, self._sleep = verify, timeout, sleep
        self._http = session or requests.Session()
        self._last = 0.0

    def query(self, query, start, end):
        body = {"query": query, "startTime": start, "endTime": end}
        attempt = 0
        while True:
            wait = CALL_DELAY - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            try:
                resp = self._http.post(self.url, json=body, headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                                       timeout=self.timeout, verify=self.verify)
            except requests.Timeout as exc:
                raise MeteringError("Timed out talking to the console", "timeout") from exc
            except requests.RequestException as exc:
                raise MeteringError(str(exc).replace(self._token, "***")[:300], "network") from exc
            finally:
                self._last = time.monotonic()
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                attempt += 1
                retry = resp.headers.get("Retry-After")
                self._sleep(float(retry) if retry and retry.isdigit() else min(2.0 * attempt, 8.0))
                continue
            if resp.status_code in (401, 403):
                raise MeteringError("Token lacks Metering Reports – View (or is invalid)", "permission", resp.status_code)
            if resp.status_code == 404:
                raise MeteringError("Metering is not enabled on this console", "unsupported", 404)
            if resp.status_code >= 400:
                text = resp.text[:200].replace(self._token, "***")
                raise MeteringError(f"HTTP {resp.status_code}: {text}", "error", resp.status_code)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise MeteringError("Non-JSON response from PowerQuery", "error") from exc
            if isinstance(payload, dict) and payload.get("status") and str(payload["status"]).lower() not in ("ok", "success", "complete", "completed"):
                raise MeteringError(f"PowerQuery status {payload['status']}: {str(payload.get('message') or payload.get('warnings') or '')[:200]}", "error")
            return parse_response(payload)


def client_for_profile(code, sleep=time.sleep):
    cfg = Config(profile=code)
    token = cfg.powerquery_token or cfg.api_token
    if not cfg.base_url or not token:
        raise MeteringError(f"Console '{code}' has no base URL or token configured", "config")
    return PowerQueryClient(cfg.base_url, token, verify=getattr(cfg, "verify_ssl", True), sleep=sleep)


# ── store ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
  console TEXT NOT NULL, report TEXT NOT NULL, date TEXT NOT NULL,
  account_id TEXT NOT NULL, site_id TEXT NOT NULL DEFAULT '', breakdown TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL, state TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (console, report, date, account_id, site_id, breakdown)
);
CREATE INDEX IF NOT EXISTS daily_lookup ON daily (console, report, date);
CREATE TABLE IF NOT EXISTS names (console TEXT NOT NULL, scope_id TEXT NOT NULL, name TEXT, level TEXT, salesforce_id TEXT, updated_at TEXT,
  PRIMARY KEY (console, scope_id));
CREATE TABLE IF NOT EXISTS reports_cache (console TEXT PRIMARY KEY, reports TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS console_status (console TEXT PRIMARY KEY, ok INTEGER, kind TEXT, message TEXT, reports TEXT, last_sync TEXT, last_ok TEXT);
CREATE TABLE IF NOT EXISTS sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, console TEXT, report TEXT, month TEXT, days INTEGER, rows INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)  # wait for a concurrent writer instead of failing
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)

    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self._lock:
            self.conn.execute(sql, args); self.conn.commit()

    # reports cache (24h)
    def cached_reports(self, console, ttl=REPORTS_TTL):
        r = self._q("SELECT reports, fetched_at FROM reports_cache WHERE console=?", (console,))
        if r and time.time() - r[0]["fetched_at"] < ttl:
            return json.loads(r[0]["reports"])
        return None

    def save_reports(self, console, reports):
        self._x("INSERT OR REPLACE INTO reports_cache VALUES (?,?,?)", (console, json.dumps(reports), time.time()))

    # names
    def save_names(self, console, rows):
        with self._lock:
            self.conn.executemany(
                "INSERT INTO names VALUES (?,?,?,?,?,?) ON CONFLICT(console, scope_id) DO UPDATE SET "
                "name=COALESCE(excluded.name, name), level=COALESCE(excluded.level, level), "
                "salesforce_id=COALESCE(excluded.salesforce_id, salesforce_id), updated_at=excluded.updated_at",
                [(console, r["scope_id"], r.get("name"), r.get("level"), r.get("salesforce_id"), _now()) for r in rows if r.get("scope_id")])
            self.conn.commit()

    def names(self, console=None):
        rows = self._q("SELECT * FROM names" + (" WHERE console=?" if console else ""), (console,) if console else ())
        return {(r["console"], r["scope_id"]): r for r in rows}

    # daily rows
    def day_states(self, console, report, start, end):
        """{date: state} for days we already hold (a day is 'live' if any row is live)."""
        out = {}
        for r in self._q("SELECT date, MIN(CASE WHEN state='archived' THEN 1 ELSE 0 END) AS final FROM daily WHERE console=? AND report=? AND date>=? AND date<=? GROUP BY date",
                         (console, report, start, end)):
            out[r["date"]] = "archived" if r["final"] else "live"
        return out

    def replace_days(self, console, report, days, rows):
        """Replace everything we hold for the given days with the fresh rows."""
        if not days:
            return 0
        with self._lock:
            self.conn.executemany("DELETE FROM daily WHERE console=? AND report=? AND date=?", [(console, report, d) for d in days])
            self.conn.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?)",
                                  [(console, report, r["date"], str(r.get("account_id") or ""), str(r.get("site_id") or ""), str(r.get("breakdown") or ""),
                                    float(r.get("value") or 0), r.get("state") or "live", _now()) for r in rows])
            self.conn.commit()
        return len(rows)

    def daily(self, start, end, consoles=None, reports=None, account_id=None, site_id=None):
        sql, args = "SELECT * FROM daily WHERE date>=? AND date<=?", [start, end]
        if consoles:
            sql += f" AND console IN ({','.join('?' * len(consoles))})"; args += list(consoles)
        if reports:
            sql += f" AND report IN ({','.join('?' * len(reports))})"; args += list(reports)
        if account_id:
            sql += " AND account_id=?"; args.append(str(account_id))
        if site_id:
            sql += " AND site_id=?"; args.append(str(site_id))
        return self._q(sql, args)

    # status / log / meta
    def set_status(self, console, ok, kind=None, message=None, reports=None):
        prev = self._q("SELECT last_ok FROM console_status WHERE console=?", (console,))
        last_ok = _now() if ok else (prev[0]["last_ok"] if prev else None)
        self._x("INSERT OR REPLACE INTO console_status VALUES (?,?,?,?,?,?,?)",
                (console, int(bool(ok)), kind, message, json.dumps(reports or []), _now(), last_ok))

    def statuses(self):
        return {r["console"]: dict(r, reports=json.loads(r["reports"] or "[]")) for r in self._q("SELECT * FROM console_status")}

    def log(self, console, report, month, days, rows, message=""):
        self._x("INSERT INTO sync_log (at, console, report, month, days, rows, message) VALUES (?,?,?,?,?,?,?)", (_now(), console, report, month, days, rows, message))

    def set_meta(self, k, v):
        self._x("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))

    def get_meta(self, k):
        r = self._q("SELECT value FROM meta WHERE key=?", (k,))
        return r[0]["value"] if r else None


# ── per-console fetches ────────────────────────────────────────────────────

def list_reports(client, store=None, console=None):
    if store and console:
        cached = store.cached_reports(console)
        if cached is not None:
            return cached
    rows = client.query(REPORTS_QUERY, *_recent_window())
    reports = [{"report": r.get("report_name"), "category": r.get("product_category"), "product": r.get("product_name"),
                "unit": r.get("unit"), "description": r.get("description")} for r in rows if r.get("report_name")]
    if store and console:
        store.save_reports(console, reports)
    return reports


def list_tenants(client):
    rows = client.query(TENANTS_QUERY, *_recent_window())
    return [{"scope_id": str(r.get("tenant_scope_id") or ""), "name": r.get("tenant_scope_name"), "level": r.get("tenant_scope_level")} for r in rows if r.get("tenant_scope_id")]


NAME_REPORTS = ("server_endpoints", "workstation_endpoints", "singularity_identity_endpoints", "container_hosts")
TENANT_NAME_REPORTS = ("singularity_identity_users", "singularity_identity_endpoints")


def collect_names(client, available, start, end):
    """Account/site names and Salesforce IDs from the first endpoint report that has them (the byte and CNS
    reports only carry scope ids). Complements the ``tenants`` table, which has names but no Salesforce IDs."""
    for rep in NAME_REPORTS:
        if rep not in available:
            continue
        rows = client.query(build_query(rep, start, end, group_by=["account_scope_id", "account_scope_name", "site_scope_id", "site_scope_name",
                                                                 "organization_level_1_salesforce_id"], agg="n=count()", limit=ROW_LIMIT), start, _plus_day(end))
        out = {}
        for r in rows:
            if r.get("account_scope_id"):
                out[str(r["account_scope_id"])] = {"scope_id": str(r["account_scope_id"]), "name": r.get("account_scope_name"), "level": "account",
                                                   "salesforce_id": r.get("organization_level_1_salesforce_id") or None}
            if r.get("site_scope_id"):
                out[str(r["site_scope_id"])] = {"scope_id": str(r["site_scope_id"]), "name": r.get("site_scope_name"), "level": "site", "salesforce_id": None}
        return list(out.values())
    return []


def collect_tenant_names(client, available, start, end):
    """Identity reports scope rows by tenant_scope_id; their names live on the same rows."""
    for rep in TENANT_NAME_REPORTS:
        if rep not in available:
            continue
        rows = client.query(build_query(rep, start, end, group_by=["tenant_scope_id", "tenant_scope_name", "tenant_scope_level"], agg="n=count()", limit=ROW_LIMIT), start, _plus_day(end))
        return [{"scope_id": str(r["tenant_scope_id"]), "name": r.get("tenant_scope_name"), "level": r.get("tenant_scope_level"), "salesforce_id": None}
                for r in rows if r.get("tenant_scope_id")]
    return []


def get_usage(client, report, start, end, level="site", account_id=None, breakdown=None, daily="sum"):
    """Daily values for one report. Returns normalised rows: date, account_id, site_id, breakdown, value, state (+ org names)."""
    rows = client.query(usage_query(report, start, end, level, account_id, breakdown, daily), start, _plus_day(end))
    out = []
    for r in rows:
        if level in ("org1", "org2"):
            n = level[-1]
            out.append({"date": r.get("date"), "account_id": str(r.get(f"organization_level_{n}_scope_id") or ""), "site_id": "",
                        "name": r.get(f"organization_level_{n}_name"), "salesforce_id": r.get(f"organization_level_{n}_salesforce_id"),
                        "breakdown": str(r.get(breakdown) or "") if breakdown else "", "value": float(r.get("value") or 0), "state": r.get("state") or "live"})
        else:
            acc, site = _scope_ids(r, level)
            out.append({"date": r.get("date"), "account_id": acc, "site_id": site,
                        "breakdown": str(r.get(breakdown) or "") if breakdown else "", "value": float(r.get("value") or 0), "state": r.get("state") or "live"})
    return out


def _scope_ids(r, level):
    """(account_id, site_id) with fallbacks: tenant_scope_* when the account/site columns are empty (identity
    reports), then organization level 1 when it is an account."""
    acc = str(r.get("account_scope_id") or "")
    site = str(r.get("site_scope_id") or "") if level == "site" else ""
    t_level = (r.get("tenant_scope_level") or "").lower()
    t_id = str(r.get("tenant_scope_id") or "")
    if not acc and t_level == "account" and t_id:
        acc = t_id
    if not site and level == "site" and t_level == "site" and t_id:
        site = t_id
    if not acc and (r.get("organization_level_1_scope_level") or "").lower() == "account":
        acc = str(r.get("organization_level_1_scope_id") or "")
    return acc, site


def _plus_day(d):
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _recent_window(days=7):
    """Absolute dates for the discovery queries — the API rejects ``endTime: "now"``."""
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), (today + timedelta(days=1)).isoformat()


# ── month helpers ──────────────────────────────────────────────────────────

def month_bounds(month):
    y, m = (int(x) for x in month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def month_days(month):
    start, end = month_bounds(month)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


# ── rollups ────────────────────────────────────────────────────────────────

def rollup_monthly(rows, rule):
    """rows: daily dicts with date, value, state (one scope). Returns {value, rule, days, live_days, state, first, last}."""
    if rule not in ROLLUPS:
        raise ValueError(f"unknown rollup {rule!r}")
    by_day = {}
    live = set()
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + float(r.get("value") or 0)
        if (r.get("state") or "live") != "archived":
            live.add(r["date"])
    if not by_day:
        return {"value": None, "rule": rule, "days": 0, "live_days": 0, "state": "empty", "first": None, "last": None}
    days = sorted(by_day)
    vals = [by_day[d] for d in days]
    if rule == "peak":
        value = max(vals)
    elif rule == "average":
        value = sum(vals) / len(vals)
    elif rule == "month_end":
        value = by_day[days[-1]]
    else:
        value = sum(vals)
    return {"value": value, "rule": rule, "days": len(days), "live_days": len(live), "state": "provisional" if live else "final",
            "first": days[0], "last": days[-1]}


def format_bytes(n, base=1000):
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v, i = float(n), 0
    while v >= base and i < len(units) - 1:
        v /= base; i += 1
    return f"{v:,.2f} {units[i]}"


# ── sync ───────────────────────────────────────────────────────────────────

def sync_console(client, store, console, month, products, today=None, force=False, progress=None):
    """Fetch the month's site-level rows for every product the console reports. Archived days are kept; live or
    missing days are re-fetched. Returns a status dict; never raises."""
    today = today or date.today().isoformat()
    start, end = month_bounds(month)
    end = min(end, today)
    status = {"console": console, "ok": False, "kind": None, "message": None, "reports": [], "fetched": {}, "skipped": []}
    try:
        reports = list_reports(client, store, console)
        available = {r["report"] for r in reports}
        status["reports"] = sorted(available)
        try:
            store.save_names(console, list_tenants(client))
            if end >= start:
                store.save_names(console, collect_names(client, available, start, end))
                store.save_names(console, collect_tenant_names(client, available, start, end))
        except MeteringError as exc:
            if exc.kind in ("permission", "unsupported"):
                raise
        for p in products:
            rep = p["report"]
            if rep not in available:
                status["skipped"].append(rep)
                continue
            if end < start:
                continue
            held = {} if force else store.day_states(console, rep, start, end)
            days = month_days(month)
            days = [d for d in days if d <= end and held.get(d) != "archived"]
            if not days:
                status["fetched"][rep] = 0
                continue
            rows = get_usage(client, rep, days[0], days[-1], level="site", breakdown=p.get("breakdown"), daily=p.get("daily", "sum"))
            # Keep archived days we already hold if they fall inside the window but were not returned (gap days).
            n = store.replace_days(console, rep, days, rows)
            # Site rows carry names only through the tenants table; the endpoint reports also give account names.
            status["fetched"][rep] = n
            store.log(console, rep, month, len(days), n)
            if progress:
                progress({"console": console, "report": rep, "rows": n})
        status["ok"] = True
        store.set_status(console, True, reports=status["reports"])
    except MeteringError as exc:
        status["kind"], status["message"] = exc.kind, str(exc)
        store.set_status(console, False, exc.kind, str(exc), status["reports"])
        store.log(console, "", month, 0, 0, f"{exc.kind}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        status["kind"], status["message"] = "error", str(exc)[:300]
        store.set_status(console, False, "error", str(exc)[:300], status["reports"])
    return status


def sync_all(store, consoles, month, products, client_for=None, workers=MAX_WORKERS, force=False, progress=None):
    client_for = client_for or client_for_profile   # resolved at call time so tests can swap the factory

    def one(code):
        try:
            client = client_for(code)
        except MeteringError as exc:
            store.set_status(code, False, exc.kind, str(exc))
            return {"console": code, "ok": False, "kind": exc.kind, "message": str(exc), "reports": [], "fetched": {}, "skipped": []}
        return sync_console(client, store, code, month, products, force=force, progress=progress)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(consoles) or 1))) as pool:
        results = list(pool.map(one, consoles))
    store.set_meta("last_sync", _now())
    store.set_meta("last_sync_month", month)
    return {"at": _now(), "month": month, "consoles": results}


# ── views ──────────────────────────────────────────────────────────────────

def _name(names, console, scope_id, fallback=None):
    if not scope_id:
        return fallback or "(console-wide — no account scope)"
    r = names.get((console, str(scope_id)))
    return (r["name"] if r and r.get("name") else None) or fallback or str(scope_id)


def _sf(names, console, scope_id):
    r = names.get((console, str(scope_id)))
    return r.get("salesforce_id") if r else None


def summary(store, month, products, consoles=None, rules=None, bytes_base=1000, hide_zero=True):
    """One row per (console, account) with the billable value per product under the active rollup rule.
    ``hide_zero`` drops accounts whose every product rolls up to 0 (hundreds of empty test tenants on Staging)."""
    start, end = month_bounds(month)
    rules = rules or {}
    prods = {p["report"]: p for p in products}
    rows = store.daily(start, end, consoles=consoles, reports=list(prods))
    names = store.names()
    acc = {}
    for r in rows:
        key = (r["console"], r["account_id"])
        a = acc.setdefault(key, {"console": r["console"], "account_id": r["account_id"], "account": _name(names, r["console"], r["account_id"]),
                                 "salesforce_id": _sf(names, r["console"], r["account_id"]), "products": {}, "state": "final", "sites": set()})
        a["products"].setdefault(r["report"], []).append(r)
        if r["site_id"]:
            a["sites"].add(r["site_id"])
    out = []
    for a in acc.values():
        cells = {}
        for rep, rs in a["products"].items():
            p = prods[rep]
            rule = rules.get(p["key"]) or p["rollup"]
            roll = rollup_monthly(rs, rule)
            roll["display"] = format_bytes(roll["value"], bytes_base) if p["unit"] == "bytes" else (f"{roll['value']:,.2f}" if rule == "average" else f"{roll['value']:,.0f}") if roll["value"] is not None else ""
            cells[p["key"]] = roll
            if roll["state"] == "provisional":
                a["state"] = "provisional"
        a["products"] = cells
        a["sites"] = len(a["sites"])
        if hide_zero and not any((c["value"] or 0) > 0 for c in cells.values()):
            continue
        out.append(a)
    out.sort(key=lambda a: (a["console"], (a["account"] or "").lower()))
    totals = {}
    for p in products:
        vals = [a["products"][p["key"]]["value"] for a in out if p["key"] in a["products"] and a["products"][p["key"]]["value"] is not None]
        tot = sum(vals) if vals else None
        totals[p["key"]] = {"value": tot, "display": format_bytes(tot, bytes_base) if p["unit"] == "bytes" else (f"{tot:,.0f}" if tot is not None else "")}
    return {"month": month, "accounts": out, "totals": totals, "rules": {p["key"]: rules.get(p["key"]) or p["rollup"] for p in products},
            "hidden_zero": len(acc) - len(out) if hide_zero else 0}


def _series(rows):
    by = {}
    for r in rows:
        d = by.setdefault(r["date"], {"date": r["date"], "value": 0.0, "state": "archived"})
        d["value"] += float(r["value"] or 0)
        if r["state"] != "archived":
            d["state"] = "live"
    return [by[d] for d in sorted(by)]


def _breakdown(rows):
    by = {}
    for r in rows:
        k = r["breakdown"] or "(none)"
        by[k] = by.get(k, 0.0) + float(r["value"] or 0)
    return [{"key": k, "value": v} for k, v in sorted(by.items(), key=lambda kv: -kv[1])]


def account_detail(store, month, products, console, account_id, rules=None, bytes_base=1000):
    start, end = month_bounds(month)
    rules = rules or {}
    rows = store.daily(start, end, consoles=[console], reports=[p["report"] for p in products], account_id=account_id)
    names = store.names(console)
    out = {"console": console, "account_id": account_id, "account": _name(names, console, account_id), "salesforce_id": _sf(names, console, account_id),
           "month": month, "products": {}, "sites": []}
    sites = {}
    for p in products:
        rs = [r for r in rows if r["report"] == p["report"]]
        if not rs:
            continue
        rule = rules.get(p["key"]) or p["rollup"]
        roll = rollup_monthly(rs, rule)
        out["products"][p["key"]] = {**roll, "series": _series(rs), "breakdown": _breakdown(rs) if p.get("breakdown") else [],
                                     "display": format_bytes(roll["value"], bytes_base) if p["unit"] == "bytes" else f"{roll['value']:,.2f}" if rule == "average" else f"{roll['value']:,.0f}"}
        for r in rs:
            s = sites.setdefault(r["site_id"], {"site_id": r["site_id"], "site": _name(names, console, r["site_id"], "(account level)" if not r["site_id"] else None), "products": {}})
            s["products"].setdefault(p["key"], []).append(r)
    for s in sites.values():
        for key, rs in list(s["products"].items()):
            p = product_by_key(products, key)
            roll = rollup_monthly(rs, rules.get(key) or p["rollup"])
            roll["display"] = format_bytes(roll["value"], bytes_base) if p["unit"] == "bytes" else f"{roll['value']:,.2f}" if roll["rule"] == "average" else f"{roll['value']:,.0f}"
            s["products"][key] = roll
        out["sites"].append(s)
    out["sites"].sort(key=lambda s: (s["site"] or "").lower())
    return out


def site_detail(store, month, products, console, site_id, rules=None, bytes_base=1000):
    start, end = month_bounds(month)
    rules = rules or {}
    rows = store.daily(start, end, consoles=[console], reports=[p["report"] for p in products], site_id=site_id)
    names = store.names(console)
    out = {"console": console, "site_id": site_id, "site": _name(names, console, site_id), "month": month, "products": {},
           "account_id": rows[0]["account_id"] if rows else None}
    if rows:
        out["account"] = _name(names, console, rows[0]["account_id"])
    for p in products:
        rs = [r for r in rows if r["report"] == p["report"]]
        if not rs:
            continue
        rule = rules.get(p["key"]) or p["rollup"]
        roll = rollup_monthly(rs, rule)
        out["products"][p["key"]] = {**roll, "series": _series(rs), "breakdown": _breakdown(rs) if p.get("breakdown") else [],
                                     "display": format_bytes(roll["value"], bytes_base) if p["unit"] == "bytes" else f"{roll['value']:,.2f}" if rule == "average" else f"{roll['value']:,.0f}"}
    return out


def health(store, consoles, products):
    """Per-console strip: data / permission / not enabled / never synced, plus which products exist there."""
    st = store.statuses()
    out = []
    wanted = {p["report"] for p in products}
    for c in consoles:
        s = st.get(c)
        if not s:
            out.append({"console": c, "state": "never", "message": "Not synced yet", "reports": [], "last_sync": None}); continue
        if s["ok"]:
            have = sorted(wanted & set(s["reports"]))
            out.append({"console": c, "state": "ok" if have else "no_reports", "message": f"{len(have)} of {len(wanted)} products available" if have else "Metering has no reports yet",
                        "reports": have, "missing": sorted(wanted - set(s["reports"])), "last_sync": s["last_sync"]})
        else:
            state = {"permission": "permission", "unsupported": "not_enabled", "config": "config"}.get(s["kind"], "error")
            out.append({"console": c, "state": state, "message": s["message"], "reports": [], "last_sync": s["last_sync"], "last_ok": s["last_ok"]})
    return out


# ── export ─────────────────────────────────────────────────────────────────

def export_rows(store, month, products, level="account", consoles=None, rules=None, bytes_base=1000, hide_zero=True):
    """Flat rows for CSV/XLSX: one per scope with a column per product plus rule/state columns."""
    s = summary(store, month, products, consoles, rules, bytes_base, hide_zero)
    out = []
    if level == "account":
        for a in s["accounts"]:
            row = {"console": a["console"], "account_id": a["account_id"], "account": a["account"], "salesforce_id": a["salesforce_id"] or "", "state": a["state"]}
            for p in products:
                c = a["products"].get(p["key"])
                row[p["key"]] = c["value"] if c else None
                row[p["key"] + "_display"] = c["display"] if c else ""
                row[p["key"] + "_state"] = c["state"] if c else ""
            out.append(row)
    else:
        start, end = month_bounds(month)
        names = store.names()
        rows = store.daily(start, end, consoles=consoles, reports=[p["report"] for p in products])
        by = {}
        for r in rows:
            k = (r["console"], r["account_id"], r["site_id"])
            by.setdefault(k, {}).setdefault(r["report"], []).append(r)
        for (console, acc, site), reps in sorted(by.items()):
            row = {"console": console, "account_id": acc, "account": _name(names, console, acc), "site_id": site,
                   "site": _name(names, console, site, "(account level)") if site else "(account level)", "state": "final"}
            for p in products:
                rs = reps.get(p["report"])
                if rs:
                    roll = rollup_monthly(rs, (rules or {}).get(p["key"]) or p["rollup"])
                    row[p["key"]] = roll["value"]; row[p["key"] + "_display"] = format_bytes(roll["value"], bytes_base) if p["unit"] == "bytes" else roll["value"]
                    row[p["key"] + "_state"] = roll["state"]
                    if roll["state"] == "provisional":
                        row["state"] = "provisional"
                else:
                    row[p["key"]] = None; row[p["key"] + "_display"] = ""; row[p["key"] + "_state"] = ""
            if hide_zero and not any((row.get(p["key"]) or 0) > 0 for p in products):
                continue
            out.append(row)
    return out, s["rules"]


def to_csv(rows, products, level, month, rules):
    buf = io.StringIO()
    buf.write(f"# SentinelOne usage {month} — {level} level — rules: " + "; ".join(f"{p['label']}={rules[p['key']]}" for p in products)
              + " — state: final = all days archived; provisional = live days present\r\n")
    w = csv.writer(buf)
    head = ["Console", "Account ID", "Account", "Salesforce ID" if level == "account" else "Site ID", *(["Site"] if level == "site" else []), "State"]
    for p in products:
        head += [f"{p['label']} ({p['unit']}, {rules[p['key']]})", f"{p['label']} state"]
    w.writerow(head)
    for r in rows:
        line = [r["console"], r["account_id"], r["account"], r.get("salesforce_id", "") if level == "account" else r.get("site_id", ""),
                *([r.get("site", "")] if level == "site" else []), r["state"]]
        for p in products:
            v = r.get(p["key"])
            line += ["" if v is None else (round(v, 2) if isinstance(v, float) else v), r.get(p["key"] + "_state", "")]
        w.writerow(line)
    return buf.getvalue()


def to_xlsx(rows, products, level, month, rules, bytes_base=1000):
    """One sheet per product with the rule and data state in the header, plus a Summary sheet."""
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Summary"
    ws.append([f"SentinelOne usage — {month} — {level} level"]); ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Product", "Report", "Unit", "Rollup rule", "Scopes with data", "Provisional scopes", "Total"])
    for p in products:
        vals = [r[p["key"]] for r in rows if r.get(p["key"]) is not None]
        prov = sum(1 for r in rows if r.get(p["key"] + "_state") == "provisional")
        tot = sum(vals) if vals else None
        ws.append([p["label"], p["report"], p["unit"], rules[p["key"]], len(vals), prov, format_bytes(tot, bytes_base) if p["unit"] == "bytes" else tot])
    for p in products:
        sh = wb.create_sheet(p["short"][:28] or p["key"][:28])
        sh.append([f"{p['label']} — {month} — rule: {rules[p['key']]} — unit: {p['unit']} — state: final = all days archived, provisional = live days present"])
        sh["A1"].font = Font(bold=True)
        head = ["Console", "Account ID", "Account", "Salesforce ID" if level == "account" else "Site ID", *(["Site"] if level == "site" else []), "Value", "Display", "State"]
        sh.append(head)
        for c in sh[2]:
            c.font = Font(bold=True)
        for r in rows:
            if r.get(p["key"]) is None:
                continue
            sh.append([r["console"], r["account_id"], r["account"], r.get("salesforce_id", "") if level == "account" else r.get("site_id", ""),
                       *([r.get("site", "")] if level == "site" else []), r[p["key"]], r.get(p["key"] + "_display", ""), r.get(p["key"] + "_state", "")])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


# ── scheduler ──────────────────────────────────────────────────────────────

class DailySync(threading.Thread):
    """Background daily sync of the current and previous month so month-end rollups don't rely on S1 retention."""

    def __init__(self, run, hour=DEFAULT_SYNC_HOUR, check_every=600):
        super().__init__(daemon=True, name="usage-daily-sync")
        self.run_fn, self.hour, self.check_every = run, hour, check_every
        self.last_day = None

    def run(self):
        while True:
            now = datetime.now()
            if now.hour >= self.hour and self.last_day != now.date():
                try:
                    self.run_fn()
                except Exception:   # a failed run (locked database, console down) must not kill the scheduler for good
                    logging.getLogger(__name__).exception("%s: daily sync failed; will try again tomorrow", self.name)
                finally:
                    self.last_day = now.date()
            time.sleep(self.check_every)


def months_to_sync(today=None):
    today = today or date.today()
    cur = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return [prev, cur]
