"""Platform churn from SentinelOne activity indicators (Jira LWT-2192).

What it does
  * Pulls churn-indicator activities (uninstall, move-to-another-console, …) from every console's
    activity log with GET /activities?activityTypes=…, incrementally by createdAt.
  * Keeps every event for good in ~/.config/s1am/churn.db, so trends survive the console's own
    activity retention and can be read back over months and years.
  * Classifies the target console of migration events (the "competitor" indicator) by host:
    internal (one of our consoles), another SentinelOne console, or other. Labels are editable.
  * Groups destination hosts into competitors: a vendor usually runs several SentinelOne consoles
    (usea1-ninjaone2, euce1-ninjaone, apse1-ninjaone …), so hosts are keyed by their tenant slug with
    the region prefix, numbering and role suffixes (-mssp, -mdr, …) removed. Label and kind live on
    the group; a host only carries its own when someone edits it.
  * Rolls up per month / quarter / year, per account, per indicator and per destination; the
    device view keeps only the most recent record per device, as the ticket asks.

Everything here is read-only against SentinelOne. Tokens never reach logs, URLs or exports.
"""
import csv
import fnmatch
import io
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from ..api.exceptions import SentinelOneError
from ..api.config import Config

DEFAULT_DB = Path.home() / ".config" / "s1am" / "churn.db"
INDICATORS_FILE = Path(__file__).with_name("indicators.json")
PAGE = 1000
PAGE_DELAY = 0.25          # seconds between pages, well under the 5 req/s budget
OVERLAP = timedelta(hours=6)  # re-read a little before the last seen event; rows are keyed by id
GRANULARITIES = ("month", "quarter", "year")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class ChurnError(Exception):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


# ── configuration ──────────────────────────────────────────────────────────

def load_indicators(path=None):
    cfg = json.loads(Path(path or INDICATORS_FILE).read_text())
    for ind in cfg["indicators"]:
        ind.setdefault("enabled", True)
        ind.setdefault("destination", None)
    return cfg


def enabled_indicators(cfg):
    return [i for i in cfg["indicators"] if i.get("enabled")]


def host_of(url):
    if not url:
        return ""
    u = str(url).strip()
    if "://" not in u:
        u = "https://" + u
    return (urlparse(u).hostname or "").lower()


S1_DOMAINS = (".sentinelone.net", ".s1gov.net")
_REGION_RE = re.compile(r"^[a-z]{2,6}(?:gov)?[a-z]{0,3}\d-")        # usea1-, euce1-, apse2-, usgovwe1-
_ROLE_SUFFIXES = {"mssp", "msp", "mdr", "edr", "xdr", "nfr", "sled", "exsp", "staging", "stg", "prod", "prd", "test", "dev", "poc", "lab"}
INTERNAL_VENDOR = "n-able"
NUMBERED_VENDOR = "sentinelone-numbered"


def vendor_key(host):
    """Grouping key for a destination host: the vendor's tenant slug.

    usea1-ninjaone2.sentinelone.net, euce1-ninjaone.sentinelone.net → "ninjaone"; usea1-pax8-03 → "pax8";
    usea1-cw04mdr / euce1-cw01 → "cw"; carvir-msp02 → "carvir"; euce1-109 → "109" (a numbered S1 console).
    Hosts outside SentinelOne are keyed by their registrable domain (console.rival.example.com → rival.example.com).
    """
    h = (host or "").lower().strip().strip(".")
    if not h:
        return ""
    for d in S1_DOMAINS:
        if h.endswith(d):
            slug = _REGION_RE.sub("", h[:-len(d)])
            break
    else:
        parts = h.split(".")
        return ".".join(parts[-3:] if len(parts) >= 3 and len(parts[-2]) <= 3 and parts[-1] != "com" else parts[-2:]) if len(parts) >= 2 else h
    toks = slug.split("-")
    while len(toks) > 1 and (toks[-1].isdigit() or re.sub(r"\d+$", "", toks[-1]) in _ROLE_SUFFIXES):
        toks.pop()
    m = re.match(r"^([a-z]+?)(\d+)([a-z]{0,4})$", toks[0])
    if m and (len(m.group(2)) >= 2 or len(m.group(1)) >= 5 or m.group(3)):
        toks[0] = m.group(1)          # ninjaone2 → ninjaone, swprd1 → swprd, cw04mdr → cw; pax8 stays pax8
    return "-".join(toks) or slug


def vendor_for(host, kind, cfg):
    """Group key for a host: our own consoles collapse into one group, numbered S1 consoles into another,
    configured aliases (swprd → n-able) apply, everything else is vendor_key(host)."""
    if kind == "internal":
        return INTERNAL_VENDOR
    key = vendor_key(host)
    for v in cfg.get("vendors", []):
        if key == v["key"] or key in v.get("match", []):
            return v["key"]
    if key.isdigit():
        return NUMBERED_VENDOR
    return key


def vendor_defaults(key, kind, cfg):
    """(label, kind) a new group starts with: the configured vendor, else a readable form of the key."""
    for v in cfg.get("vendors", []):
        if v["key"] == key:
            return v["label"], v.get("kind") or kind or "other"
    if key == INTERNAL_VENDOR:
        return "N-able (internal move)", "internal"
    if key == NUMBERED_VENDOR:
        return "SentinelOne direct (numbered console)", "sentinelone"
    label = key.replace("-", " ").title() if key and "." not in key else key
    return label or "Unknown destination", kind or "other"


