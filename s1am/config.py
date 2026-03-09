import os
import sys
from getpass import getpass


class Config:
    def __init__(self):
        self.base_url = os.getenv("S1_BASE_URL", "").rstrip("/")
        self.api_token = os.getenv("S1_API_TOKEN", "")
        self.auth_prefix = os.getenv("S1_AUTH_PREFIX", "").strip()
        self.api_path = os.getenv("S1_API_PATH", "/web/api/v2.1").strip()
        self.timeout = int(os.getenv("S1_TIMEOUT", "30"))
        self.verify_ssl = os.getenv("S1_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
        self._prompt_missing_values()

    def _prompt_missing_values(self):
        if not sys.stdin.isatty():
            return
        if not self.base_url:
            self.base_url = input("Enter SentinelOne base URL (e.g. https://tenant.sentinelone.net): ").strip().rstrip("/")
        if not self.api_token:
            self.api_token = getpass("Enter SentinelOne API token (input hidden): ").strip()

    def validate(self):
        missing = []
        if not self.base_url:
            missing.append("S1_BASE_URL")
        if not self.api_token:
            missing.append("S1_API_TOKEN")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
