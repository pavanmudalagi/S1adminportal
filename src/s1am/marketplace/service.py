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
import re
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
    "account_retention", "site_retention",
]
GLOBAL_ACCOUNT_ID = "global"

# ── retention ───────────────────────────────────────────────────────────────
# Source (verified on Staging, 2026-11-13): GET /accounts and GET /sites return ``licenses.settings[]``; the entry with
# groupName "dv_retention" ("Deep Visibility Data Retention") holds the configured retention as text ("14 Days",
# "90 Days"). Sites carry their own effective entry — the API has no inheritance flag, so "inherited" is derived by
# comparing the site value with its account. Some sites have no entry at all → "not exposed by API". Data Ingest
# retention is only visible as licensed add-on modules ``xdr_<N>d_retention`` ("Data Ingest - Retention up to N days").
# The per-instance Marketplace config and the account/site policy objects carry no retention field.
RETENTION_GROUP = "dv_retention"
RETENTION_SOURCE = "GET /accounts|/sites → licenses.settings[groupName=dv_retention]"
NOT_EXPOSED = "not exposed by API"
_XDR_RET = re.compile(r"^xdr_(\d+)d_retention$")
_PERIOD = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(day|week|month|year)s?\s*$", re.I)


def parse_period(text):
    """'14 Days' → (14, 'days'); '1 Year' → (365, 'years'). Days are normalised for sorting; unit is what the API said."""
    m = _PERIOD.match(str(text or ""))
    if not m:
        return None, None
    n, unit = float(m.group(1)), m.group(2).lower()
    days = n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]
    return int(days) if days == int(days) else days, unit + "s"


def parse_retention(obj, level):
    """Retention record for an account or site object. ``status`` is ok | not_exposed."""
    lic = (obj or {}).get("licenses") or {}
    entry = next((s for s in lic.get("settings") or [] if (s.get("groupName") or s.get("settingGroup")) == RETENTION_GROUP), None)
    ingest = sorted({int(m.group(1)) for mod in lic.get("modules") or [] for m in [_XDR_RET.match(mod.get("name") or "")] if m})
    rec = {"level": level, "source": RETENTION_SOURCE if entry else None, "inherited": None, "ingest_days": ingest,
           "scope_id": str((obj or {}).get("id") or ""), "scope_name": (obj or {}).get("name")}
    if not entry:
        return {**rec, "value": None, "days": None, "unit": None, "status": "not_exposed", "display": NOT_EXPOSED}
    text = entry.get("setting") or entry.get("displayName")
    days, unit = parse_period(text)
    return {**rec, "value": text, "days": days, "unit": unit, "status": "ok", "display": text}


def derive_site_inheritance(site_ret, account_ret):
    """The API has no inherit flag: a site value equal to its account's is reported as inherited, a different one as an
    override. A site without an entry stays 'not exposed' and carries the account value for context."""
    out = dict(site_ret)
    out["account_value"] = account_ret.get("value") if account_ret else None
    if out["status"] != "ok" or not account_ret or account_ret.get("status") != "ok":
        out["inherited"] = None
        return out
    out["inherited"] = out["days"] == account_ret["days"]
    out["note"] = "same as account (API has no explicit inherit flag)" if out["inherited"] else f"overrides account ({account_ret['value']})"
    return out


def effective_retention(instance):
    """Site retention when the API exposes it, else the account's — what applies to this instance's data."""
    s, a = instance.get("site_retention"), instance.get("account_retention")
    if s and s.get("status") == "ok":
        return s
    return a


def retention_matches(rec, spec):
    """Filter spec: 'none' (not exposed), 'lt:30', 'le:30', 'eq:90', 'ge:180'."""
    if not spec:
        return True
    if spec == "none":
        return not rec or rec.get("status") != "ok"
    if not rec or rec.get("days") is None:
        return False
    op, _, n = spec.partition(":")
    try:
        n = float(n)
    except ValueError:
        return True
    d = rec["days"]
    return {"lt": d < n, "le": d <= n, "eq": d == n, "ge": d >= n, "gt": d > n}.get(op, True)


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
                "account_retention": None,
                "site_retention": None,
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
        try:
            attach_retention(client, result["instances"], sleep)
        except SentinelOneError as exc:
            # Connectors still show; retention cells say why they could not be read.
            result["retention_error"] = _classify(exc)[1]
            for i in result["instances"]:
                i["account_retention"] = i["account_retention"] or {"status": "not_exposed", "display": NOT_EXPOSED, "level": "account", "error": result["retention_error"]}
        result["ok"] = True
    except SentinelOneError as exc:
        result["error_kind"], result["error"] = _classify(exc)
    except Exception as exc:  # network, config, anything else
        result["error_kind"], result["error"] = "error", str(exc)[:300]
    return result