def classify_destination(host, own_hosts, rules):
    """(kind, label) for a target console host. Our own consoles win over the pattern rules."""
    if not host:
        return "", ""
    if host in own_hosts:
        return "internal", "N-able console (internal move)"
    for r in rules:
        if fnmatch.fnmatch(host, r["pattern"].lower()):
            return r["kind"], r["label"]
    return "other", "Unknown destination"


# ── fetching ───────────────────────────────────────────────────────────────

def _classify(exc):
    if isinstance(exc, SentinelOneError):
        code = getattr(exc, "status_code", None)
        if code == 401:
            return "auth", "Token rejected (401)"
        if code == 403:
            return "permission", "Token lacks Activities – View (403)"
        if code == 404:
            return "unsupported", "Activities endpoint not found (404)"
        return "error", f"HTTP {code}: {str(exc)[:160]}"
    msg = str(exc)
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        return "timeout", "Console did not answer in time"
    return "error", msg[:200]


def fetch_activities(client, types, since, until=None, sleep=time.sleep, page=PAGE):
    """Every activity of the given types created after `since` (ISO 8601), oldest first, following the cursor."""
    params = {"activityTypes": ",".join(str(t) for t in types), "createdAt__gt": since, "limit": page,
              "sortBy": "createdAt", "sortOrder": "asc", "includeHidden": "true"}
    if until:
        params["createdAt__lte"] = until
    out, pages, retries = [], 0, 0
    while True:
        try:
            data = client.request("GET", "activities", params=params)
        except SentinelOneError as exc:
            if getattr(exc, "status_code", None) == 429 and retries < 5:
                retries += 1
                sleep(min(2 ** retries, 30))
                continue
            raise
        rows = data.get("data") or []
        out.extend(rows)
        pages += 1
        nxt = (data.get("pagination") or {}).get("nextCursor")
        if not nxt or not rows:
            break
        # The cursor must travel with the same filters and sort parameters, or the API answers
        # 400 "Cursor and sorting parameters mismatch" (seen on every console with >1000 events).
        params = {**params, "cursor": nxt}
        sleep(PAGE_DELAY)
    return out


EVENT_COLUMNS = ("console", "id", "activity_type", "indicator", "kind", "created_at", "account_id", "account_name",
                 "site_id", "site_name", "group_id", "group_name", "agent_id", "device_key", "device_uuid",
                 "computer_name", "os_type", "machine_type", "username", "destination_url", "destination_host",
                 "primary_description", "fetched_at")


def parse_event(raw, console, by_type):
    """One stored row from an activity payload. Returns None for types we don't track."""
    ind = by_type.get(raw.get("activityType"))
    if not ind:
        return None
    d = raw.get("data") or {}
    dest = d.get(ind["destination"]) if ind.get("destination") else None
    if not dest and ind["kind"] == "migration":
        dest = d.get("consoleUrl") or d.get("targetUrl") or d.get("console_url") or d.get("target_url")
    uuid = d.get("uuid") or d.get("agentUuid") or ""
    agent_id = str(raw.get("agentId") or "")
    # The agent id is on every activity type; data.uuid only on some (97 has it, 99 does not), so it comes second.
    device_key = agent_id or uuid or (d.get("computerName") or "")
    username = d.get("username") or d.get("realUser") or ""
    return {
        "console": console, "id": str(raw.get("id")), "activity_type": raw.get("activityType"),
        "indicator": ind["key"], "kind": ind["kind"], "created_at": (raw.get("createdAt") or "")[:19].replace("T", " "),
        "account_id": str(raw.get("accountId") or ""), "account_name": raw.get("accountName") or d.get("accountName") or "",
        "site_id": str(raw.get("siteId") or ""), "site_name": raw.get("siteName") or d.get("siteName") or "",
        "group_id": str(raw.get("groupId") or ""), "group_name": raw.get("groupName") or d.get("groupName") or "",
        "agent_id": agent_id, "device_key": device_key, "device_uuid": uuid,
        "computer_name": d.get("computerName") or "", "os_type": d.get("osType") or "", "machine_type": d.get("machineType") or "",
        "username": username, "destination_url": dest or "", "destination_host": host_of(dest),
        "primary_description": raw.get("primaryDescription") or "", "fetched_at": _now(),
    }


def fetch_accounts(client, account_ids, sleep=time.sleep):
    """{account_id: {name, salesforce_id, external_id, state}} for the ids seen in events (50 per call)."""
    ids = sorted({i for i in account_ids if i})
    out = {}
    for n in range(0, len(ids), 50):
        chunk = ids[n:n + 50]
        data = client.request("GET", "accounts", params={"ids": ",".join(chunk), "limit": 50, "states": "active,deleted,expired"})
        for a in data.get("data") or []:
            out[str(a.get("id"))] = {"name": a.get("name") or "", "salesforce_id": a.get("salesforceId") or "",
                                     "external_id": a.get("externalId") or "", "state": a.get("state") or ""}
        if n + 50 < len(ids):
            sleep(PAGE_DELAY)
    return out


