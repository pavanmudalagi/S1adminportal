"""SQLite store for EAP requests, their enrichment, console-check results and the audit trail.

One file at ~/.config/s1am/eap.db (next to web_labels.json); single user, local. Fields the
user edits in the app are remembered per row in `edited_fields` and are never overwritten
by a sync, re-enrichment or console check; everything else is recomputed freely.
"""
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / ".config" / "s1am" / "eap.db"

# (column, type). Missing columns are added on open, so the schema can grow.
COLUMNS = [
    # from the workbook
    ("start_time", "TEXT"), ("completion_time", "TEXT"), ("last_modified", "TEXT"),
    ("responder_email", "TEXT"), ("joe_ref", "TEXT"), ("joe_status", "TEXT"),
    ("contact_name", "TEXT"), ("company", "TEXT"), ("contact_email", "TEXT"),
    ("super_admin_raw", "TEXT"), ("super_admin_name", "TEXT"), ("super_admin_email", "TEXT"),
    ("super_admin_source", "TEXT"), ("super_admin_needs_review", "INTEGER"), ("super_admin_note", "TEXT"),
    ("products_raw", "TEXT"), ("products", "TEXT"), ("wants_prompt", "INTEGER"),
    ("details_raw", "TEXT"), ("details", "TEXT"), ("no_s1_claimed", "INTEGER"),
    # from Salesforce
    ("sf_account_id", "TEXT"), ("sf_account_name", "TEXT"), ("customer_id", "TEXT"), ("country", "TEXT"),
    ("sf_tenants", "TEXT"), ("sf_tenant", "TEXT"), ("sf_tenant_id", "TEXT"),
    ("match_rule", "INTEGER"), ("match_confidence", "TEXT"), ("match_flag", "TEXT"), ("match_note", "TEXT"),
    ("complete_agents", "INTEGER"), ("control_agents", "INTEGER"), ("total_agents", "INTEGER"),
    ("sf_updated_at", "TEXT"), ("sf_team", "TEXT"), ("sf_team_at", "TEXT"),
    # from the SentinelOne console
    ("s1_console", "TEXT"), ("s1_account_id", "TEXT"), ("s1_account_name", "TEXT"), ("s1_state", "TEXT"),
    ("s1_skus", "TEXT"), ("s1_prompt_enabled", "INTEGER"), ("s1_prompt_bundles", "TEXT"),
    ("s1_prompt_tenant", "TEXT"), ("s1_prompt_users", "TEXT"), ("s1_prompt_region", "TEXT"),
    ("s1_target_source", "TEXT"), ("s1_checked_at", "TEXT"), ("s1_error", "TEXT"),
    # workflow
    ("status", "TEXT"), ("status_reason", "TEXT"),
    # user-editable
    ("notes", "TEXT"), ("first_contacted", "TEXT"), ("enabled_yn", "TEXT"), ("date_enabled", "TEXT"),
    ("enabled_by", "TEXT"), ("prompt_tenant_name", "TEXT"), ("first_users", "TEXT"),
    ("chosen_console", "TEXT"), ("chosen_account_id", "TEXT"), ("bundle_counts", "TEXT"),
    ("tenant_confirmed", "INTEGER"), ("tenant_confirmed_at", "TEXT"), ("tenant_confirmed_by", "TEXT"),
    ("edited_fields", "TEXT"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
    ("email_drafted_at", "TEXT"), ("email_drafted_by", "TEXT"),  # customer email opened from the tracker
    ("origin", "TEXT"),  # "form" (default, from the workbook) or "manual" (added in the UI / CLI)
    ("merged_into", "INTEGER"),  # manual row folded into this form row (hidden from listings)
]
MANUAL_ID_BASE = 100000  # manual requests live far above the Forms response ids, so a sync can never collide
JSON_COLUMNS = {"products", "details", "sf_tenants", "s1_skus", "s1_prompt_bundles", "s1_prompt_users",
                "first_users", "bundle_counts", "edited_fields", "sf_team"}

# A request can need Prompt on more than one tenant (customer with several contract tenants).
# The first/primary tenant lives on the request row; further ones are rows here, each with
# its own console account, check result, provisioning details and status.
TENANT_COLUMNS = [
    ("request_id", "INTEGER"), ("tenant_name", "TEXT"), ("tenant_id", "TEXT"), ("console", "TEXT"),
    ("account_id", "TEXT"), ("source", "TEXT"), ("confirmed_at", "TEXT"), ("confirmed_by", "TEXT"),
    ("prompt_tenant_name", "TEXT"), ("first_users", "TEXT"),
    ("s1_account_name", "TEXT"), ("s1_state", "TEXT"), ("s1_skus", "TEXT"), ("s1_prompt_enabled", "INTEGER"),
    ("s1_prompt_bundles", "TEXT"), ("s1_prompt_tenant", "TEXT"), ("s1_prompt_users", "TEXT"),
    ("s1_prompt_region", "TEXT"), ("s1_checked_at", "TEXT"), ("s1_error", "TEXT"),
    ("status", "TEXT"), ("status_reason", "TEXT"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
]
TENANT_JSON_COLUMNS = {"first_users", "s1_skus", "s1_prompt_bundles", "s1_prompt_users"}
TENANT_USER_FIELDS = {"prompt_tenant_name", "first_users", "status"}
FORM_FIELDS = [c for c, _ in COLUMNS[:COLUMNS.index(("no_s1_claimed", "INTEGER")) + 1]]
USER_FIELDS = {"notes", "first_contacted", "enabled_yn", "date_enabled", "enabled_by", "prompt_tenant_name",
               "first_users", "chosen_console", "chosen_account_id", "bundle_counts", "sf_tenant", "status",
               "tenant_confirmed"}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY)")
            have = {r["name"] for r in c.execute("PRAGMA table_info(requests)")}
            for name, typ in COLUMNS:
                if name not in have:
                    c.execute(f"ALTER TABLE requests ADD COLUMN {name} {typ}")
            c.execute("CREATE TABLE IF NOT EXISTS request_tenants (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            have = {r["name"] for r in c.execute("PRAGMA table_info(request_tenants)")}
            for name, typ in TENANT_COLUMNS:
                if name not in have:
                    c.execute(f"ALTER TABLE request_tenants ADD COLUMN {name} {typ}")
            c.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, "
                      "ts TEXT, kind TEXT, message TEXT, data TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, "
                      "console TEXT, account_id TEXT, action TEXT, dry_run INTEGER, payload_hash TEXT, result TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    @staticmethod
    def _encode(fields, json_cols=JSON_COLUMNS):
        return {k: (json.dumps(v) if k in json_cols and v is not None else v) for k, v in fields.items()}

    @staticmethod
    def _decode(row, json_cols=JSON_COLUMNS):
        d = dict(row)
        for k in json_cols:
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    # ── requests ────────────────────────────────────────────────────────

    def get(self, request_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            return None
        d = self._decode(row)
        d["extra_tenants"] = self.tenants(request_id)
        return d

    def list(self, include_merged=False):
        with self._conn() as c:
            rows = [self._decode(r) for r in c.execute("SELECT * FROM requests ORDER BY id")]
            if not include_merged:
                rows = [r for r in rows if not r.get("merged_into")]
            extras = {}
            for t in c.execute("SELECT * FROM request_tenants ORDER BY id"):
                extras.setdefault(t["request_id"], []).append(self._decode(t, TENANT_JSON_COLUMNS))
        for r in rows:
            r["extra_tenants"] = extras.get(r["id"], [])
        return rows

    # ── additional tenants ──────────────────────────────────────────────

    def tenants(self, request_id):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM request_tenants WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
        return [self._decode(r, TENANT_JSON_COLUMNS) for r in rows]

    def get_tenant(self, tenant_row_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM request_tenants WHERE id = ?", (tenant_row_id,)).fetchone()
        return self._decode(row, TENANT_JSON_COLUMNS) if row else None

    def add_tenant(self, request_id, fields):
        enc = self._encode(dict(fields, request_id=request_id, created_at=_now(), updated_at=_now()), TENANT_JSON_COLUMNS)
        with self._conn() as c:
            cur = c.execute(f"INSERT INTO request_tenants ({', '.join(enc)}) VALUES ({', '.join('?' * len(enc))})",
                            list(enc.values()))
            return cur.lastrowid

    def update_tenant(self, tenant_row_id, fields):
        enc = self._encode(dict(fields, updated_at=_now()), TENANT_JSON_COLUMNS)
        with self._conn() as c:
            c.execute(f"UPDATE request_tenants SET {', '.join(f'{k} = ?' for k in enc)} WHERE id = ?",
                      [*enc.values(), tenant_row_id])

    def delete_tenant(self, tenant_row_id):
        with self._conn() as c:
            c.execute("DELETE FROM request_tenants WHERE id = ?", (tenant_row_id,))

    def upsert_form_row(self, rec):
        """Insert or refresh the workbook-derived fields of one row → "new" | "updated" | "unchanged"."""
        fields = {k: rec.get(k) for k in FORM_FIELDS}
        existing = self.get(rec["id"])
        if not existing:
            fields.update(id=rec["id"], status="new", edited_fields=[], created_at=_now(), updated_at=_now())
            enc = self._encode(fields)
            with self._conn() as c:
                c.execute(f"INSERT INTO requests ({', '.join(enc)}) VALUES ({', '.join('?' * len(enc))})",
                          list(enc.values()))
            return "new"
        changed = {k: v for k, v in fields.items() if (existing.get(k) or None) != (v or None)}
        if not changed:
            return "unchanged"
        self.update(rec["id"], changed, source="sync")
        return "updated"

    def repoint(self, from_id, to_id):
        """Move the extra tenants and the history of one request onto another (used when merging a duplicate)."""
        with self._conn() as c:
            c.execute("UPDATE request_tenants SET request_id = ? WHERE request_id = ?", (to_id, from_id))
            c.execute("UPDATE history SET request_id = ? WHERE request_id = ?", (to_id, from_id))

    def next_manual_id(self):
        with self._conn() as c:
            row = c.execute("SELECT MAX(id) FROM requests WHERE id >= ?", (MANUAL_ID_BASE,)).fetchone()
        return (row[0] or MANUAL_ID_BASE) + 1

    def update(self, request_id, fields, source="system"):
        """Write fields. source="user" marks them as edited (protected); any other source
        skips fields the user has edited. Returns True if anything was written."""
        row = self.get(request_id)
        if row is None:
            raise KeyError(f"No request {request_id}")
        edited = set(row.get("edited_fields") or [])
        if source == "user":
            edited |= set(fields)
            fields = dict(fields, edited_fields=sorted(edited))
        else:
            fields = {k: v for k, v in fields.items() if k not in edited}
        if not fields:
            return False
        fields["updated_at"] = _now()
        enc = self._encode(fields)
        with self._conn() as c:
            c.execute(f"UPDATE requests SET {', '.join(f'{k} = ?' for k in enc)} WHERE id = ?",
                      [*enc.values(), request_id])
        return True

    # ── history / audit / meta ──────────────────────────────────────────

    def add_history(self, request_id, kind, message, data=None):
        with self._conn() as c:
            c.execute("INSERT INTO history (request_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
                      (request_id, _now(), kind, message, json.dumps(data) if data is not None else None))

    def history(self, request_id):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM history WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                try:
                    d["data"] = json.loads(d["data"])
                except ValueError:
                    pass
            out.append(d)
        return out

    def audit(self, actor, console, account_id, action, dry_run, payload, result):
        """Append one line per SentinelOne write (or dry run). Payloads are hashed, not stored."""
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
        with self._conn() as c:
            c.execute("INSERT INTO audit (ts, actor, console, account_id, action, dry_run, payload_hash, result) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (_now(), actor, console, str(account_id), action, int(bool(dry_run)), digest, result))

    def set_meta(self, key, value):
        with self._conn() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, value))

    def get_meta(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
