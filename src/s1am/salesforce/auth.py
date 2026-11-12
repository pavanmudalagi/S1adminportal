"""Salesforce sign-in: OAuth 2.0 authorization code + PKCE through the browser.

Ported from the product-health-dashboard (data/auth.py, Salesforce half; the Snowflake
and Streamlit parts are left out). The flow defaults to Salesforce's well-known
``PlatformCLI`` public client, whose ``http://localhost:1717/OauthRedirect`` callback is
already registered, so nothing needs creating up front. Orgs that allowlist connected
apps can point client_id / redirect_uri at their own Connected App.

Settings — an environment variable wins over the [salesforce] section of
~/.config/s1am/config, which wins over the default:

  SF_CLIENT_ID       client_id       PlatformCLI
  SF_REDIRECT_URI    redirect_uri    http://localhost:1717/OauthRedirect
  SF_LOGIN_URL       login_url       https://login.salesforce.com  (or the My Domain host)
  SF_CLIENT_SECRET   client_secret   only for a Connected App that requires one
  SF_PROMPT          prompt          login  (always ask which account; "" reuses the browser session)
  SF_TOKEN_FILE      token_file      ~/.config/s1am/sf_tokens.json

Tokens are cached in the token file (mode 0600) so restarts do not re-prompt. The file has
the same shape as the dashboard's ~/.product-health-dashboard/tokens.json, and when s1am
has no token file yet but the dashboard does, its Salesforce entry is imported once, so a
single sign-in serves both apps. Nothing here is ever logged.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from ..api.config import Config

DEFAULTS = {
    "client_id":     "PlatformCLI",
    "redirect_uri":  "http://localhost:1717/OauthRedirect",
    "login_url":     "https://login.salesforce.com",
    "client_secret": "",
    "prompt":        "login",
    "token_file":    str(Path.home() / ".config" / "s1am" / "sf_tokens.json"),
}
OAUTH_SCOPES = "api refresh_token"
CALLBACK_TIMEOUT_SECONDS = 180
DASHBOARD_TOKEN_FILE = Path(os.getenv("PHD_TOKEN_DIR", str(Path.home() / ".product-health-dashboard"))) / "tokens.json"


def setting(key):
    """SF_<KEY> from the environment, else [salesforce] <key> from the config file, else the default."""
    value = os.getenv("SF_" + key.upper())
    if value is None:
        value = Config.section("salesforce").get(key)
    if value is None:
        value = DEFAULTS.get(key, "")
    return value.strip() if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

def token_file():
    return Path(os.path.expanduser(setting("token_file")))


def _load_all():
    path = token_file()
    if not path.exists():
        _import_dashboard_token(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        # A corrupt cache should never take the app down -- re-auth instead.
        return {}


def _import_dashboard_token(path):
    """One-time copy of the dashboard's cached Salesforce session, if it has one."""
    try:
        if not DASHBOARD_TOKEN_FILE.exists():
            return
        data = json.loads(DASHBOARD_TOKEN_FILE.read_text(encoding="utf-8"))
        entry = data.get("salesforce") if isinstance(data, dict) else None
        if entry and entry.get("refresh_token"):
            _save_all({"salesforce": entry}, path)
    except (ValueError, OSError):
        return


def _save_all(data, path=None):
    path = path or token_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Write via a private temp file so the token is never briefly world-readable.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def save_token(payload):
    data = _load_all()
    data["salesforce"] = payload
    _save_all(data)


def load_token():
    return _load_all().get("salesforce")


def clear_token():
    """Sign out. Leaves an empty file behind so the dashboard's session is not re-imported."""
    _save_all({})


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@dataclass
class ConnectionStatus:
    connected: bool
    detail: str = ""
    identity: str = ""

    def as_dict(self):
        return {"connected": self.connected, "detail": self.detail, "identity": self.identity}


def status():
    """Report whether credentials are available. Never performs a login."""
    cached = load_token()
    if cached and cached.get("access_token") and cached.get("instance_url"):
        return ConnectionStatus(True, "OAuth session cached", _identity_from(cached.get("id", "")))
    if os.getenv("SF_USERNAME") and os.getenv("SF_PASSWORD"):
        return ConnectionStatus(True, "Using SF_USERNAME / SF_PASSWORD from the environment")
    return ConnectionStatus(False, "Not signed in — run `s1am sf-check --login`")