# ── store ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  console TEXT NOT NULL, id TEXT NOT NULL, activity_type INTEGER, indicator TEXT, kind TEXT, created_at TEXT,
  account_id TEXT, account_name TEXT, site_id TEXT, site_name TEXT, group_id TEXT, group_name TEXT,
  agent_id TEXT, device_key TEXT, device_uuid TEXT, computer_name TEXT, os_type TEXT, machine_type TEXT,
  username TEXT, destination_url TEXT, destination_host TEXT, primary_description TEXT, fetched_at TEXT,
  PRIMARY KEY (console, id)
);
CREATE INDEX IF NOT EXISTS events_time ON events (created_at);
CREATE INDEX IF NOT EXISTS events_acct ON events (console, account_id, created_at);
CREATE INDEX IF NOT EXISTS events_dev ON events (console, device_key, created_at);
CREATE TABLE IF NOT EXISTS accounts (console TEXT NOT NULL, account_id TEXT NOT NULL, name TEXT, salesforce_id TEXT,
  external_id TEXT, state TEXT, updated_at TEXT, PRIMARY KEY (console, account_id));
CREATE TABLE IF NOT EXISTS sf_accounts (salesforce_id TEXT PRIMARY KEY, name TEXT, customer_id TEXT, country TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS destinations (host TEXT PRIMARY KEY, label TEXT, kind TEXT, note TEXT, first_seen TEXT, updated_at TEXT, edited INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS destination_groups (key TEXT PRIMARY KEY, label TEXT, kind TEXT, note TEXT, created_at TEXT, updated_at TEXT, edited INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS console_status (console TEXT PRIMARY KEY, ok INTEGER, kind TEXT, message TEXT, last_sync TEXT, last_ok TEXT, last_event TEXT, events INTEGER);
CREATE TABLE IF NOT EXISTS sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, console TEXT, since TEXT, fetched INTEGER, new INTEGER, message TEXT);
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
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(destinations)")}
            if "vendor" not in cols:
                self.conn.execute("ALTER TABLE destinations ADD COLUMN vendor TEXT")
                self.conn.commit()

    def _q(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self._lock:
            self.conn.execute(sql, args); self.conn.commit()

    # meta
    def get_meta(self, key):
        r = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def set_meta(self, key, value):
        self._x("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    # events
    def add_events(self, rows):
        """Insert new rows; rows already held (same console + activity id) are left alone. Returns how many were new."""
        if not rows:
            return 0
        with self._lock:
            before = self.conn.total_changes
            self.conn.executemany(
                f"INSERT OR IGNORE INTO events ({','.join(EVENT_COLUMNS)}) VALUES ({','.join('?' * len(EVENT_COLUMNS))})",
                [tuple(r.get(c) for c in EVENT_COLUMNS) for r in rows])
            self.conn.commit()
            return self.conn.total_changes - before

    def last_event_at(self, console):
        r = self._q("SELECT MAX(created_at) AS m FROM events WHERE console=?", (console,))
        return r[0]["m"] if r and r[0]["m"] else None

    def events(self, start=None, end=None, consoles=None, indicators=None, kinds=None, account=None, dest_kind=None):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if start:
            sql += " AND created_at >= ?"; args.append(start)
        if end:
            sql += " AND created_at < ?"; args.append(end)
        if consoles:
            sql += f" AND console IN ({','.join('?' * len(consoles))})"; args += list(consoles)
        if indicators:
            sql += f" AND indicator IN ({','.join('?' * len(indicators))})"; args += list(indicators)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"; args += list(kinds)
        if account:
            sql += " AND console=? AND account_id=?"; args += [account[0], account[1]]
        rows = self._q(sql + " ORDER BY created_at", args)
        if dest_kind:
            kinds = {dest_kind} if isinstance(dest_kind, str) else set(dest_kind)
            dests = self.effective_destinations()
            rows = [r for r in rows if r["destination_host"] and (dests.get(r["destination_host"]) or {}).get("kind") in kinds]
        return rows

    # accounts
    def save_accounts(self, console, accounts):
        with self._lock:
            self.conn.executemany(
                "INSERT INTO accounts VALUES (?,?,?,?,?,?,?) ON CONFLICT(console, account_id) DO UPDATE SET "
                "name=COALESCE(NULLIF(excluded.name,''), name), salesforce_id=COALESCE(NULLIF(excluded.salesforce_id,''), salesforce_id), "
                "external_id=COALESCE(NULLIF(excluded.external_id,''), external_id), state=COALESCE(NULLIF(excluded.state,''), state), updated_at=excluded.updated_at",
                [(console, aid, a.get("name"), a.get("salesforce_id"), a.get("external_id"), a.get("state"), _now()) for aid, a in accounts.items()])
            self.conn.commit()

    def accounts(self):
        return {(r["console"], r["account_id"]): r for r in self._q("SELECT * FROM accounts")}

    def save_sf_accounts(self, rows):
        with self._lock:
            self.conn.executemany("INSERT OR REPLACE INTO sf_accounts VALUES (?,?,?,?,?)",
                                  [(r["salesforce_id"], r.get("name"), r.get("customer_id"), r.get("country"), _now()) for r in rows if r.get("salesforce_id")])
            self.conn.commit()

    def sf_accounts(self):
        return {r["salesforce_id"]: r for r in self._q("SELECT * FROM sf_accounts")}

    # destinations
    def seen_destinations(self, hosts, own_hosts, rules, cfg=None):
        """Create rows for hosts we have not seen before and put them in their vendor group; edited rows are never overwritten."""
        cfg = cfg if cfg is not None else {"destinations": rules}
        known = self.destinations()
        new = []
        for h in sorted({h for h in (hosts or []) if h}):
            if h in known:
                continue
            kind, label = classify_destination(h, own_hosts, rules)
            new.append((h, label, kind, "", _now(), _now(), 0, vendor_for(h, kind, cfg)))
        if new:
            with self._lock:
                self.conn.executemany("INSERT OR IGNORE INTO destinations VALUES (?,?,?,?,?,?,?,?)", new)
                self.conn.commit()
        self.ensure_groups(cfg)

    def ensure_groups(self, cfg):
        """Give every host a vendor and every vendor a group row. Runs on sync and before rollups, so databases
        written before grouping existed are upgraded in place; a group's first label comes from a host someone
        already edited, then from the configured vendors, then from the key itself."""
        hosts = self._q("SELECT * FROM destinations")
        missing = [h for h in hosts if not h.get("vendor")]
        if missing:
            with self._lock:
                self.conn.executemany("UPDATE destinations SET vendor=? WHERE host=?",
                                      [(vendor_for(h["host"], h["kind"], cfg), h["host"]) for h in missing])
                self.conn.commit()
            for h in missing:
                h["vendor"] = vendor_for(h["host"], h["kind"], cfg)
        groups = self.groups()
        new = []
        for key in sorted({h["vendor"] for h in hosts if h.get("vendor")} - set(groups)):
            members = [h for h in hosts if h["vendor"] == key]
            edited = sorted((h for h in members if h["edited"]), key=lambda h: h["updated_at"] or "", reverse=True)
            if edited:
                label, kind = edited[0]["label"], edited[0]["kind"]
            else:
                label, kind = vendor_defaults(key, members[0]["kind"], cfg)
            new.append((key, label, kind, "", _now(), _now(), 0))
        if new:
            with self._lock:
                self.conn.executemany("INSERT OR IGNORE INTO destination_groups VALUES (?,?,?,?,?,?,?)", new)
                self.conn.commit()

    def destinations(self):
        return {r["host"]: r for r in self._q("SELECT * FROM destinations")}

    def groups(self):
        return {r["key"]: r for r in self._q("SELECT * FROM destination_groups")}

    def effective_destinations(self):
        """host → {label, kind, note, vendor, vendor_label, override}: a host edited by hand keeps its own label and
        kind, every other host shows its group's."""
        groups = self.groups()
        out = {}
        for h, d in self.destinations().items():
            g = groups.get(d.get("vendor") or "") or {}
            own = bool(d.get("edited"))
            out[h] = {**d, "label": d["label"] if own or not g else g["label"], "kind": d["kind"] if own or not g else g["kind"],
                      "vendor": d.get("vendor") or "", "vendor_label": g.get("label") or d["label"], "vendor_kind": g.get("kind") or d["kind"],
                      "override": own, "group_note": g.get("note") or ""}
        return out

    @staticmethod
    def _check_kind(kind):
        if kind and kind not in ("internal", "sentinelone", "competitor", "other"):
            raise ValueError("kind must be internal, sentinelone, competitor or other")

    def set_destination(self, host, label=None, kind=None, note=None, vendor=None, cfg=None):
        """Edit one host. label / kind make the host override its group; vendor moves it to another group
        (created on the fly when the key is new) and drops the override so it follows that group."""
        cur = self.destinations().get(host)
        if not cur:
            return None
        self._check_kind(kind)
        if vendor is not None:
            vendor = vendor.strip().lower()
            if not vendor:
                raise ValueError("vendor must not be empty")
            if vendor not in self.groups():
                glabel, gkind = vendor_defaults(vendor, self.effective_destinations()[host]["kind"], cfg or load_indicators())
                self._x("INSERT OR IGNORE INTO destination_groups VALUES (?,?,?,?,?,?,?)", (vendor, glabel, gkind, "", _now(), _now(), 0))
            self._x("UPDATE destinations SET vendor=?, updated_at=?, edited=0 WHERE host=?", (vendor, _now(), host))
            cur = self.destinations()[host]
        if label is not None or kind or note is not None:
            edited = 1 if (label is not None or kind) else cur["edited"]
            self._x("UPDATE destinations SET label=?, kind=?, note=?, updated_at=?, edited=? WHERE host=?",
                    (label if label is not None else cur["label"], kind or cur["kind"], note if note is not None else cur["note"], _now(), edited, host))
        return self.effective_destinations()[host]

    def reset_destination(self, host):
        """Drop a host's own label and kind so it follows its group again."""
        if host not in self.destinations():
            return None
        self._x("UPDATE destinations SET edited=0, updated_at=? WHERE host=?", (_now(), host))
        return self.effective_destinations()[host]

    def set_group(self, key, label=None, kind=None, note=None):
        """Rename a competitor or change its kind; every host in the group that has no override follows."""
        cur = self.groups().get(key)
        if not cur:
            return None
        self._check_kind(kind)
        self._x("UPDATE destination_groups SET label=?, kind=?, note=?, updated_at=?, edited=1 WHERE key=?",
                (label if label is not None else cur["label"], kind or cur["kind"], note if note is not None else cur["note"], _now(), key))
        return self.groups()[key]

    # status
    def set_status(self, console, ok, kind, message, events=None):
        cur = self._q("SELECT * FROM console_status WHERE console=?", (console,))
        last_ok = _now() if ok else (cur[0]["last_ok"] if cur else None)
        self._x("INSERT OR REPLACE INTO console_status VALUES (?,?,?,?,?,?,?,?)",
                (console, 1 if ok else 0, kind, message, _now(), last_ok, self.last_event_at(console), events))

    def status(self):
        return {r["console"]: r for r in self._q("SELECT * FROM console_status")}

    def log(self, console, since, fetched, new, message=""):
        self._x("INSERT INTO sync_log (at, console, since, fetched, new, message) VALUES (?,?,?,?,?,?)",
                (_now(), console, since, fetched, new, message))


# ── sync ───────────────────────────────────────────────────────────────────

def client_for_profile(code):
    from ..api.client import SentinelOneClient
    cfg = Config(profile=code)
    if not cfg.base_url or not cfg.api_token:
        raise ChurnError(f"Console '{code}' has no base URL or API token configured", "config")
    return SentinelOneClient(cfg.base_url, cfg.api_token, api_path=cfg.api_path, timeout=cfg.timeout,
                             verify=cfg.verify_ssl, auth_prefix=cfg.auth_prefix)


def own_console_hosts():
    from ..eap import service as eap_service
    return {host_of(u) for u in eap_service.profiles().values() if u}


def sync_console(store, console, client, cfg, own_hosts=(), backfill_days=365, now=None, sleep=time.sleep):
    """Pull new indicator activities for one console since the last stored event (minus an overlap)."""
    now = now or datetime.now(timezone.utc)
    types = [i["type"] for i in cfg["indicators"]]  # disabled ones are stored too, cheaply
    by_type = {i["type"]: i for i in cfg["indicators"]}
    last = store.last_event_at(console)
    if last:
        since_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) - OVERLAP
    else:
        since_dt = now - timedelta(days=backfill_days)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {"console": console, "ok": False, "kind": None, "message": "", "since": since, "fetched": 0, "new": 0}
    try:
        raw = fetch_activities(client, types, since, sleep=sleep)
        rows = [r for r in (parse_event(a, console, by_type) for a in raw) if r]
        new = store.add_events(rows)
        store.seen_destinations([r["destination_host"] for r in rows], own_hosts, cfg["destinations"], cfg)
        try:
            accounts = fetch_accounts(client, [r["account_id"] for r in rows], sleep=sleep)
            # Names from the events themselves fill in anything the accounts endpoint did not return (deleted accounts).
            for r in rows:
                accounts.setdefault(r["account_id"], {"name": r["account_name"], "salesforce_id": "", "external_id": "", "state": ""})
            store.save_accounts(console, accounts)
        except Exception as exc:  # names are a nicety; the events are the data
            result["message"] = f"accounts lookup failed: {_classify(exc)[1]}"
        result.update(ok=True, kind="ok", fetched=len(raw), new=new)
        store.set_status(console, True, "ok", result["message"], events=len(raw))
        store.log(console, since, len(raw), new, result["message"])
    except Exception as exc:
        kind, msg = _classify(exc)
        result.update(kind=kind, message=msg)
        store.set_status(console, False, kind, msg)
        store.log(console, since, 0, 0, msg)
    return result


def sync_all(store, consoles, cfg, client_for=None, own_hosts=None, backfill_days=365, workers=4, sleep=time.sleep):
    """Parallel across consoles, sequential within. One console failing never hides the others."""
    client_for = client_for or client_for_profile
    own_hosts = set(own_hosts) if own_hosts is not None else own_console_hosts()

    def one(code):
        try:
            client = client_for(code)
        except Exception as exc:
            kind, msg = (exc.kind, str(exc)) if isinstance(exc, ChurnError) else _classify(exc)
            store.set_status(code, False, kind, msg)
            return {"console": code, "ok": False, "kind": kind, "message": msg, "fetched": 0, "new": 0, "since": ""}
        return sync_console(store, code, client, cfg, own_hosts, backfill_days, sleep=sleep)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(consoles) or 1))) as ex:
        results = list(ex.map(one, consoles))
    store.set_meta("last_sync", _now())
    return {"at": _now(), "consoles": results}


