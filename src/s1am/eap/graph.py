"""Download the Forms workbook from OneDrive/SharePoint through Microsoft Graph.

The workbook lives in a colleague's OneDrive and is shared by link, so it is fetched as
GET /shares/{sharing-token}/driveItem/content (read-only). Auth is MSAL device-code with
delegated Files.Read.All: the user is shown a URL + code once, and the refresh token is
cached under ~/.config/s1am/ (mode 0600) so later syncs are silent.

The default client ID is Microsoft's public "Microsoft Graph Command Line Tools" app, which
already allows public-client (device-code) flows in most tenants. Override it with the
[eap] graph_client_id config key if the organisation blocks it.
"""
import base64
import json
import os
from pathlib import Path

import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
DEFAULT_TENANT = "organizations"
SCOPES = ["Files.Read.All"]
TOKEN_CACHE_FILE = Path.home() / ".config" / "s1am" / "msgraph_token_cache.json"


def share_token(link):
    """Encode a sharing URL the way /shares/{token} expects (base64url, no padding, 'u!' prefix)."""
    return "u!" + base64.urlsafe_b64encode(link.strip().encode("utf-8")).decode("ascii").rstrip("=")


def _msal_app(client_id=None, tenant=None):
    import msal  # lazy: only Graph mode needs it
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_FILE.exists():
        cache.deserialize(TOKEN_CACHE_FILE.read_text())
    app = msal.PublicClientApplication(
        client_id or DEFAULT_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{tenant or DEFAULT_TENANT}",
        token_cache=cache,
    )
    return app, cache


def _save_cache(cache):
    if not cache.has_state_changed:
        return
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_FILE.write_text(cache.serialize())
    os.chmod(TOKEN_CACHE_FILE, 0o600)


def acquire_token(client_id=None, tenant=None, interactive=True, on_device_code=print):
    """Return a Graph access token, silently from the cache when possible.

    With `interactive`, falls back to the device-code flow and passes Microsoft's
    "go to … and enter code …" message to `on_device_code`. Raises RuntimeError otherwise.
    """
    app, cache = _msal_app(client_id, tenant)
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result and interactive:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError("Could not start device-code sign-in: "
                               + str(flow.get("error_description") or flow.get("error") or flow))
        on_device_code(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (result or {}).get("error") or "not signed in"
        raise RuntimeError(f"Microsoft Graph sign-in failed: {detail}")
    return result["access_token"]


def signed_in_account():
    """Username of the cached Graph account, or None. Never prompts."""
    try:
        app, _ = _msal_app()
    except ImportError:
        return None
    accounts = app.get_accounts()
    return accounts[0].get("username") if accounts else None


def sign_out():
    if TOKEN_CACHE_FILE.exists():
        TOKEN_CACHE_FILE.unlink()


def shared_item(link, token):
    """Metadata (name, size, lastModifiedDateTime, …) of the item behind a sharing link."""
    resp = requests.get(f"{GRAPH_BASE}/shares/{share_token(link)}/driveItem",
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    _raise_for_graph(resp)
    return resp.json()


def download_shared_file(link, dest, token):
    """Download the shared workbook to `dest` (a Path). Returns the item metadata."""
    meta = shared_item(link, token)
    resp = requests.get(f"{GRAPH_BASE}/shares/{share_token(link)}/driveItem/content",
                        headers={"Authorization": f"Bearer {token}"}, timeout=120,
                        allow_redirects=True)
    _raise_for_graph(resp)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    return meta


def _raise_for_graph(resp):
    if resp.ok:
        return
    try:
        err = resp.json().get("error") or {}
        detail = f"{err.get('code')}: {err.get('message')}"
    except (ValueError, AttributeError):
        detail = resp.text[:300]
    raise RuntimeError(f"Microsoft Graph {resp.status_code} for {resp.url.split('?')[0]} — {detail}")


def _token_cache_summary():
    """For status displays: which account is cached, without exposing tokens."""
    if not TOKEN_CACHE_FILE.exists():
        return {"signed_in": False}
    try:
        data = json.loads(TOKEN_CACHE_FILE.read_text())
        accounts = list((data.get("Account") or {}).values())
        return {"signed_in": bool(accounts), "username": accounts[0].get("username") if accounts else None}
    except (ValueError, AttributeError, IndexError):
        return {"signed_in": False}
