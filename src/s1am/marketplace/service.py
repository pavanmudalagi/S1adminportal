"""Singularity Marketplace connectors across every configured console — read-only.

Data model (built from the real response of the Staging console, 2026-11-13):

* ``GET singularity-marketplace/applications`` returns one item per installed app with
  ``name``, ``applicationCatalogId``, ``hasAlert``, ``lastInstalledAt`` and ``scopes[]``.
  Each scope is one *instance*. Besides the documented fields it carries ``scopeLevel``
  (``account`` | ``site`` | ``global``), ``scopeId``, ``site``/``siteId``, the actual
  ``status`` (active, disabled, activationfailed, deactivated) next to ``desiredStatus``,
  ``errorLog``, ``retryUntil``, ``modifier`` and ``updatedAt``. ``icon`` is a large base64
  image and is dropped. ``accountIds`` / ``siteIds`` query filters are honoured.
* ``GET singularity-marketplace/applications-catalog`` lists every app (202 on Staging) with
  ``category``, ``type`` and ``installed``. Cursor pages must be requested with the cursor
  alone — the API rejects ``cursor`` combined with ``limit``.
* Rate limit: 5 req/s per token, burst 20. Calls are sequential per console with a small
  delay, 429 → back off and retry; consoles run in parallel.

Nothing here installs, deletes or toggles anything.
"""
import csv
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..api.exceptions import SentinelOneError

APPS_PATH = "singularity-marketplace/applications"
CATALOG_PATH = "singularity-marketplace/applications-catalog"
PAGE_LIMIT = 100
CALL_DELAY = 0.25          # seconds between calls on one console (≤ 4 req/s)
MAX_RETRIES = 4
DEFAULT_TTL = 300          # seconds the scan is served from memory
MAX_WORKERS = 6            # consoles scanned concurrently

INSTANCE_FIELDS = [
    "console", "app_name", "catalog_id", "category", "app_type", "instance_id", "instance_name",
    "scope_level", "account_id", "account", "site_id", "site", "scope_id", "desired_status", "status",
    "has_alert", "alert_message", "error_log", "retry_until", "creator", "modifier", "created_at",
    "updated_at", "last_entity_created_at", "app_last_installed_at", "app_has_alert",
]
GLOBAL_ACCOUNT_ID = "global"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── parsing ────────────────────────────────────────────────────────────────

def parse_catalog(payload_or_items):
    """Catalog id → {name, category, type, installed, key}."""
    items = payload_or_items.get("data", []) if isinstance(payload_or_items, dict) else payload_or_items
    out = {}
    for it in items or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        out[str(it["id"])] = {"name": it.get("name"), "category": it.get("category"), "type": it.get("type"),
                              "installed": bool(it.get("installed")), "key": it.get("key")}
    return out


def _error_text(err):
    """``errorLog`` is sometimes a dict with a message; keep it short and stringy."""
    if not err:
        return ""
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or json.dumps(err, default=str)
    return str(err)[:500]