def enrich_salesforce(store, lookup):
    """Resolve Salesforce account names for the IDs the consoles gave us. `lookup(ids)` returns SOQL rows."""
    ids = sorted({a["salesforce_id"] for a in store.accounts().values() if a.get("salesforce_id")})
    have = store.sf_accounts()
    missing = [i for i in ids if i not in have]
    if not missing:
        return 0
    rows = lookup(missing)
    store.save_sf_accounts([{"salesforce_id": r.get("Id"), "name": r.get("Name"), "customer_id": r.get("Customer_ID__c"), "country": r.get("BillingCountry")} for r in rows])
    return len(rows)


# ── periods & rollups ──────────────────────────────────────────────────────

def period_key(ts, granularity="month"):
    y, m = int(ts[:4]), int(ts[5:7])
    if granularity == "year":
        return f"{y}"
    if granularity == "quarter":
        return f"{y}-Q{(m - 1) // 3 + 1}"
    return f"{y}-{m:02d}"


def periods_between(start_month, end_month, granularity="month"):
    """Ordered period keys covering [start_month, end_month] (YYYY-MM)."""
    y, m = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    out = []
    while (y, m) <= (ey, em):
        k = period_key(f"{y}-{m:02d}", granularity)
        if not out or out[-1] != k:
            out.append(k)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def month_range(start_month, end_month):
    """[start, end) timestamps for SQL."""
    y, m = int(end_month[:4]), int(end_month[5:7])
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return f"{start_month}-01 00:00:00", f"{y}-{m:02d}-01 00:00:00"


