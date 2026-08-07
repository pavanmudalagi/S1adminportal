import json
from urllib.parse import urljoin

import requests

from .exceptions import SentinelOneError


class SentinelOneClient:
    def __init__(
        self,
        base_url,
        api_token,
        api_path="/web/api/v2.1",
        timeout=30,
        verify=True,
        auth_prefix="",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.api_path = api_path if api_path.startswith("/") else f"/{api_path}"
        self.timeout = timeout
        self.verify = verify
        self.auth_prefix = auth_prefix.strip()

    def _build_url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path
        path = path.lstrip("/")
        base = f"{self.base_url}{self.api_path}/"
        return urljoin(base, path)

    def _headers(self, has_json=False):
        token = self.api_token
        if self.auth_prefix:
            token = f"{self.auth_prefix} {self.api_token}"
        headers = {"Authorization": token, "Accept": "application/json"}
        if has_json:
            headers["Content-Type"] = "application/json"
        return headers

    def request(self, method, path, params=None, payload=None):
        url = self._build_url(path)
        has_json = payload is not None
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self._headers(has_json=has_json),
                params=params,
                json=payload,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise SentinelOneError(str(exc)) from exc

        if not response.ok:
            message = f"HTTP {response.status_code} for {method} {url}"
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            raise SentinelOneError(message, status_code=response.status_code, payload=payload)

        if response.content:
            try:
                return response.json()
            except ValueError:
                return response.text
        return None

    def paginate(self, path, params=None, page_limit=None):
        params = dict(params or {})
        results = []
        pages = 0
        while True:
            data = self.request("GET", path, params=params)
            results.append(data)
            pages += 1
            if page_limit and pages >= page_limit:
                break
            next_cursor = None
            if isinstance(data, dict):
                pagination = data.get("pagination") or {}
                next_cursor = pagination.get("nextCursor") or pagination.get("next_cursor")
            if not next_cursor:
                break
            params["cursor"] = next_cursor
        return results

    @staticmethod
    def load_json_payload(value):
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON payload: {exc}") from exc