def parse_applications(payload_or_items, console, catalog=None):
    """Flatten the applications response into one row per instance (scope)."""
    items = payload_or_items.get("data", []) if isinstance(payload_or_items, dict) else payload_or_items
    catalog = catalog or {}
    rows = []
    for app in items or []:
        if not isinstance(app, dict):
            continue
        cat = catalog.get(str(app.get("applicationCatalogId") or ""), {})
        for sc in app.get("scopes") or []:
            level = (sc.get("scopeLevel") or ("site" if sc.get("siteId") else "account" if sc.get("accountId") else "global")).lower()
            account_id = str(sc.get("accountId") or "") or (GLOBAL_ACCOUNT_ID if level == "global" else "")
            rows.append({
                "console": console,
                "app_name": (app.get("name") or "").strip(),
                "catalog_id": str(app.get("applicationCatalogId") or ""),
                "category": cat.get("category"),
                "app_type": cat.get("type"),
                "instance_id": str(sc.get("id") or ""),
                "instance_name": (sc.get("applicationInstanceName") or app.get("name") or "").strip(),
                "scope_level": level,
                "account_id": account_id,
                "account": sc.get("account") or ("Global scope" if level == "global" else ""),
                "site_id": str(sc.get("siteId") or "") or None,
                "site": sc.get("site") if level == "site" else None,
                "scope_id": str(sc.get("scopeId") or "") or None,
                "desired_status": (sc.get("desiredStatus") or "").lower(),
                "status": (sc.get("status") or "").lower(),
                "has_alert": bool(sc.get("hasAlert")),
                "alert_message": (sc.get("alertMessage") or "")[:500],
                "error_log": _error_text(sc.get("errorLog")),
                "retry_until": sc.get("retryUntil"),
                "creator": sc.get("creator"),
                "modifier": sc.get("modifier"),
                "created_at": sc.get("createdAt"),
                "updated_at": sc.get("updatedAt"),
                "last_entity_created_at": sc.get("lastEntityCreatedAt"),
                "app_last_installed_at": app.get("lastInstalledAt"),
                "app_has_alert": bool(app.get("hasAlert")),
            })
    return rows


# ── fetching ───────────────────────────────────────────────────────────────

def _classify(exc):
    code = getattr(exc, "status_code", None)
    text = str(getattr(exc, "payload", "") or exc).lower()
    if code == 401:
        return "auth", "Unauthorized — the API token is invalid or expired"
    if code == 403 or "permission" in text or "forbidden" in text:
        return "permission", "The token lacks the Singularity Marketplace view permission"
    if code == 404:
        return "unsupported", "Singularity Marketplace is not available on this console"
    if "timed out" in text or "timeout" in text:
        return "timeout", "Timed out talking to the console"
    return "error", str(exc)[:300]


def _get_all(client, path, sleep=time.sleep):
    """Cursor pagination with throttling and 429 back-off. Cursor is sent alone after page 1."""
    params = {"limit": PAGE_LIMIT}
    items = []
    while True:
        attempt = 0
        while True:
            try:
                data = client.request("GET", path, params=params)
                break
            except SentinelOneError as exc:
                if getattr(exc, "status_code", None) == 429 and attempt < MAX_RETRIES:
                    attempt += 1
                    sleep(min(2.0 * attempt, 8.0))
                    continue
                raise
        page = data.get("data") if isinstance(data, dict) else data
        items.extend(page or [])
        cursor = ((data.get("pagination") or {}).get("nextCursor")) if isinstance(data, dict) else None
        if not cursor:
            return items
        params = {"cursor": cursor}
        sleep(CALL_DELAY)


def fetch_console(client, console, sleep=time.sleep):
    """Catalog then applications for one console. Never raises; errors land in the result."""
    result = {"console": console, "ok": False, "error": None, "error_kind": None, "instances": [],
              "catalog_size": 0, "fetched_at": _now()}
    try:
        catalog = {}
        try:
            catalog = parse_catalog(_get_all(client, CATALOG_PATH, sleep))
            sleep(CALL_DELAY)
        except SentinelOneError as exc:
            # The catalog is only used for categories; a failure there must not hide the instances.
            kind, _ = _classify(exc)
            if kind in ("auth", "permission", "unsupported"):
                raise
        apps = _get_all(client, APPS_PATH, sleep)
        result["instances"] = parse_applications(apps, console, catalog)
        result["catalog_size"] = len(catalog)
        result["ok"] = True
    except SentinelOneError as exc:
        result["error_kind"], result["error"] = _classify(exc)
    except Exception as exc:  # network, config, anything else
        result["error_kind"], result["error"] = "error", str(exc)[:300]
    return result