def default_range(months=12, today=None):
    today = today or date.today()
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}", today.strftime("%Y-%m")


def latest_per_device(rows):
    """Most recent record per (console, device). Rows must be in created_at order."""
    latest = {}
    for r in rows:
        latest[(r["console"], r["device_key"])] = r
    return sorted(latest.values(), key=lambda r: r["created_at"], reverse=True)


def _count_devices(rows):
    return len({(r["console"], r["device_key"]) for r in rows})


def summary(store, start_month, end_month, granularity="month", consoles=None, indicators=None, dest_kind=None, cfg=None):
    cfg = cfg or load_indicators()
    inds = [i for i in enabled_indicators(cfg) if not indicators or i["key"] in indicators]
    keys = [i["key"] for i in inds]
    start, end = month_range(start_month, end_month)
    store.ensure_groups(cfg)
    rows = store.events(start, end, consoles, keys, dest_kind=dest_kind)
    dests = store.effective_destinations()
    groups = store.groups()
    accounts_meta = store.accounts()
    sf = store.sf_accounts()
    periods = periods_between(start_month, end_month, granularity)

    def dest(h):
        return dests.get(h) or {"label": h, "kind": "other", "vendor": h, "vendor_label": h, "vendor_kind": "other", "note": "", "override": False}

    # trend: per period, devices per indicator, per destination kind and per competitor (vendor group)
    trend = {p: {"period": p, "devices": 0, "events": 0, "accounts": 0, "by_indicator": {k: 0 for k in keys}, "by_destination": {}, "by_vendor": {}} for p in periods}
    buckets = {}
    for r in rows:
        buckets.setdefault(period_key(r["created_at"], granularity), []).append(r)
    for p, prs in buckets.items():
        if p not in trend:
            continue
        t = trend[p]
        t["events"] = len(prs)
        t["devices"] = _count_devices(prs)
        t["accounts"] = len({(r["console"], r["account_id"]) for r in prs})
        for k in keys:
            t["by_indicator"][k] = _count_devices([r for r in prs if r["indicator"] == k])
        for r in prs:
            if r["destination_host"]:
                d = dest(r["destination_host"])
                t["by_destination"].setdefault(d["kind"] or "other", set()).add((r["console"], r["device_key"]))
                t["by_vendor"].setdefault(d["vendor"] or r["destination_host"], set()).add((r["console"], r["device_key"]))
        t["by_destination"] = {k: len(v) for k, v in t["by_destination"].items()}
        t["by_vendor"] = {k: len(v) for k, v in t["by_vendor"].items()}

    # accounts
    by_acct = {}
    for r in rows:
        by_acct.setdefault((r["console"], r["account_id"]), []).append(r)
    accounts = []
    for (console, aid), ars in by_acct.items():
        meta = accounts_meta.get((console, aid)) or {}
        sfid = meta.get("salesforce_id") or ""
        dest_counts = {}
        for r in ars:
            if r["destination_host"]:
                dest_counts.setdefault(r["destination_host"], set()).add((r["console"], r["device_key"]))
        # one chip per competitor; its hosts listed inside, biggest first
        by_vendor = {}
        for h, v in dest_counts.items():
            d = dest(h)
            g = by_vendor.setdefault(d["vendor"] or h, {"vendor": d["vendor"] or h, "label": d["vendor_label"], "kind": d["vendor_kind"], "devices": set(), "hosts": []})
            g["devices"] |= v
            g["hosts"].append({"host": h, "devices": len(v), "label": d["label"], "kind": d["kind"]})
        vendor_chips = []
        for g in by_vendor.values():
            g["hosts"].sort(key=lambda x: -x["devices"])
            vendor_chips.append({**g, "devices": len(g["devices"]), "host": g["hosts"][0]["host"]})
        vendor_chips.sort(key=lambda g: -g["devices"])
        series = {p: 0 for p in periods}
        for p, prs in buckets.items():
            if p in series:
                series[p] = _count_devices([r for r in prs if r["console"] == console and r["account_id"] == aid])
        accounts.append({
            "console": console, "account_id": aid, "account": meta.get("name") or ars[-1]["account_name"], "state": meta.get("state") or "",
            "salesforce_id": sfid, "sf_account": (sf.get(sfid) or {}).get("name") or "", "customer_id": (sf.get(sfid) or {}).get("customer_id") or "",
            "events": len(ars), "devices": _count_devices(ars),
            "by_indicator": {k: _count_devices([r for r in ars if r["indicator"] == k]) for k in keys},
            "destinations": vendor_chips,
            "destination_hosts": sorted(({"host": h, "devices": len(v), "label": dest(h)["label"], "kind": dest(h)["kind"], "vendor": dest(h)["vendor"]}
                                         for h, v in dest_counts.items()), key=lambda d: -d["devices"]),
            "last_event": ars[-1]["created_at"], "first_event": ars[0]["created_at"],
            "series": [series[p] for p in periods],
        })
    accounts.sort(key=lambda a: (-a["devices"], a["account"]))

    # destinations table
    dest_rows = {}
    for r in rows:
        if r["destination_host"]:
            d = dest_rows.setdefault(r["destination_host"], {"devices": set(), "accounts": set(), "last": r["created_at"]})
            d["devices"].add((r["console"], r["device_key"])); d["accounts"].add((r["console"], r["account_id"])); d["last"] = max(d["last"], r["created_at"])
    destinations = sorted(({"host": h, "label": dest(h)["label"], "kind": dest(h)["kind"], "note": dest(h).get("note") or "",
                            "edited": bool(dest(h).get("override")), "vendor": dest(h)["vendor"] or h, "vendor_label": dest(h)["vendor_label"],
                            "devices": len(d["devices"]), "accounts": len(d["accounts"]), "last_event": d["last"]} for h, d in dest_rows.items()),
                          key=lambda d: -d["devices"])

    # competitors: one row per vendor group with its consoles
    grp_rows = {}
    for h, d in dest_rows.items():
        key = dest(h)["vendor"] or h
        g = grp_rows.setdefault(key, {"devices": set(), "accounts": set(), "last": d["last"], "hosts": []})
        g["devices"] |= d["devices"]; g["accounts"] |= d["accounts"]; g["last"] = max(g["last"], d["last"]); g["hosts"].append(h)
    competitors = []
    for key, g in grp_rows.items():
        meta = groups.get(key) or {}
        first = dest(g["hosts"][0])
        competitors.append({"key": key, "label": meta.get("label") or first["vendor_label"], "kind": meta.get("kind") or first["vendor_kind"],
                            "note": meta.get("note") or "", "edited": bool(meta.get("edited")), "known": key in groups,
                            "hosts": sorted(g["hosts"], key=lambda h: -len(dest_rows[h]["devices"])), "consoles": len(g["hosts"]),
                            "devices": len(g["devices"]), "accounts": len(g["accounts"]), "last_event": g["last"]})
    competitors.sort(key=lambda c: -c["devices"])

    migr = [r for r in rows if r["kind"] == "migration"]
    external = [r for r in migr if r["destination_host"] and dest(r["destination_host"])["kind"] != "internal"]
    totals = {
        "events": len(rows), "devices": _count_devices(rows), "accounts": len(by_acct),
        "by_indicator": {k: _count_devices([r for r in rows if r["indicator"] == k]) for k in keys},
        "migrated_devices": _count_devices(migr), "migrated_external": _count_devices(external),
        "uninstalled_devices": _count_devices([r for r in rows if r["kind"] == "uninstall"]),
        "top_destination": destinations[0] if destinations else None,
        "top_competitor": next((c for c in competitors if c["kind"] != "internal"), None),
    }
    return {"start": start_month, "end": end_month, "granularity": granularity, "periods": periods, "trend": [trend[p] for p in periods],
            "totals": totals, "accounts": accounts, "destinations": destinations, "competitors": competitors,
            "groups": sorted(({"key": k, "label": g["label"], "kind": g["kind"]} for k, g in groups.items()), key=lambda g: g["label"].lower()),
            "indicators": [{k: i[k] for k in ("type", "key", "label", "short", "kind", "note")} for i in inds]}