# ---------------------------------------------------------------------------
# OAuth (authorization code + PKCE)
# ---------------------------------------------------------------------------

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_pkce():
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        expected = urlparse(self.server.redirect_uri).path
        if parsed.path != expected:
            self.send_response(404)
            self.end_headers()
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.server.result = params

        ok = "code" in params
        body = (
            "<html><body style=\"font-family:system-ui;padding:3rem\">"
            "<h2>{}</h2><p>{}</p></body></html>"
        ).format(
            "Signed in" if ok else "Sign-in failed",
            "You can close this tab and return to s1am."
            if ok
            else "Error: {}".format(params.get("error_description", params.get("error", "unknown"))),
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging (may contain the code)
        return


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def login_interactive(timeout=CALLBACK_TIMEOUT_SECONDS, open_browser=webbrowser.open):
    """Run the browser SSO flow and cache the resulting tokens.

    Blocks until the browser redirects back or `timeout` elapses. `open_browser` is
    called with the authorize URL (swap it to print the URL instead of launching a browser).
    """
    client_id, redirect_uri, login_url = setting("client_id"), setting("redirect_uri"), setting("login_url").rstrip("/")
    port = urlparse(redirect_uri).port or 80

    if not _port_free(port):
        return ConnectionStatus(False, f"Port {port} is already in use, so the OAuth callback cannot be "
                                       "received. Close whatever is listening on it and retry.")

    verifier, challenge = _make_pkce()
    state = _b64url(secrets.token_bytes(16))
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    # "login" always asks which account to use. Without it Salesforce silently reuses
    # whatever session the browser already has, which can sign you in as the wrong user.
    if setting("prompt"):
        params["prompt"] = setting("prompt")
    authorize_url = f"{login_url}/services/oauth2/authorize?{urlencode(params)}"

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1
    server.result = None
    server.redirect_uri = redirect_uri

    def serve():
        deadline = time.time() + timeout
        while server.result is None and time.time() < deadline:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    open_browser(authorize_url)
    thread.join(timeout + 2)

    result = server.result
    server.server_close()

    if not result:
        return ConnectionStatus(False, f"Timed out after {timeout}s waiting for the browser redirect.")
    if result.get("state") != state:
        # Mismatched state means the response is not from the request we made.
        return ConnectionStatus(False, "OAuth state mismatch — sign-in rejected.")
    if "code" not in result:
        return ConnectionStatus(False, "Authorization denied: {}".format(
            result.get("error_description", result.get("error", "unknown"))))
    return _exchange_code(result["code"], verifier)


def _token_request(payload):
    secret = setting("client_secret")
    if secret:
        payload["client_secret"] = secret
    return requests.post(f"{setting('login_url').rstrip('/')}/services/oauth2/token", data=payload, timeout=30)


def _exchange_code(code, verifier):
    response = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": setting("client_id"),
        "redirect_uri": setting("redirect_uri"),
        "code_verifier": verifier,
    })
    if response.status_code != 200:
        return ConnectionStatus(False, f"Token exchange failed ({response.status_code}): {_safe_error(response)}")

    body = response.json()
    save_token({
        "access_token":  body.get("access_token"),
        "refresh_token": body.get("refresh_token"),
        "instance_url":  body.get("instance_url"),
        "id":            body.get("id"),
        "issued_at":     body.get("issued_at"),
    })
    return ConnectionStatus(True, "Signed in via OAuth", _identity_from(body.get("id", "")))


def _safe_error(response):
    """Error text without echoing anything token-shaped."""
    try:
        data = response.json()
        return str(data.get("error_description") or data.get("error") or "unknown")
    except ValueError:
        return "unparseable response"


def _identity_from(id_url):
    # The identity URL ends in /<org id>/<user id>.
    return id_url.rsplit("/", 1)[-1] if id_url else ""


last_refresh_error = ""


def refresh():
    """Exchange the cached refresh token for a fresh access token. Returns the session or None."""
    global last_refresh_error
    cached = load_token()
    if not cached or not cached.get("refresh_token"):
        last_refresh_error = "no refresh token cached"
        return None
    response = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": cached["refresh_token"],
        "client_id": setting("client_id"),
    })
    if response.status_code != 200:
        last_refresh_error = f"{response.status_code}: {_safe_error(response)}"
        return None
    last_refresh_error = ""
    body = response.json()
    cached["access_token"] = body.get("access_token", cached.get("access_token"))
    if body.get("instance_url"):
        cached["instance_url"] = body["instance_url"]
    save_token(cached)
    return cached


def session():
    """Cached Salesforce session, refreshed if the access token is missing."""
    cached = load_token()
    if not cached:
        return None
    if cached.get("access_token") and cached.get("instance_url"):
        return cached
    return refresh()