def scan(client_for, consoles, workers=MAX_WORKERS, sleep=time.sleep):
    """Parallel across consoles, sequential inside each. Returns every console's result."""
    def one(code):
        try:
            client = client_for(code)
        except Exception as exc:
            return {"console": code, "ok": False, "error": str(exc)[:300], "error_kind": "config",
                    "instances": [], "catalog_size": 0, "fetched_at": _now()}
        return fetch_console(client, code, sleep)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(consoles) or 1))) as pool:
        results = list(pool.map(one, consoles))
    return {"fetched_at": _now(), "consoles": results}


# ── cache ──────────────────────────────────────────────────────────────────

_cache = {"scan": None, "at": 0.0}
_cache_lock = threading.Lock()


def cached_scan(client_for, consoles, force=False, ttl=DEFAULT_TTL, **kw):
    with _cache_lock:
        fresh = _cache["scan"] and (time.time() - _cache["at"]) < ttl
        if fresh and not force and set(c["console"] for c in _cache["scan"]["consoles"]) == set(consoles):
            return dict(_cache["scan"], from_cache=True, age=int(time.time() - _cache["at"]))
    result = scan(client_for, consoles, **kw)
    with _cache_lock:
        _cache["scan"], _cache["at"] = result, time.time()
    return dict(result, from_cache=False, age=0)


def clear_cache():
    with _cache_lock:
        _cache["scan"], _cache["at"] = None, 0.0


# ── views ──────────────────────────────────────────────────────────────────

def instances_of(scan_result):
    return [i for c in scan_result["consoles"] for i in c["instances"]]


def account_rows(instances):
    """One row per (console, account): counts, alerts, last install, sites and connector names."""
    acc = {}
    for i in instances:
        key = (i["console"], i["account_id"])
        r = acc.setdefault(key, {"console": i["console"], "account_id": i["account_id"], "account": i["account"],
                                 "instances": 0, "enabled": 0, "alerts": 0, "sites": set(), "connectors": set(),
                                 "last_installed_at": None, "last_entity_created_at": None, "site_level": 0, "account_level": 0})
        r["instances"] += 1
        r["enabled"] += int(i["desired_status"] == "active")
        r["alerts"] += int(i["has_alert"] or i["status"] == "activationfailed")
        if i["site"]:
            r["sites"].add(i["site"]); r["site_level"] += 1
        else:
            r["account_level"] += 1
        r["connectors"].add(i["app_name"])
        for src, dst in (("created_at", "last_installed_at"), ("last_entity_created_at", "last_entity_created_at")):
            if i.get(src) and (r[dst] is None or i[src] > r[dst]):
                r[dst] = i[src]
    rows = []
    for r in acc.values():
        r["sites"] = sorted(r["sites"]); r["connectors"] = sorted(r["connectors"])
        rows.append(r)
    rows.sort(key=lambda r: (r["console"], (r["account"] or "").lower()))
    return rows


def connector_rows(instances):
    """Reverse view: one row per connector (app name) with every account/site that has it."""
    by = {}
    for i in instances:
        r = by.setdefault(i["app_name"], {"app_name": i["app_name"], "category": i["category"], "catalog_ids": set(),
                                          "instances": 0, "alerts": 0, "enabled": 0, "consoles": set(), "accounts": set(), "sites": set()})
        r["catalog_ids"].add(i["catalog_id"]); r["instances"] += 1
        r["alerts"] += int(i["has_alert"] or i["status"] == "activationfailed")
        r["enabled"] += int(i["desired_status"] == "active")
        r["consoles"].add(i["console"]); r["accounts"].add((i["console"], i["account_id"], i["account"]))
        if i["site"]:
            r["sites"].add((i["console"], i["account"], i["site"]))
        r["category"] = r["category"] or i["category"]
    rows = []
    for r in by.values():
        r["catalog_ids"] = sorted(r["catalog_ids"]); r["consoles"] = sorted(r["consoles"])
        r["accounts"] = [{"console": c, "account_id": a, "account": n} for c, a, n in sorted(r["accounts"], key=lambda x: (x[0], (x[2] or "").lower()))]
        r["sites"] = [{"console": c, "account": a, "site": s} for c, a, s in sorted(r["sites"], key=lambda x: (x[0], (x[1] or "").lower(), (x[2] or "").lower()))]
        r["account_count"] = len(r["accounts"]); r["site_count"] = len(r["sites"])
        rows.append(r)
    rows.sort(key=lambda r: r["app_name"].lower())
    return rows