def account_detail(store, console, account_id, start_month, end_month, granularity="month", cfg=None):
    cfg = cfg or load_indicators()
    keys = [i["key"] for i in enabled_indicators(cfg)]
    start, end = month_range(start_month, end_month)
    rows = store.events(start, end, indicators=keys, account=(console, account_id))
    dests = store.effective_destinations()
    meta = store.accounts().get((console, account_id)) or {}
    sf = store.sf_accounts().get(meta.get("salesforce_id") or "") or {}
    periods = periods_between(start_month, end_month, granularity)
    series = []
    for p in periods:
        prs = [r for r in rows if period_key(r["created_at"], granularity) == p]
        series.append({"period": p, "devices": _count_devices(prs), "events": len(prs),
                       "by_indicator": {k: _count_devices([r for r in prs if r["indicator"] == k]) for k in keys}})
    devices = [{**r, "destination_label": (dests.get(r["destination_host"]) or {}).get("label", ""),
                "destination_kind": (dests.get(r["destination_host"]) or {}).get("kind", ""),
                "destination_vendor": (dests.get(r["destination_host"]) or {}).get("vendor_label", "")} for r in latest_per_device(rows)]
    sites = {}
    for r in rows:
        s = sites.setdefault((r["site_id"], r["site_name"]), set()); s.add((r["console"], r["device_key"]))
    return {"console": console, "account_id": account_id, "account": meta.get("name") or (rows[-1]["account_name"] if rows else ""),
            "salesforce_id": meta.get("salesforce_id") or "", "sf_account": sf.get("name") or "", "customer_id": sf.get("customer_id") or "",
            "country": sf.get("country") or "", "state": meta.get("state") or "", "events": len(rows), "devices": len(devices),
            "series": series, "device_rows": devices,
            "sites": sorted(({"site_id": k[0], "site": k[1], "devices": len(v)} for k, v in sites.items()), key=lambda s: -s["devices"])}


