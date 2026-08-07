import configparser
import os
import sys
from getpass import getpass
from pathlib import Path

_CONFIG_FILE = Path.home() / ".config" / "s1am" / "config"


class Config:
    def __init__(self, profile=None):
        self.base_url          = os.getenv("S1_BASE_URL", "").rstrip("/")
        self.api_token         = os.getenv("S1_API_TOKEN", "")
        self.powerquery_token  = os.getenv("S1_POWERQUERY_TOKEN", "")
        self.auth_prefix       = os.getenv("S1_AUTH_PREFIX", "").strip()
        self.api_path          = os.getenv("S1_API_PATH", "/web/api/v2.1").strip()
        self.timeout           = int(os.getenv("S1_TIMEOUT", "30"))
        self.verify_ssl        = os.getenv("S1_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}

        if profile:
            self._load_section(profile, required=True)
        else:
            self._load_section("default", required=False)

        self._prompt_missing_values()

    def _load_section(self, section, required):
        cfg_path = Path(str(_CONFIG_FILE))
        if not cfg_path.exists():
            if required:
                raise ValueError(f"Profile '{section}' not found in config file (file does not exist).")
            return
        parser = configparser.ConfigParser()
        parser.read(str(cfg_path))
        if not parser.has_section(section):
            if required:
                raise ValueError(f"Profile '{section}' not found in config file.")
            return
        # File values fill in only what env vars left empty
        if not self.base_url         and parser.has_option(section, "s1_base_url"):
            self.base_url         = parser.get(section, "s1_base_url").rstrip("/")
        if not self.api_token        and parser.has_option(section, "s1_api_token"):
            self.api_token        = parser.get(section, "s1_api_token")
        if not self.powerquery_token and parser.has_option(section, "s1_powerquery_token"):
            self.powerquery_token = parser.get(section, "s1_powerquery_token")
        if not self.auth_prefix      and parser.has_option(section, "s1_auth_prefix"):
            self.auth_prefix      = parser.get(section, "s1_auth_prefix").strip()

    @classmethod
    def list_profiles(cls):
        cfg_path = Path(str(_CONFIG_FILE))
        if not cfg_path.exists():
            return []
        parser = configparser.ConfigParser()
        parser.read(str(cfg_path))
        return list(parser.sections())

    @classmethod
    def save_profile(cls, name, base_url="", api_token="", pq_token="", auth_prefix=""):
        cfg_path = Path(str(_CONFIG_FILE))
        parser = configparser.ConfigParser()
        if cfg_path.exists():
            parser.read(str(cfg_path))
        if not parser.has_section(name):
            parser.add_section(name)
        if base_url:    parser.set(name, "s1_base_url",          base_url)
        if api_token:   parser.set(name, "s1_api_token",         api_token)
        if pq_token:    parser.set(name, "s1_powerquery_token",  pq_token)
        if auth_prefix: parser.set(name, "s1_auth_prefix",       auth_prefix)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(cfg_path), "w") as f:
            parser.write(f)

    def _prompt_missing_values(self):
        if not sys.stdin.isatty():
            return
        if not self.base_url:
            self.base_url = input("Enter SentinelOne base URL (e.g. https://tenant.sentinelone.net): ").strip().rstrip("/")
        if not self.api_token:
            self.api_token = getpass("Enter SentinelOne MGMT API token (input hidden): ").strip()

    def validate(self, require_mgmt=True, require_powerquery=False):
        missing = []
        if not self.base_url:
            missing.append("S1_BASE_URL")
        if require_mgmt and not self.api_token:
            missing.append("S1_API_TOKEN")
        if require_powerquery and not self.powerquery_token:
            missing.append("S1_POWERQUERY_TOKEN")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    def as_display_dict(self):
        def mask(val):
            if not val:
                return "(not set)"
            return val[:4] + "****" if len(val) > 4 else "****"
        return {
            "S1_BASE_URL":         self.base_url or "(not set)",
            "S1_API_TOKEN":        mask(self.api_token),
            "S1_POWERQUERY_TOKEN": mask(self.powerquery_token) if self.powerquery_token else "(not set)",
            "S1_AUTH_PREFIX":      self.auth_prefix or "(not set)",
            "S1_API_PATH":         self.api_path,
            "S1_TIMEOUT":          str(self.timeout),
            "S1_VERIFY_SSL":       str(self.verify_ssl),
        }