def _chunks(seq, n=50):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_scopes(client, account_ids, site_ids, sleep=time.sleep):
    """Account and site objects (with licenses) for the ids the connectors reference — not the whole console."""
    accounts, sites = {}, {}
    for chunk in _chunks([a for a in account_ids if a and a != GLOBAL_ACCOUNT_ID]):
        data = client.request("GET", "accounts", params={"ids": ",".join(chunk), "limit": 100})
        for a in (data.get("data") if isinstance(data, dict) else data) or []:
            accounts[str(a.get("id"))] = a
        sleep(CALL_DELAY)
    for chunk in _chunks([s for s in site_ids if s]):
        data = client.request("GET", "sites", params={"siteIds": ",".join(chunk), "limit": 100})
        body = data.get("data") if isinstance(data, dict) else data
        for s in (body.get("sites") if isinstance(body, dict) else body) or []:
            sites[str(s.get("id"))] = s
        sleep(CALL_DELAY)
    return accounts, sites


def attach_retention(client, instances, sleep=time.sleep):
    accounts, sites = fetch_scopes(client, {i["account_id"] for i in instances}, {i["site_id"] for i in instances if i["site_id"]}, sleep)
    acc_ret = {aid: parse_retention(a, "account") for aid, a in accounts.items()}
    site_ret = {sid: parse_retention(s, "site") for sid, s in sites.items()}
    for i in instances:
        a = acc_ret.get(i["account_id"])
        if a is None and i["account_id"] and i["account_id"] != GLOBAL_ACCOUNT_ID:
            a = {"level": "account", "status": "not_exposed", "display": NOT_EXPOSED, "value": None, "days": None, "unit": None,
                 "inherited": None, "ingest_days": [], "source": None, "note": "account not returned by GET /accounts"}
        i["account_retention"] = a
        if i["site_id"]:
            s = site_ret.get(i["site_id"]) or {"level": "site", "status": "not_exposed", "display": NOT_EXPOSED, "value": None, "days": None,
                                                 "unit": None, "inherited": None, "ingest_days": [], "source": None, "note": "site not returned by GET /sites"}
            i["site_retention"] = derive_site_inheritance(s, a)
        else:
            i["site_retention"] = None
    return acc_ret, site_ret


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
                                 "last_installed_at": None, "last_entity_created_at": None, "site_level": 0, "account_level": 0,
                                 "retention": None})
        if r["retention"] is None and i.get("account_retention"):
            r["retention"] = i["account_retention"]
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
        ret = r["retention"] or {}
        r["retention_display"] = ret.get("display") or (NOT_EXPOSED if r["account_id"] != GLOBAL_ACCOUNT_ID else "n/a (console-wide)")
        r["retention_days"] = ret.get("days")
        r["ingest_retention_days"] = ret.get("ingest_days") or []
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
        g = groups.setdefault(key, {"site": i["site"], "site_id": i["site_id"], "level": "site" if i["site"] else "account", "instances": [],
                                    "retention": i.get("site_retention") if i["site"] else i.get("account_retention")})
        g["instances"].append(i)
    ordered = sorted(groups.values(), key=lambda g: (g["level"] != "account", (g["site"] or "").lower()))
    for g in ordered:
        g["instances"].sort(key=lambda i: (i["app_name"].lower(), i["instance_name"].lower()))
    return {"console": console, "account_id": str(account_id), "account": inst[0]["account"] if inst else None,
            "retention": inst[0].get("account_retention") if inst else None, "groups": ordered, "instances": len(inst)}


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
    """rows: list of dicts; columns: list of (header, key) — a key may be dotted (retention.display). Lists → ' | '."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in columns])
    for r in rows:
        line = []
        for _, k in columns:
            v = r
            for part in k.split("."):
                v = v.get(part) if isinstance(v, dict) else None
            if isinstance(v, (list, set, tuple)):
                v = " | ".join(str(x) for x in v)
            elif isinstance(v, bool):
                v = "yes" if v else "no"
            line.append("" if v is None else v)
        w.writerow(line)
    return buf.getvalue()