def device_rows(store, start_month, end_month, consoles=None, indicators=None, dest_kind=None, cfg=None):
    """Export view: most recent record per device, with the ticket's fields."""
    cfg = cfg or load_indicators()
    keys = [i["key"] for i in enabled_indicators(cfg) if not indicators or i["key"] in indicators]
    start, end = month_range(start_month, end_month)
    rows = store.events(start, end, consoles, keys, dest_kind=dest_kind)
    dests = store.effective_destinations()
    accounts = store.accounts()
    sf = store.sf_accounts()
    out = []
    for r in latest_per_device(rows):
        meta = accounts.get((r["console"], r["account_id"])) or {}
        sfid = meta.get("salesforce_id") or ""
        out.append({**r, "salesforce_id": sfid, "sf_account": (sf.get(sfid) or {}).get("name") or "",
                    "destination_label": (dests.get(r["destination_host"]) or {}).get("label", ""),
                    "destination_kind": (dests.get(r["destination_host"]) or {}).get("kind", ""),
                    "destination_vendor": (dests.get(r["destination_host"]) or {}).get("vendor_label", "")})
    return out


def health(store, consoles):
    st = store.status()
    out = []
    for c in consoles:
        s = st.get(c)
        if not s:
            out.append({"console": c, "state": "never", "message": "not synced yet", "last_sync": None, "last_event": None})
        else:
            out.append({"console": c, "state": "ok" if s["ok"] else s["kind"], "message": s["message"], "last_sync": s["last_sync"],
                        "last_event": s["last_event"], "events": s["events"]})
    return out