def totals(scan_result):
    inst = instances_of(scan_result)
    return {
        "consoles_scanned": sum(1 for c in scan_result["consoles"] if c["ok"]),
        "consoles_failed": sum(1 for c in scan_result["consoles"] if not c["ok"]),
        "accounts_with_connectors": len({(i["console"], i["account_id"]) for i in inst}),
        "instances": len(inst),
        "instances_with_alerts": sum(1 for i in inst if i["has_alert"] or i["status"] == "activationfailed"),
        "connectors": len({i["app_name"] for i in inst}),
    }


def account_detail(scan_result, console, account_id):
    """Instances of one account grouped console → account → site → connector."""
    inst = [i for i in instances_of(scan_result) if i["console"] == console and i["account_id"] == str(account_id)]
    groups = {}
    for i in inst:
        key = i["site"] or ""
        g = groups.setdefault(key, {"site": i["site"], "site_id": i["site_id"], "level": "site" if i["site"] else "account", "instances": []})
        g["instances"].append(i)
    ordered = sorted(groups.values(), key=lambda g: (g["level"] != "account", (g["site"] or "").lower()))
    for g in ordered:
        g["instances"].sort(key=lambda i: (i["app_name"].lower(), i["instance_name"].lower()))
    return {"console": console, "account_id": str(account_id), "account": inst[0]["account"] if inst else None,
            "groups": ordered, "instances": len(inst)}


# ── per-instance detail (fetched on demand) ───────────────────────────────

SECRET_HINTS = ("secret", "token", "password", "passwd", "key", "credential", "apikey", "api_key", "client_secret")


def mask_config(payload):
    """Mask every field whose type or id looks like a secret, even if the API already did."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        return data
    fields = []
    for f in data.get("fields") or []:
        if not isinstance(f, dict):
            continue
        fid = (f.get("id") or "").lower()
        secret = (f.get("type") or "").lower() in ("secret", "password") or any(h in fid for h in SECRET_HINTS)
        value = f.get("value")
        fields.append({"id": f.get("id"), "label": f.get("label"), "type": f.get("type"),
                       "value": ("••••••••" if value not in (None, "", False) else value) if secret else value,
                       "secret": secret})
    return {"fields": fields, "instance_id": str(data.get("id") or ""), "instance_name": data.get("applicationInstanceName")}


def instance_log(client, instance_id, limit=50):
    data = client.request("GET", f"{APPS_PATH}/{instance_id}/log", params={"limit": limit})
    items = data.get("data") if isinstance(data, dict) else data
    out = []
    for e in (items or [])[:limit]:   # the API ignores ``limit`` on this endpoint (observed on Staging)
        if isinstance(e, dict):
            out.append({"created_at": e.get("createdAt"), "status": e.get("status"), "title": e.get("title"),
                        "http_status": e.get("httpStatus"), "message": (e.get("message") or "")[:1000], "trace_id": e.get("traceId")})
    return out


def instance_config(client, instance_id):
    return mask_config(client.request("GET", f"{APPS_PATH}/{instance_id}/config"))


# ── CSV ────────────────────────────────────────────────────────────────────

def to_csv(rows, columns):
    """rows: list of dicts; columns: list of (header, key). Lists are joined with ' | '."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in columns])
    for r in rows:
        line = []
        for _, k in columns:
            v = r.get(k)
            if isinstance(v, (list, set, tuple)):
                v = " | ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "yes" if v else "no"
            line.append("" if v is None else v)
        w.writerow(line)
    return buf.getvalue()
