"""Read-only Salesforce SOQL access on top of simple-salesforce.

Ported from the dashboard's data/salesforce.py. Differences: the Streamlit caches are a
module-level client plus a small TTL dict, rows come back as plain dicts (one level of
relationship traversal flattened, e.g. "Account__r.Name"), and simple-salesforce is
imported lazily so the rest of s1am runs without it. Only SELECT statements are issued.
"""
import os
import time

from . import auth

CACHE_TTL = 3600
# Values per IN (...) list. Keeps each statement far below SOQL's limits (100k characters,
# and a few thousand values before the query planner starts timing out).
IN_CHUNK = 200


class SalesforceConfigError(RuntimeError):
    pass


_client = None
_cache = {}


def client():
    """Authenticated simple-salesforce client.

    Prefers the cached OAuth/SSO session from `auth`; falls back to username/password/token
    from the environment so an org that blocks the OAuth client can still be used.
    """
    global _client
    if _client is not None:
        return _client
    try:
        from simple_salesforce import Salesforce
    except ImportError as exc:
        raise SalesforceConfigError("simple-salesforce is not installed (pip install simple-salesforce)") from exc

    sess = auth.session()
    if sess and sess.get("access_token") and sess.get("instance_url"):
        _client = Salesforce(session_id=sess["access_token"], instance_url=sess["instance_url"])
        return _client

    username = os.getenv("SF_USERNAME")
    password = os.getenv("SF_PASSWORD")
    token = os.getenv("SF_SECURITY_TOKEN")
    if username and password and token:
        _client = Salesforce(username=username, password=password, security_token=token,
                             domain=os.getenv("SF_DOMAIN", "login"))
        return _client

    raise SalesforceConfigError(
        "Not signed in to Salesforce. Run `s1am sf-check --login` (browser SSO), "
        "or set SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN."
    )


def reset_client():
    """Drop the cached client so the next call re-authenticates."""
    global _client
    _client = None


def clear_cache():
    _cache.clear()


def is_connected():
    """True if credentials are already available. Never performs a login."""
    return auth.status().connected


def whoami():
    """Identity of the authenticated user, for status displays. {} on failure."""
    try:
        me = client().restful("chatter/users/me", {})
        return {"username": me.get("username"), "display_name": me.get("displayName"), "id": me.get("id")}
    except Exception:
        return {}


def _looks_like_auth_error(exc):
    text = str(exc).upper()
    return "INVALID_SESSION" in text or "401" in text or "EXPIRED" in text


def _flatten(record):
    row = {}
    for key, value in record.items():
        if key == "attributes":
            continue
        if isinstance(value, dict):
            # Flatten one level of relationship traversal (Account__r.Name).
            row.update({f"{key}.{k}": v for k, v in value.items() if k != "attributes"})
        else:
            row[key] = value
    return row


def run_soql(query, use_cache=True):
    """Execute a SOQL SELECT and return a list of flattened row dicts."""
    query = " ".join(query.split())
    if not query.lower().startswith("select"):
        raise ValueError("Only SELECT queries are permitted.")
    if use_cache:
        hit = _cache.get(query)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return list(hit[1])

    try:
        records = client().query_all(query)["records"]
    except Exception as exc:
        # An expired access token is recoverable: refresh once and retry.
        if _looks_like_auth_error(exc) and auth.refresh():
            reset_client()
            records = client().query_all(query)["records"]
        else:
            raise
    rows = [_flatten(r) for r in records]
    if use_cache:
        _cache[query] = (time.time(), rows)
    return list(rows)


# --------------------------------------------------------------------------
# SOQL helpers
# --------------------------------------------------------------------------

def soql_quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def soql_in_list(values):
    """Render values as a quoted SOQL IN (...) body."""
    return ", ".join(soql_quote(v) for v in values)


def chunked(values, size=IN_CHUNK):
    values = list(values)
    return [values[i:i + size] for i in range(0, len(values), size)]


def query_in(template, values, size=IN_CHUNK, use_cache=True):
    """Run `template` once per chunk of `values`, substituting {values} with a quoted IN list.

    Values are de-duplicated (order kept) and blanks dropped; rows from all chunks are
    concatenated. One statement per rule across every pending request, never per row.
    """
    rows = []
    unique = [v for v in dict.fromkeys(values) if v not in (None, "")]
    for chunk in chunked(unique, size):
        rows += run_soql(template.format(values=soql_in_list(chunk)), use_cache=use_cache)
    return rows


def describe_fields(sobject):
    """Set of field API names on an sObject (for checking a query before it is written)."""
    desc = getattr(client(), sobject).describe()
    return {f["name"] for f in desc.get("fields", [])}