# ── exports ────────────────────────────────────────────────────────────────

DEVICE_COLUMNS = [("Console", "console"), ("Account ID", "account_id"), ("Account", "account_name"), ("Salesforce ID", "salesforce_id"),
                  ("Salesforce account", "sf_account"), ("Site ID", "site_id"), ("Site", "site_name"), ("Group ID", "group_id"), ("Group", "group_name"),
                  ("Agent ID", "agent_id"), ("Device UUID", "device_uuid"), ("Computer", "computer_name"), ("OS", "os_type"), ("Type", "machine_type"),
                  ("Indicator", "indicator"), ("Activity type", "activity_type"), ("When (UTC)", "created_at"), ("User", "username"),
                  ("Destination console", "destination_url"), ("Destination", "destination_label"), ("Competitor", "destination_vendor"), ("Destination kind", "destination_kind"),
                  ("Description", "primary_description")]


def to_csv(rows, columns, note=""):
    buf = io.StringIO()
    if note:
        buf.write(f"# {note}\r\n")
    w = csv.writer(buf)
    w.writerow([c[0] for c in columns])
    for r in rows:
        w.writerow([r.get(c[1], "") for c in columns])
    return buf.getvalue()


def trend_columns(keys):
    return [("Period", "period"), ("Devices", "devices"), ("Events", "events"), ("Accounts", "accounts")] + [(k, k) for k in keys]


def flatten_trend(trend, keys):
    out = []
    for t in trend:
        row = {"period": t["period"], "devices": t["devices"], "events": t["events"], "accounts": t["accounts"]}
        row.update({k: t["by_indicator"].get(k, 0) for k in keys})
        for dk, n in t["by_destination"].items():
            row[f"dest_{dk}"] = n
        for vk, n in t.get("by_vendor", {}).items():
            row[f"vendor_{vk}"] = n
        out.append(row)
    return out


def to_xlsx(summary_data, devices, cfg=None):
    import openpyxl
    from openpyxl.styles import Font
    keys = [i["key"] for i in summary_data["indicators"]]
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Trend"
    ws.append([f"SentinelOne platform churn — {summary_data['start']} to {summary_data['end']} — per {summary_data['granularity']} — devices counted once per period"])
    ws["A1"].font = Font(bold=True, size=13)
    dest_kinds = sorted({k for t in summary_data["trend"] for k in t["by_destination"]})
    ws.append(["Period", "Devices", "Events", "Accounts", *[f"{k} (devices)" for k in keys], *[f"to {k} console (devices)" for k in dest_kinds]])
    for c in ws[2]:
        c.font = Font(bold=True)
    for t in summary_data["trend"]:
        ws.append([t["period"], t["devices"], t["events"], t["accounts"], *[t["by_indicator"].get(k, 0) for k in keys], *[t["by_destination"].get(k, 0) for k in dest_kinds]])
    sh = wb.create_sheet("Accounts")
    sh.append(["Console", "Account ID", "Account", "Salesforce ID", "Salesforce account", "Devices", "Events", *keys, "Destinations", "First event", "Last event"])
    for c in sh[1]:
        c.font = Font(bold=True)
    for a in summary_data["accounts"]:
        sh.append([a["console"], a["account_id"], a["account"], a["salesforce_id"], a["sf_account"], a["devices"], a["events"],
                   *[a["by_indicator"].get(k, 0) for k in keys], "; ".join(f"{d['label']} ({d['devices']})" for d in a["destinations"]), a["first_event"], a["last_event"]])
    sc = wb.create_sheet("Competitors")
    sc.append(["Competitor", "Kind", "Consoles", "Devices", "Accounts", "Last event", "Hosts", "Note"])
    for c in sc[1]:
        c.font = Font(bold=True)
    for g in summary_data.get("competitors", []):
        sc.append([g["label"], g["kind"], g["consoles"], g["devices"], g["accounts"], g["last_event"], "; ".join(g["hosts"]), g["note"]])
    sd = wb.create_sheet("Destinations")
    sd.append(["Host", "Competitor", "Label", "Kind", "Devices", "Accounts", "Last event", "Note"])
    for c in sd[1]:
        c.font = Font(bold=True)
    for d in summary_data["destinations"]:
        sd.append([d["host"], d.get("vendor_label", ""), d["label"], d["kind"], d["devices"], d["accounts"], d["last_event"], d["note"]])
    dv = wb.create_sheet("Devices")
    dv.append([c[0] for c in DEVICE_COLUMNS])
    for c in dv[1]:
        c.font = Font(bold=True)
    for r in devices:
        dv.append([r.get(c[1], "") for c in DEVICE_COLUMNS])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()
