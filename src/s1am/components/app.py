"""Flask web application providing a browser UI for s1am."""
import concurrent.futures
import datetime
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent  # repo root


def _persistent_secret_key() -> bytes:
    """Return a stable secret key, creating and saving it on first run."""
    if env_key := os.environ.get("S1AM_SECRET_KEY"):
        return env_key.encode() if isinstance(env_key, str) else env_key
    key_path = Path.home() / ".config" / "s1am" / "web_secret.key"
    if key_path.exists():
        return key_path.read_bytes()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    key_path.write_bytes(key)
    key_path.chmod(0o600)  # owner read/write only
    return key


app = Flask(__name__, template_folder="templates")
app.secret_key = _persistent_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

_log = logging.getLogger(__name__)

from s1am import __version__ as _VERSION
from s1am import account_fields, licenses


# ─── Field Helpers ────────────────────────────────────────────────────────────

def _out_fields(include_columns=True):
    f = [
        {"id": "format", "label": "Display As", "type": "select",
         "options": ["table", "json", "csv"], "default": "table"},
    ]
    if include_columns:
        f.append({"id": "columns", "label": "Columns (comma-separated)", "type": "text",
                  "placeholder": "e.g. id,name,createdAt", "optional": True})
    return f


def _list_fields(search_label="Search", search_ph="Filter by name"):
    return [
        {"id": "search", "label": search_label, "type": "text",
         "placeholder": search_ph, "optional": True},
        {"id": "limit", "label": "Limit", "type": "number",
         "placeholder": "Max items per page", "optional": True},
        {"id": "all_pages", "label": "Fetch all pages", "type": "checkbox"},
    ] + _out_fields()


def _report_fields():
    return [
        {"id": "account_id", "label": "Account ID", "type": "text",
         "placeholder": "Required unless All Accounts"},
        {"id": "account_name", "label": "Account Name", "type": "text",
         "placeholder": "Optional filter", "optional": True},
        {"id": "site_id", "label": "Site ID", "type": "text",
         "placeholder": "Optional", "optional": True},
        {"id": "site_name", "label": "Site Name", "type": "text",
         "placeholder": "Optional", "optional": True},
        {"id": "start_time", "label": "Start Time", "type": "text",
         "placeholder": "e.g. 31d", "default": "31d"},
        {"id": "end_time", "label": "End Time", "type": "text",
         "placeholder": "Optional", "optional": True},
        {"id": "all_accounts", "label": "All Accounts", "type": "checkbox"},
        {"id": "compare_previous", "label": "Compare Previous Period", "type": "checkbox"},
    ] + _out_fields()


# ─── CLI Arg Builders ─────────────────────────────────────────────────────────

def _join_lines(text):
    """Join newline/comma-separated entries with commas (keeps spaces inside display names)."""
    return ",".join(p.strip() for p in re.split(r"[,\n]+", text) if p.strip())


def _out_args(f):
    # Always fetch JSON from the CLI; the browser handles table/csv rendering.
    # --columns is still forwarded because it filters the API response server-side.
    args = []
    if f.get("columns"):
        args += ["--columns", f["columns"]]
    return args


def _list_args(cmd_parts, f):
    args = list(cmd_parts)
    if f.get("search"):
        args += ["--search", f["search"]]
    if f.get("limit"):
        args += ["--limit", str(f["limit"])]
    if f.get("all_pages"):
        args += ["--all"]
    args += _out_args(f)
    return args


def _report_args(cmd_parts, f):
    args = list(cmd_parts)
    if f.get("account_id"):
        args += ["--account-id", f["account_id"]]
    if f.get("account_name"):
        args += ["--account-name", f["account_name"]]
    if f.get("site_id"):
        args += ["--site-id", f["site_id"]]
    if f.get("site_name"):
        args += ["--site-name", f["site_name"]]
    args += ["--start-time", f.get("start_time") or "31d"]
    if f.get("end_time"):
        args += ["--end-time", f["end_time"]]
    if f.get("all_accounts"):
        args += ["--all-accounts"]
    if f.get("compare_previous"):
        args += ["--compare-previous"]
    args += _out_args(f)
    return args


# ─── Command Definitions ──────────────────────────────────────────────────────

COMMANDS = [
    # Accounts
    {
        "id": "list-accounts",
        "category": "Accounts",
        "icon": "🏢",
        "label": "List Accounts",
        "description": "List all accounts with optional filtering and pagination.",
        "requires": "mgmt",
        "fields": _list_fields("Search by Name"),
        "build_args": lambda f: _list_args(["list-accounts"], f),
    },
    {
        "id": "get-account",
        "category": "Accounts",
        "icon": "🏢",
        "label": "Get Account",
        "description": "Retrieve a single account by ID.",
        "requires": "mgmt",
        "fields": [
            {"id": "account_id", "label": "Account ID", "type": "text", "placeholder": "Required"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: ["get-account", f["account_id"]] + _out_args(f),
    },
    {
        "id": "account-policy",
        "category": "Accounts",
        "icon": "🏢",
        "label": "Account Policy",
        "description": "Get or update an account's security policy.",
        "requires": "mgmt",
        "fields": [
            {"id": "account_id", "label": "Account ID", "type": "text", "placeholder": "Required"},
            {"id": "action", "label": "Action", "type": "select",
             "options": ["get", "update"], "default": "get"},
            {"id": "payload", "label": "JSON Payload (update only)", "type": "textarea",
             "placeholder": '{"policy": {...}}', "optional": True},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["account-policy", f["account_id"], f.get("action") or "get"]
            + (["--payload", f["payload"]] if f.get("payload") else [])
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "lookup-account-id",
        "category": "Accounts",
        "icon": "🔍",
        "label": "Lookup Account ID",
        "description": (
            "Enter an account name to search by name, or paste a Salesforce tenant "
            "(e.g. 1000_USEA1_2498094131181849447) to fetch the account name, SKUs, and modules directly."
        ),
        "requires": "mgmt",
        "fields": [
            {"id": "account_name", "label": "Account Name or Salesforce Tenant", "type": "text",
             "placeholder": "e.g. Acme Corp  or  1000_USEA1_2498094131181849447"},
            {"id": "exact", "label": "Exact match (name search only)", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["lookup-account-id", f["account_name"]]
            + (["--exact"] if f.get("exact") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "edit-account",
        "category": "Accounts",
        "icon": "🖊️",
        "label": "Edit Account",
        "description": "Interactively edit an account's name, SKUs (with surface counts), add-on modules, and platform settings using a Salesforce tenant name.",
        "requires": "mgmt",
        "type": "account-editor",
        "fields": [],
        "build_args": lambda f: [],
    },
    {
        "id": "eap-tracker",
        "category": "Prompt EAP",
        "icon": "🧭",
        "label": "EAP Request Tracker",
        "description": (
            "Prompt Security early-access requests from the Microsoft Forms workbook, enriched from "
            "Salesforce (account, country, contract tenant, agents) and cross-checked on the SentinelOne "
            "console (is Prompt enabled, tenant name, first users)."
        ),
        "requires": "none",
        "type": "eap-tracker",
        "fields": [],
        "build_args": lambda f: [],
    },
    {
        "id": "update-account",
        "category": "Accounts",
        "icon": "✏️",
        "label": "Update Account",
        "description": (
            "Update an account's name, SKUs, add-ons and Deep Visibility retention using its Salesforce tenant name. "
            "The API call is routed to the console matching the tenant's console code. "
            "Changes are additive/subtractive: SKUs, add-ons and settings you don't mention are preserved. "
            "Identity Security needs Complete or Control; Wayfinder MDR Essentials needs "
            "Purple AI Foundations and Wayfinder Threat Hunting."
        ),
        "requires": "mgmt",
        "fields": [
            {
                "id": "tenant",
                "label": "Salesforce Tenant",
                "type": "text",
                "placeholder": "e.g. 1000_USEA1_2498094131181849447",
            },
            {
                "id": "name",
                "label": "New Account Name",
                "type": "text",
                "placeholder": "Leave blank to keep current name",
                "optional": True,
            },
            {
                "id": "add_skus",
                "label": "SKUs to Add / Change Count",
                "type": "textarea",
                "placeholder": "unified_identity=500\nhyperautomation=unlimited\n(one per line or comma-separated: "
                               + ", ".join(s["name"] for s in licenses.SKUS) + ")",
                "optional": True,
            },
            {
                "id": "remove_skus",
                "label": "SKUs to Remove",
                "type": "textarea",
                "placeholder": "SKU_NAME\n(one per line or comma-separated — leave blank to make no change)",
                "optional": True,
            },
            {
                "id": "add_modules",
                "label": "Add-ons to Add",
                "type": "textarea",
                "placeholder": "purple_ai\nwayfinder_threat_hunting\n(one per line or comma-separated: "
                               + ", ".join(a["name"] for a in licenses.ADDONS) + ")",
                "optional": True,
            },
            {
                "id": "remove_modules",
                "label": "Add-ons to Remove",
                "type": "textarea",
                "placeholder": "ADDON_NAME\n(one per line or comma-separated — leave blank to make no change)",
                "optional": True,
            },
            {
                "id": "dv_retention",
                "label": "Deep Visibility Data Retention (blank = keep current)",
                "type": "select",
                "options": [""] + licenses.DV_RETENTION_OPTIONS,
                "default": "",
                "optional": True,
            },
            {"id": "dry_run", "label": "Dry Run (validate and show payload only)", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["update-account", f["tenant"]]
            + (["--name", f["name"]] if f.get("name") else [])
            + (["--add-skus", _join_lines(f["add_skus"])] if f.get("add_skus") else [])
            + (["--remove-skus", _join_lines(f["remove_skus"])] if f.get("remove_skus") else [])
            + (["--add-modules", _join_lines(f["add_modules"])] if f.get("add_modules") else [])
            + (["--remove-modules", _join_lines(f["remove_modules"])] if f.get("remove_modules") else [])
            + (["--dv-retention", f["dv_retention"]] if f.get("dv_retention") else [])
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "list-console-modules",
        "category": "Accounts",
        "icon": "📦",
        "label": "Console SKUs & Modules",
        "description": "Show the bundles (SKUs), modules, and settings enabled on each selected console.",
        "requires": "none",
        "console_field": "profiles",
        "fields": [
            {"id": "profiles", "type": "console-selector",
             "label": "Consoles to query"},
        ],
        "build_args": lambda f: [],
    },
    {
        "id": "lookup-accounts-bulk",
        "category": "Accounts",
        "icon": "📋",
        "label": "Bulk Account ID Lookup",
        "description": "Upload an Excel or CSV file with two columns — Console URL and Account Name — to resolve Account IDs across multiple consoles.",
        "requires": "none",
        "file_field": "excel_file",
        "fields": [
            {
                "id": "excel_file",
                "label": "Column A: Console URL · Column B: Account Name",
                "type": "file",
                "accept": ".xlsx,.xls,.csv",
            },
        ],
        "build_args": lambda f: [],
    },
    # Sites
    {
        "id": "list-sites",
        "category": "Sites",
        "icon": "🌐",
        "label": "List Sites",
        "description": "List all sites. Use Columns filter with accountId=<id> for a single account.",
        "requires": "mgmt",
        "fields": _list_fields("Search by Name"),
        "build_args": lambda f: _list_args(["list-sites"], f),
    },
    {
        "id": "sites-with-data-ingest",
        "category": "Sites",
        "icon": "📥",
        "label": "Sites with Data Ingest & Retention",
        "description": "List all sites that have a Data Ingest add-on enabled with any retention add-on selected. Shows Account ID, Account Name, Site Name, and Data Ingest selection.",
        "requires": "mgmt",
        "fields": [
            {"id": "account_id", "label": "Account ID", "type": "text",
             "placeholder": "Optional — leave blank for all accounts", "optional": True},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["sites-with-data-ingest"]
            + (["--account-id", f["account_id"]] if f.get("account_id") else [])
        ),
    },
    {
        "id": "create-site",
        "category": "Sites",
        "icon": "🌐",
        "label": "Create Site",
        "description": "Create a new site under an account.",
        "requires": "mgmt",
        "fields": [
            {"id": "payload", "label": "JSON Payload", "type": "textarea",
             "placeholder": '{"name": "Site Name", "accountId": "...", "siteType": "Paid"}'},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["create-site", "--payload", f["payload"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "update-site",
        "category": "Sites",
        "icon": "🌐",
        "label": "Update Site",
        "description": "Update an existing site.",
        "requires": "mgmt",
        "fields": [
            {"id": "site_id", "label": "Site ID", "type": "text", "placeholder": "Required"},
            {"id": "payload", "label": "JSON Payload", "type": "textarea",
             "placeholder": '{"name": "New Name"}'},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["update-site", f["site_id"], "--payload", f["payload"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    # Agents
    {
        "id": "list-agents",
        "category": "Agents",
        "icon": "🖥️",
        "label": "List Agents",
        "description": "List agents/endpoints. Search filters by computer name.",
        "requires": "mgmt",
        "fields": _list_fields("Search by Computer Name", "Filter by hostname"),
        "build_args": lambda f: _list_args(["list-agents"], f),
    },
    {
        "id": "agent-count",
        "category": "Agents",
        "icon": "📊",
        "label": "Agent Count by Platform",
        "description": "Health check: total installed agents per console, broken down by platform (Windows, Windows Legacy, macOS, Linux).",
        "requires": "none",
        "console_field": "profiles",
        "stream_url": "/api/agent-count-stream",
        "fields": [
            {"id": "profiles", "type": "console-selector", "label": "Consoles to query"},
        ],
        "build_args": lambda f: [],
    },
    {
        "id": "export-agent-passphrases",
        "category": "Agents",
        "icon": "🔑",
        "label": "Export Agent Passphrases",
        "description": "Fetch passphrases for all agents in an account (GET /agents/passphrases). Use 'Fetch all pages' to get every agent.",
        "requires": "mgmt",
        "fields": [
            {"id": "account_id", "label": "Account ID", "type": "text",
             "placeholder": "Required — account to export passphrases for"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["export-agent-passphrases", "--all", "--limit", "1000"]
            + (["--account-id", f["account_id"]] if f.get("account_id") else [])
        ),
    },
    {
        "id": "latest-agent-versions",
        "category": "Agents",
        "icon": "🔄",
        "label": "Latest Agent Versions",
        "description": "Show the latest GA 64-bit agent package version (Windows, Linux, macOS) per console.",
        "requires": "none",
        "console_field": "profiles",
        "stream_url": "/api/agent-versions-stream",
        "fields": [
            {"id": "profiles", "type": "console-selector", "label": "Consoles to query"},
        ],
        "build_args": lambda f: [],
    },
    {
        "id": "move-agents",
        "category": "Agents",
        "icon": "🖥️",
        "label": "Move Agents",
        "description": "Move agents to a different site.",
        "requires": "mgmt",
        "fields": [
            {"id": "payload", "label": "JSON Payload", "type": "textarea",
             "placeholder": '{"filter": {"ids": ["..."]}, "data": {"siteId": "..."}}'},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["move-agents", "--payload", f["payload"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "decommission-agent",
        "category": "Agents",
        "icon": "🖥️",
        "label": "Decommission Agent",
        "description": "Decommission an agent by ID.",
        "requires": "mgmt",
        "fields": [
            {"id": "agent_id", "label": "Agent ID", "type": "text", "placeholder": "Required"},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["decommission-agent", f["agent_id"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "initiate-scan",
        "category": "Agents",
        "icon": "🖥️",
        "label": "Initiate Scan",
        "description": "Trigger a full disk scan on an agent.",
        "requires": "mgmt",
        "fields": [
            {"id": "agent_id", "label": "Agent ID", "type": "text", "placeholder": "Required"},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["initiate-scan", f["agent_id"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "fetch-logs",
        "category": "Agents",
        "icon": "🖥️",
        "label": "Fetch Logs",
        "description": "Request diagnostic log collection from an agent.",
        "requires": "mgmt",
        "fields": [
            {"id": "agent_id", "label": "Agent ID", "type": "text", "placeholder": "Required"},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["fetch-logs", f["agent_id"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    # Threats
    {
        "id": "list-threats",
        "category": "Threats",
        "icon": "⚠️",
        "label": "List Threats",
        "description": "List threats. Search filters by computer name.",
        "requires": "mgmt",
        "fields": _list_fields("Search by Computer Name", "Filter by hostname"),
        "build_args": lambda f: _list_args(["list-threats"], f),
    },
    {
        "id": "resolve-threat",
        "category": "Threats",
        "icon": "⚠️",
        "label": "Resolve Threat",
        "description": "Mark a threat as resolved.",
        "requires": "mgmt",
        "fields": [
            {"id": "threat_id", "label": "Threat ID", "type": "text", "placeholder": "Required"},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["resolve-threat", f["threat_id"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "mark-threat-benign",
        "category": "Threats",
        "icon": "⚠️",
        "label": "Mark Threat Benign",
        "description": "Mark a threat as a false positive.",
        "requires": "mgmt",
        "fields": [
            {"id": "threat_id", "label": "Threat ID", "type": "text", "placeholder": "Required"},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["mark-threat-benign", f["threat_id"]]
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
    # Groups & Exclusions
    {
        "id": "list-groups",
        "category": "Groups & Exclusions",
        "icon": "👥",
        "label": "List Groups",
        "description": "List agent groups.",
        "requires": "mgmt",
        "fields": _list_fields("Search by Name"),
        "build_args": lambda f: _list_args(["list-groups"], f),
    },
    {
        "id": "list-exclusions",
        "category": "Groups & Exclusions",
        "icon": "👥",
        "label": "List Exclusions",
        "description": "List allowlist/blocklist exclusion entries.",
        "requires": "mgmt",
        "fields": [
            {"id": "limit", "label": "Limit", "type": "number",
             "placeholder": "Max items per page", "optional": True},
            {"id": "all_pages", "label": "Fetch all pages", "type": "checkbox"},
        ] + _out_fields(),
        "build_args": lambda f: _list_args(["list-exclusions"], f),
    },
    # Metering
    {
        "id": "metering-summary",
        "category": "Metering",
        "icon": "📊",
        "label": "Billing Summary",
        "description": "Pull all 11 billing reports for an account in one command.",
        "requires": "pq",
        "fields": [
            {"id": "account_id", "label": "Account ID", "type": "text", "placeholder": "Required"},
            {"id": "start_time", "label": "Start Time", "type": "text",
             "placeholder": "e.g. 31d", "default": "31d"},
            {"id": "end_time", "label": "End Time", "type": "text",
             "placeholder": "Optional", "optional": True},
        ] + _out_fields(),
        "build_args": lambda f: (
            ["metering", "summary", "--account-id", f["account_id"]]
            + ["--start-time", f.get("start_time") or "31d"]
            + (["--end-time", f["end_time"]] if f.get("end_time") else [])
            + _out_args(f)
        ),
    },
    {
        "id": "metering-list-reports",
        "category": "Metering",
        "icon": "📊",
        "label": "List Reports",
        "description": "List all available metering report names and descriptions.",
        "requires": "pq",
        "fields": _out_fields(include_columns=False),
        "build_args": lambda f: ["metering", "list-reports"] + _out_args(f),
    },
    {
        "id": "metering-workstation-endpoints",
        "category": "Metering",
        "icon": "📊",
        "label": "Workstation Endpoints",
        "description": "EDR workstation endpoint counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "workstation-endpoints"], f),
    },
    {
        "id": "metering-server-endpoints",
        "category": "Metering",
        "icon": "📊",
        "label": "Server Endpoints",
        "description": "CWS server endpoint counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "server-endpoints"], f),
    },
    {
        "id": "metering-container-hosts",
        "category": "Metering",
        "icon": "📊",
        "label": "Container Hosts",
        "description": "CWS container host counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "container-hosts"], f),
    },
    {
        "id": "metering-serverless-containers",
        "category": "Metering",
        "icon": "📊",
        "label": "Serverless Containers",
        "description": "CWS serverless container counts (EKS/ECS Fargate).",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "serverless-containers"], f),
    },
    {
        "id": "metering-identity-endpoints",
        "category": "Metering",
        "icon": "📊",
        "label": "Identity Endpoints",
        "description": "Identity IDR endpoint counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "identity-endpoints"], f),
    },
    {
        "id": "metering-identity-users",
        "category": "Metering",
        "icon": "📊",
        "label": "Identity Users",
        "description": "Identity user counts (ISPM/IdP).",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "identity-users"], f),
    },
    {
        "id": "metering-xdr-ingested-bytes",
        "category": "Metering",
        "icon": "📊",
        "label": "XDR Ingested Bytes",
        "description": "SDL Unified Data Lake ingested byte counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "xdr-ingested-bytes"], f),
    },
    {
        "id": "metering-log-analytics-bytes",
        "category": "Metering",
        "icon": "📊",
        "label": "Log Analytics Bytes",
        "description": "Log Analytics ingested byte counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "log-analytics-bytes"], f),
    },
    {
        "id": "metering-hyperautomation-actions",
        "category": "Metering",
        "icon": "📊",
        "label": "Hyperautomation Actions",
        "description": "Hyperautomation workflow action counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "hyperautomation-actions"], f),
    },
    {
        "id": "metering-cns-workloads",
        "category": "Metering",
        "icon": "📊",
        "label": "CNS Workloads",
        "description": "Cloud Native Security workload counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "cns-workloads"], f),
    },
    {
        "id": "metering-mobile-devices",
        "category": "Metering",
        "icon": "📊",
        "label": "Mobile Devices",
        "description": "Singularity Mobile enrolled device counts.",
        "requires": "pq",
        "fields": _report_fields(),
        "build_args": lambda f: _report_args(["metering", "mobile-devices"], f),
    },
    {
        "id": "metering-query",
        "category": "Metering",
        "icon": "📊",
        "label": "Raw PowerQuery",
        "description": "Execute a raw PowerQuery DSL statement against any datasource.",
        "requires": "pq",
        "fields": [
            {"id": "query", "label": "PowerQuery Statement", "type": "textarea",
             "placeholder": "| datasource 'metering' from 'workstation_endpoints'\n| columns endpoint_bundle, value"},
            {"id": "start_time", "label": "Start Time", "type": "text",
             "placeholder": "e.g. 31d", "default": "31d"},
            {"id": "end_time", "label": "End Time", "type": "text",
             "placeholder": "Optional", "optional": True},
            {"id": "all_pages", "label": "Fetch all pages", "type": "checkbox"},
        ] + _out_fields(),
        "build_args": lambda f: (
            ["metering", "query", f["query"]]
            + ["--start-time", f.get("start_time") or "31d"]
            + (["--end-time", f["end_time"]] if f.get("end_time") else [])
            + (["--all-pages"] if f.get("all_pages") else [])
            + _out_args(f)
        ),
    },
    # Config
    {
        "id": "config-show",
        "category": "Config",
        "icon": "⚙️",
        "label": "Show Config",
        "description": "Show the resolved configuration (tokens are masked).",
        "requires": "none",
        "fields": _out_fields(include_columns=False),
        "build_args": lambda f: ["config", "show"] + _out_args(f),
    },
    {
        "id": "config-list-profiles",
        "category": "Config",
        "icon": "⚙️",
        "label": "List Profiles",
        "description": "List profiles defined in ~/.config/s1am/config.",
        "requires": "none",
        "fields": _out_fields(include_columns=False),
        "build_args": lambda f: ["config", "list-profiles"] + _out_args(f),
    },
    # XDR / Data Lake
    {
        "id": "third-party-ingest",
        "category": "XDR / Data Lake",
        "icon": "🔎",
        "label": "Third-Party Data Ingest Check",
        "description": "Find all sites receiving non-SentinelOne data (dataSource.vendor != 'SentinelOne'). Runs the probe query across all selected consoles in parallel; drills into site-level counts for any console that has hits.",
        "requires": "none",
        "console_field": "profiles",
        "stream_url": "/api/third-party-ingest-stream",
        "fields": [
            {"id": "profiles", "type": "console-selector", "label": "Consoles to query"},
            {"id": "start_time", "label": "Time Range", "type": "text",
             "placeholder": "e.g. 36h, 7d, 31d", "default": "36h"},
        ],
        "build_args": lambda f: [],
    },
    # Advanced
    {
        "id": "raw",
        "category": "Advanced",
        "icon": "🔧",
        "label": "Raw API Call",
        "description": "Call any SentinelOne MGMT API endpoint directly.",
        "requires": "mgmt",
        "fields": [
            {"id": "method", "label": "HTTP Method", "type": "select",
             "options": ["GET", "POST", "PUT", "DELETE", "PATCH"], "default": "GET"},
            {"id": "path", "label": "API Path", "type": "text",
             "placeholder": "e.g. accounts  or  sites/123/policy"},
            {"id": "param", "label": "Query Params", "type": "text",
             "placeholder": "key=value,key2=value2", "optional": True},
            {"id": "payload", "label": "JSON Payload", "type": "textarea",
             "placeholder": '{"key": "value"}', "optional": True},
            {"id": "dry_run", "label": "Dry Run", "type": "checkbox"},
        ] + _out_fields(include_columns=False),
        "build_args": lambda f: (
            ["raw", f.get("method") or "GET", f["path"]]
            + [item for p in (f.get("param") or "").split(",")
               if p.strip() for item in ["--param", p.strip()]]
            + (["--payload", f["payload"]] if f.get("payload") else [])
            + (["--dry-run"] if f.get("dry_run") else [])
            + _out_args(f)
        ),
    },
]

_COMMAND_MAP = {c["id"]: c for c in COMMANDS}

# Serialisable subset (no build_args lambda) sent to the frontend
COMMANDS_JSON = [
    {k: v for k, v in cmd.items() if k != "build_args"}
    for cmd in COMMANDS
]


# ─── Credential Helpers ───────────────────────────────────────────────────────

_config_load_lock = threading.Lock()


def _load_config_silent(profile=None):
    """Load Config from file/env without blocking on interactive stdin prompt."""
    import io
    with _config_load_lock:
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("")  # isatty() → False, so _prompt_missing_values skips
        try:
            from s1am.api.config import Config
            return Config(profile=profile)
        except Exception:
            return None
        finally:
            sys.stdin = old_stdin


def _active_profile():
    """Return the active profile name stored in the session, or None."""
    return session.get("profile") or None


def _resolved_creds():
    """
    Return (base_url, api_token, pq_token, auth_prefix) with priority:
      session overrides  >  environment variables  >  active profile's config file values
    """
    cfg = _load_config_silent(profile=_active_profile())
    file_url    = cfg.base_url          if cfg else ""
    file_token  = cfg.api_token         if cfg else ""
    file_pq     = cfg.powerquery_token  if cfg else ""
    file_prefix = cfg.auth_prefix       if cfg else ""

    base_url    = session.get("base_url")    or os.environ.get("S1_BASE_URL", "")         or file_url
    api_token   = session.get("api_token")   or os.environ.get("S1_API_TOKEN", "")        or file_token
    pq_token    = session.get("pq_token")    or os.environ.get("S1_POWERQUERY_TOKEN", "") or file_pq
    auth_prefix = session.get("auth_prefix") or os.environ.get("S1_AUTH_PREFIX", "")      or file_prefix

    return base_url, api_token, pq_token, auth_prefix


def _session_env():
    """Build subprocess env: OS env overridden by resolved creds (session > env > config file)."""
    base_url, api_token, pq_token, auth_prefix = _resolved_creds()
    env = dict(os.environ)
    if base_url:    env["S1_BASE_URL"]             = base_url
    if api_token:   env["S1_API_TOKEN"]             = api_token
    if pq_token:    env["S1_POWERQUERY_TOKEN"]      = pq_token
    if auth_prefix: env["S1_AUTH_PREFIX"]           = auth_prefix
    return env


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/version")
def get_version():
    return jsonify({"version": _VERSION})


@app.route("/api/commands")
def get_commands():
    return jsonify(COMMANDS_JSON)


def _any_profile_has_token() -> bool:
    """Return True if any saved profile config file contains an api_token."""
    try:
        from s1am.api.config import Config
        for profile_id in (Config.list_profiles() or []):
            cfg = _load_config_silent(profile=profile_id)
            if cfg and cfg.api_token:
                return True
    except Exception:
        pass
    return False


@app.route("/api/status")
def get_status():
    base_url, api_token, pq_token, _ = _resolved_creds()
    mgmt_ok = bool(base_url and api_token)
    pq_ok   = bool(base_url and pq_token)
    return jsonify({
        "mgmt_configured":   mgmt_ok,
        "pq_configured":     pq_ok,
        "base_url":          base_url,
        "active_profile":    _active_profile(),
        "has_session_creds": bool(session.get("api_token") or session.get("pq_token")),
        "has_saved_tokens":  mgmt_ok or pq_ok or _any_profile_has_token(),
    })


_LABELS_FILE = Path.home() / ".config" / "s1am" / "web_labels.json"


def _load_labels() -> dict:
    """Load display-name labels from ~/.config/s1am/web_labels.json."""
    try:
        return json.loads(_LABELS_FILE.read_text())
    except Exception:
        return {}


def _save_labels(labels: dict) -> None:
    _LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LABELS_FILE.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


@app.route("/api/profiles")
def get_profiles():
    """List all profiles from ~/.config/s1am/config, enriched with display names."""
    try:
        from s1am.api.config import Config
        profile_ids = Config.list_profiles()
    except Exception:
        profile_ids = []
    labels = _load_labels()
    return jsonify({
        "profiles": [
            {"id": p, "name": labels.get(p) or p}
            for p in profile_ids
        ]
    })


@app.route("/api/profile-labels", methods=["GET"])
def get_profile_labels():
    return jsonify(_load_labels())


@app.route("/api/profile-labels", methods=["POST"])
def save_profile_labels():
    data = request.get_json() or {}
    # Only accept str → str mappings
    labels = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    _save_labels(labels)
    return jsonify({"success": True})


@app.route("/api/profile", methods=["POST"])
def set_profile():
    """Switch the active profile. POST {"profile": "acme"} or {"profile": ""} for default."""
    data = request.get_json() or {}
    profile = (data.get("profile") or "").strip()
    if profile:
        session["profile"] = profile
    else:
        session.pop("profile", None)
    # Clear any session credential overrides so the new profile's config is used cleanly
    for key in ("base_url", "api_token", "pq_token", "auth_prefix"):
        session.pop(key, None)
    base_url, api_token, pq_token, _ = _resolved_creds()
    return jsonify({
        "success":        True,
        "active_profile": _active_profile(),
        "mgmt_configured": bool(base_url and api_token),
        "pq_configured":   bool(base_url and pq_token),
    })


@app.route("/api/console", methods=["POST"])
def add_console():
    """Create a new console/profile. POST {"name":"CACE1","base_url":"https://...","api_token":"...","auth_prefix":"APIToken"}"""
    data = request.get_json() or {}
    name       = (data.get("name")        or "").strip()
    base_url   = (data.get("base_url")    or "").strip().rstrip("/")
    api_token  = (data.get("api_token")   or "").strip()
    auth_prefix = (data.get("auth_prefix") or "").strip()

    if not name or not base_url:
        return jsonify({"success": False, "error": "Name and Base URL are required."}), 400

    try:
        from s1am.api.config import Config
        Config.save_profile(name, base_url=base_url, api_token=api_token, auth_prefix=auth_prefix)
        return jsonify({"success": True, "name": name})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# ── Account Editor API ─────────────────────────────────────────────────────

def _build_client_for_console(console_code):
    """Build SentinelOneClient for a named console profile."""
    from s1am.api.client import SentinelOneClient
    cfg = _load_config_silent(profile=console_code)
    if not cfg:
        raise RuntimeError(
            f"Console '{console_code}' not found. Add it via 'Add Console' using '{console_code}' as the name."
        )
    base_url  = cfg.base_url  or os.environ.get("S1_BASE_URL", "")
    api_token = cfg.api_token or os.environ.get("S1_API_TOKEN", "")
    if not base_url or not api_token:
        raise RuntimeError(f"Console '{console_code}' has no base URL or API token configured.")
    return SentinelOneClient(
        base_url=base_url,
        api_token=api_token,
        api_path=getattr(cfg, "api_path", None) or "/web/api/v2.1",
        timeout=getattr(cfg, "timeout", 120),
        verify=getattr(cfg, "verify_ssl", True),
        auth_prefix=cfg.auth_prefix or os.environ.get("S1_AUTH_PREFIX", ""),
    )


def _split_tenant(tenant):
    """Return (console_code, account_id) or raise ValueError."""
    parts = (tenant or "").strip().split("_", 2)
    if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit():
        raise ValueError("Invalid tenant. Expected format: 1000_CODE_<accountId>")
    return parts[1], parts[2]


def _editor_target(body):
    """Return (console_code, account_id) from {console, accountId} or {tenant}; raise ValueError."""
    if body.get("console") and body.get("accountId"):
        account_id = str(body["accountId"]).strip()
        if not account_id.isdigit():
            raise ValueError("Account ID must be numeric.")
        return body["console"], account_id
    return _split_tenant(body.get("tenant", ""))


_BROWSE_PAGE_SIZE = 50


@app.route("/api/accounts-browse", methods=["POST"])
def api_accounts_browse():
    """List/search accounts for the Edit Account page.

    POST {"query"?: name | account ID | 1000_CODE_<id>, "cursor"?: str}
    Uses the active console, except a Salesforce tenant routes to its own console.
    """
    body   = request.get_json() or {}
    query  = (body.get("query") or "").strip()
    params = {"limit": _BROWSE_PAGE_SIZE, "sortBy": "name", "sortOrder": "asc"}
    console_code = _active_profile()

    try:
        console_code, account_id = _split_tenant(query)
        params["ids"] = account_id
    except ValueError:
        if query.isdigit():
            params["ids"] = query
        elif query:
            params["name__contains"] = query
    if not console_code:
        return jsonify({"error": "Select a console first, or search by Salesforce tenant (1000_CODE_<id>)."}), 400
    if body.get("cursor"):
        params["cursor"] = body["cursor"]

    try:
        client = _build_client_for_console(console_code)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    try:
        result = client.request("GET", "accounts", params=params)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    rows = []
    for a in result.get("data") or []:
        lic = a.get("licenses") or {}
        rows.append({
            "id":           a.get("id"),
            "name":         a.get("name"),
            "state":        a.get("state"),
            "accountType":  a.get("accountType"),
            "expiration":   a.get("expiration"),
            "activeAgents": a.get("activeAgents"),
            "skus":         sorted(b.get("displayName") or b.get("name") or "" for b in lic.get("bundles") or []),
        })
    pagination = result.get("pagination") or {}
    return jsonify({
        "console":    console_code,
        "accounts":   rows,
        "nextCursor": pagination.get("nextCursor"),
        "total":      pagination.get("totalItems"),
    })


def _api_error_details(exc):
    """The API's own error texts ("title: detail"), verbatim, from a SentinelOneError payload."""
    payload = getattr(exc, "payload", None)
    errors = (payload or {}).get("errors") if isinstance(payload, dict) else None
    out = []
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        detail, title = str(err.get("detail") or "").strip(), str(err.get("title") or "").strip()
        out.append(f"{title}: {detail}" if title and detail else (detail or title or json.dumps(err)))
    return out


def _explain_api_error(exc):
    """Extra guidance for API validation errors we recognise by their text, or None.

    Matched on the detail text only: SentinelOne's numeric codes (e.g. 4000010) are generic
    validation codes shared by unrelated errors, so they must not be used to classify.
    """
    for text in _api_error_details(exc):
        if "not available in the parent scope" in text:
            bundle = text.split(": ", 1)[-1].split(" ")[0]
            hint = ("For Prompt Security SKUs, ask your SentinelOne channel team to enable them on this "
                    "console." if licenses.is_prompt_bundle(bundle) else
                    "Ask SentinelOne to enable it on this console.")
            return f"{bundle} is not enabled on this console (global scope). {hint}"
    return None


def _account_api_error(exc):
    """JSON error for an account call, with distinct messages for 401 and 404."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return jsonify({"error": "Unauthorized (401): the API token for this console is missing, "
                                 "invalid or expired."}), 401
    if status == 404:
        return jsonify({"error": "Not found (404): the account no longer exists on this console."}), 404
    if status == 400:
        # Always show the API's own words; add guidance only when the text is recognised.
        details = _api_error_details(exc)
        message = "Rejected by the API (400): " + ("; ".join(details) if details else str(exc))
        explained = _explain_api_error(exc)
        if explained:
            message += f" — {explained}"
        _log.warning("[account-editor] API 400: %s", exc)
        return jsonify({"error": message, "detail": str(exc)}), 400
    return jsonify({"error": str(exc)}), 500


@app.route("/api/account-fetch", methods=["POST"])
def api_account_fetch():
    """Fetch account data for the edit UI. POST {console, accountId} or {tenant: "1000_CODE_<id>"}"""
    body = request.get_json() or {}
    try:
        console_code, account_id = _editor_target(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        client = _build_client_for_console(console_code)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    try:
        # The single-account view returns usageType/billingMode; the list view has been seen
        # to omit them on some consoles. Fall back to the list only if the single view fails.
        try:
            result = client.request("GET", f"accounts/{account_id}")
            account = result.get("data") if isinstance(result, dict) else None
        except Exception:
            account = None
        if not isinstance(account, dict) or not account.get("id"):
            result = client.request("GET", "accounts", params={"ids": account_id, "limit": 10})
            accounts = result.get("data") or []
            if not accounts:
                return jsonify({"error": f"No account with ID {account_id} on console '{console_code}'"}), 404
            account = accounts[0]
        catalog = _editor_catalog()
        labels = _load_labels()
        cfg = _load_config_silent(profile=console_code)
        return jsonify({"account": account, "console": console_code,
                        "console_label": labels.get(console_code, console_code),
                        "s1_region": licenses.s1_region(cfg.base_url if cfg else ""),
                        "catalog": catalog})
    except Exception as exc:
        return _account_api_error(exc)


def _editor_catalog():
    catalog = licenses.catalog()
    catalog["account"] = {
        "lockOnEdit": account_fields.LOCK_ON_EDIT,
        "usageTypes": account_fields.USAGE_TYPES,
        "billingModes": account_fields.BILLING_MODES,
        "accountTypes": account_fields.ACCOUNT_TYPES,
        "promptMaxUsers": account_fields.PROMPT_MAX_USERS,
        "promptStandardAdmins": account_fields.PROMPT_STANDARD_ADMINS,
    }
    return catalog


@app.route("/api/account-template", methods=["GET"])
def api_account_template():
    """Catalog and console context for the Create Account form. GET ?console=<profile>"""
    console_code = (request.args.get("console") or "").strip() or _active_profile()
    if not console_code:
        return jsonify({"error": "Select a console first."}), 400
    cfg = _load_config_silent(profile=console_code)
    if not cfg:
        return jsonify({"error": f"Console '{console_code}' not found."}), 404
    labels = _load_labels()
    return jsonify({
        "console": console_code,
        "console_label": labels.get(console_code, console_code),
        "s1_region": licenses.s1_region(cfg.base_url or ""),
        "catalog": _editor_catalog(),
    })


@app.route("/api/account-create", methods=["POST"])
def api_account_create():
    """Create an account. POST {console?, name, accountType?, usageType?, billingMode?, externalId?,
    expiration?|unlimitedExpiration, bundles?, modules?, settings?, promptFields?, dry_run?}"""
    body = request.get_json() or {}
    console_code = (body.get("console") or "").strip() or _active_profile()
    if not console_code:
        return jsonify({"error": "Select a console first."}), 400
    try:
        client = _build_client_for_console(console_code)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404

    cfg = _load_config_silent(profile=console_code)
    pd, errors = account_fields.build_create_payload(body, base_url=cfg.base_url if cfg else None)
    if errors:
        return jsonify({"error": "Not created: " + "; ".join(errors), "errors": errors}), 400
    try:
        # The docs recommend a name-availability check first; there is no such endpoint in the
        # v2.1 spec, so look the exact name up instead.
        existing = client.request("GET", "accounts", params={"name": pd["name"], "limit": 10})
        rows = (existing.get("data") or []) if isinstance(existing, dict) else []
        if any((a.get("name") or "").strip().lower() == pd["name"].lower() for a in rows):
            return jsonify({"error": f"An account named '{pd['name']}' already exists on this console."}), 409
        if body.get("dry_run"):
            return jsonify({"success": True, "dry_run": True, "payload": {"data": pd}})
        result = client.request("POST", "accounts", payload={"data": pd})
        account = result.get("data") if isinstance(result, dict) else result
        _log.info("[account-create] console=%s id=%s name=%s", console_code,
                  (account or {}).get("id") if isinstance(account, dict) else "?", pd["name"])
        return jsonify({"success": True, "account": account})
    except Exception as exc:
        return _account_api_error(exc)


@app.route("/api/account-save", methods=["POST"])
def api_account_save():
    """Save account edits. POST {console, accountId} or {tenant}, plus name?, bundles?, modules?, settings?"""
    body = request.get_json() or {}
    try:
        console_code, account_id = _editor_target(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        client = _build_client_for_console(console_code)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404
    try:
        pd, field_errors = account_fields.build_put_data(body)
        if field_errors:
            return jsonify({"error": "Not saved: " + "; ".join(field_errors), "errors": field_errors}), 400
        lic = {}
        if "bundles"  in body: lic["bundles"]  = body["bundles"]
        if "modules"  in body: lic["modules"]  = body["modules"]
        if "settings" in body: lic["settings"] = body["settings"]
        if "bundles" in lic and "settings" in lic:
            lic["settings"] = licenses.add_default_settings(
                lic["bundles"], licenses.prune_settings(lic["bundles"], lic["settings"]))
        if "bundles" in lic and "modules" in lic:
            errors = licenses.validate_licenses(lic["bundles"], lic["modules"])
            if errors:
                return jsonify({"error": "Not saved: " + "; ".join(errors), "errors": errors}), 400
        if lic:
            pd["licenses"] = lic
        # First-time Prompt provisioning on an existing account: the editor sends promptFields
        # only when the account has no tenant yet. Whether they are *required* is the API's
        # call (it knows if a tenant exists), so required=False here.
        if body.get("promptFields"):
            pf, pf_errors = account_fields.build_prompt_fields(body["promptFields"], required=False)
            if pf_errors:
                return jsonify({"error": "Not saved: " + "; ".join(pf_errors), "errors": pf_errors}), 400
            pd["promptFields"] = pf
        result = client.request("PUT", f"accounts/{account_id}", payload={"data": pd})
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return _account_api_error(exc)


# ─── Prompt EAP tracker ───────────────────────────────────────────────────────

def _eap():
    from s1am.eap import service
    return service, service.open_store()


@app.route("/api/eap/requests")
def eap_requests():
    """All tracked requests plus sync/enrichment metadata for the table."""
    from s1am.salesforce import auth as sf_auth
    svc, st = _eap()
    path = svc.form_path()
    meta = {
        "last_sync": st.get_meta("last_sync"), "sf_as_of": st.get_meta("sf_as_of"), "s1_as_of": st.get_meta("s1_as_of"),
        "form_path": str(path or ""), "form_exists": bool(path and path.exists()),
        "sf": sf_auth.status().as_dict(), "labels": _load_labels(), "consoles": sorted(svc.profiles()),
    }
    return jsonify({"rows": st.list(), "meta": meta})


@app.route("/api/eap/request/<int:rid>", methods=["GET"])
def eap_request(rid):
    _, st = _eap()
    row = st.get(rid)
    if not row:
        return jsonify({"error": f"No request {rid}"}), 404
    return jsonify({"row": row, "history": st.history(rid)})


@app.route("/api/eap/request/<int:rid>", methods=["PATCH"])
def eap_request_edit(rid):
    """User edits: only the editable fields; they become protected from sync/enrich."""
    from s1am.eap.store import USER_FIELDS
    from s1am.eap import enrich as eap_enrich
    svc, st = _eap()
    body = request.get_json() or {}
    fields = {k: v for k, v in body.items() if k in USER_FIELDS}
    if not fields:
        return jsonify({"error": "No editable fields in request"}), 400
    if st.get(rid) is None:
        return jsonify({"error": f"No request {rid}"}), 404
    st.update(rid, fields, source="user")
    # Picking a tenant by hand confirms it, resolves the "multiple/no contract tenant" review and
    # carries that tenant's agent counts; the console check then targets it (sf-tenant source).
    if "sf_tenant" in fields:
        row = st.get(rid)
        cand = next((c for c in row.get("sf_tenants") or [] if c.get("name") == fields["sf_tenant"]), None)
        st.update(rid, {"match_flag": None, "sf_tenant_id": (cand or {}).get("id"),
                        "complete_agents": (cand or {}).get("complete_agents"),
                        "control_agents": (cand or {}).get("control_agents"),
                        "total_agents": (cand or {}).get("total_agents")})
        fields.setdefault("tenant_confirmed", 1)
    if "tenant_confirmed" in fields:
        if fields["tenant_confirmed"]:
            row = st.get(rid)
            # Confirming the auto-selected tenant also pins it, so re-enrichment cannot swap it.
            st.update(rid, {"tenant_confirmed": 1, "sf_tenant": row.get("sf_tenant")}, source="user")
            st.update(rid, {"tenant_confirmed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "tenant_confirmed_by": svc.actor()})
            st.add_history(rid, "confirm", f"Tenant {row.get('sf_tenant')} confirmed by {svc.actor()}")
        else:
            row = st.get(rid)
            edited = [f for f in (row.get("edited_fields") or []) if f not in ("sf_tenant", "tenant_confirmed")]
            st.update(rid, {"tenant_confirmed": 0, "tenant_confirmed_at": None, "tenant_confirmed_by": None,
                            "edited_fields": edited})
            st.add_history(rid, "confirm", "Tenant confirmation withdrawn")
    if "chosen_console" in fields or "chosen_account_id" in fields or "sf_tenant" in fields:
        st.update(rid, {"s1_error": None, "s1_account_name": None, "s1_prompt_enabled": None})  # stale until re-checked
    if "status" not in fields:
        status, reason = eap_enrich.derive_status(st.get(rid))
        st.update(rid, {"status": status, "status_reason": reason})
    st.add_history(rid, "edit", "Edited: " + ", ".join(sorted(fields)))
    return jsonify({"row": st.get(rid)})


@app.route("/api/eap/sync", methods=["POST"])
def eap_sync():
    svc, st = _eap()
    body = request.get_json() or {}
    try:
        return jsonify(svc.sync(st, path=body.get("path") or None))
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/eap/enrich", methods=["POST"])
def eap_enrich_route():
    from s1am.salesforce.client import SalesforceConfigError
    svc, st = _eap()
    body = request.get_json() or {}
    try:
        return jsonify(svc.enrich_all(st, ids=body.get("ids")))
    except SalesforceConfigError as exc:
        return jsonify({"error": str(exc), "needs_login": True}), 401
    except Exception as exc:
        _log.warning("[eap] enrich failed: %s", exc)
        return jsonify({"error": str(exc).split(". Response content")[0][:400]}), 400


@app.route("/api/eap/check-stream", methods=["POST"])
def eap_check_stream():
    """Cross-check requests on their consoles, one row per SSE event."""
    svc, st = _eap()
    body = request.get_json() or {}
    ids = body.get("ids")
    result_q = queue.Queue()

    def worker():
        try:
            svc.check_all(st, _build_client_for_console, ids=ids, progress=result_q.put)
        except Exception as exc:  # surfaced as a row-less error event
            result_q.put({"__error__": str(exc)})
        result_q.put({"__done__": True})

    threading.Thread(target=worker, daemon=True).start()
    total = len(ids) if ids else len(st.list())

    def generate():
        yield f"data: {json.dumps({'__total__': total})}\n\n"
        while True:
            try:
                item = result_q.get(timeout=300)
            except queue.Empty:
                item = {"__done__": True, "__timeout__": True}
            yield f"data: {json.dumps(item, default=str)}\n\n"
            if item.get("__done__"):
                break

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/eap/seed", methods=["POST"])
def eap_seed():
    svc, st = _eap()
    body = request.get_json() or {}
    path = os.path.expanduser(body.get("path") or "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "Give the path to 'EAP - Customer List.xlsx'"}), 400
    try:
        return jsonify(svc.seed_customer_list(st, path))
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/eap/sf-login", methods=["POST"])
def eap_sf_login():
    """Run the browser SSO flow (blocks until the redirect or timeout)."""
    from s1am.salesforce import auth as sf_auth
    from s1am.salesforce import client as sf
    status = sf_auth.login_interactive(timeout=240)
    sf.reset_client()
    return jsonify(status.as_dict()), (200 if status.connected else 400)


@app.route("/api/credentials", methods=["GET"])
def get_credentials():
    """Return resolved credential info for the settings modal (tokens masked)."""
    base_url, api_token, pq_token, auth_prefix = _resolved_creds()
    def _src(sess_key, env_key, file_val):
        if session.get(sess_key): return "session"
        if os.environ.get(env_key): return "env"
        if file_val: return "config file"
        return "none"
    cfg = _load_config_silent(profile=_active_profile())
    return jsonify({
        "base_url":      base_url,
        "auth_prefix":   auth_prefix or "APIToken",
        "active_profile": _active_profile(),
        "mgmt_source":   _src("api_token", "S1_API_TOKEN",          cfg.api_token        if cfg else ""),
        "pq_source":     _src("pq_token",  "S1_POWERQUERY_TOKEN",   cfg.powerquery_token if cfg else ""),
        "mgmt_set":      bool(api_token),
        "pq_set":        bool(pq_token),
    })


@app.route("/api/credentials", methods=["POST"])
def save_credentials():
    data = request.get_json() or {}
    for key in ("base_url", "api_token", "pq_token", "auth_prefix"):
        val = data.get(key)
        if val is not None:
            if val:
                session[key] = val
            else:
                session.pop(key, None)
    return jsonify({"success": True})


@app.route("/api/credentials", methods=["DELETE"])
def clear_credentials():
    for key in ("base_url", "api_token", "pq_token", "auth_prefix"):
        session.pop(key, None)
    return jsonify({"success": True})


@app.route("/api/run", methods=["POST"])
def run_command():
    data = request.get_json() or {}
    command_id = data.get("command")
    fields = data.get("fields") or {}

    cmd_def = _COMMAND_MAP.get(command_id)
    if not cmd_def:
        return jsonify({"success": False, "error": f"Unknown command: {command_id}"}), 400

    try:
        cli_args = cmd_def["build_args"](fields)
    except (KeyError, TypeError) as exc:
        return jsonify({"success": False, "error": f"Missing required field: {exc}"}), 400

    env = _session_env()

    # Global flags: profile first, then quiet
    global_flags = []
    profile = _active_profile()
    if profile:
        global_flags += ["--profile", profile]
    global_flags += ["--quiet"]

    full_cmd = [sys.executable, "-m", "s1am"] + global_flags + cli_args

    # Human-readable CLI equivalent shown in the UI
    cli_display = "s1am " + " ".join(global_flags) + " " + " ".join(
        (f'"{a}"' if " " in a else a) for a in cli_args
    )

    # Some commands (e.g. lookup-accounts-bulk) read their payload from stdin.
    stdin_field = cmd_def.get("stdin_field")
    stdin_data = fields.get(stdin_field) if stdin_field else None

    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            input=stdin_data,
            env=env,
            timeout=180,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Command timed out (180 s).", "cli": cli_display})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc), "cli": cli_display})

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    if proc.returncode != 0:
        return jsonify({
            "success": False,
            "error": stderr or stdout or "Command failed with no output.",
            "cli": cli_display,
        })

    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            pass

    return jsonify({
        "success":        True,
        "data":           parsed,
        "raw":            stdout,
        "stderr":         stderr,
        "cli":            cli_display,
        "preferred_view": fields.get("format", "table"),  # user's display choice
    })


def _resolve_profiles(profiles_req):
    """Expand '__all__' to every saved profile; return list of profile name strings."""
    if profiles_req == "__all__" or profiles_req == ["__all__"]:
        from s1am.api.config import Config
        return Config.list_profiles() or [None]
    return profiles_req or []


def _process_one_console(profile, labels):
    """Query one console (single API call) and return its result row. Never raises."""
    from s1am.cli import _aggregate_licenses
    from s1am.api.client import SentinelOneClient

    display = labels.get(profile, profile) if profile else "default"
    try:
        cfg = _load_config_silent(profile=profile)
        if not cfg or not cfg.api_token:
            raise ValueError("No API token configured for this profile")
        client = SentinelOneClient(
            base_url=cfg.base_url,
            api_token=cfg.api_token,
            api_path=cfg.api_path,
            timeout=cfg.timeout,
            verify=cfg.verify_ssl,
            auth_prefix=cfg.auth_prefix,
        )
        # Single call — console-level bundles/modules are consistent across accounts
        resp = client.request("GET", "accounts", params={"limit": 100})
        accounts = resp.get("data") or [] if isinstance(resp, dict) else []
        total_accounts = (resp.get("pagination") or {}).get("totalItems", len(accounts)) if isinstance(resp, dict) else len(accounts)
        bundles, modules, settings = _aggregate_licenses(accounts)
        return {
            "console": display,
            "accounts": total_accounts,
            "bundles": bundles,
            "modules": modules,
            "settings": settings,
        }
    except Exception as exc:
        return {
            "console": display,
            "accounts": None,
            "bundles": f"ERROR: {exc}",
            "modules": "—",
            "settings": "—",
        }


@app.route("/api/console-modules", methods=["POST"])
def console_modules():
    data = request.get_json() or {}
    profiles = _resolve_profiles(data.get("profiles", []))
    if not profiles:
        return jsonify({"success": False, "error": "No consoles selected."}), 400

    labels = _load_labels()
    results = [_process_one_console(p, labels) for p in profiles]

    return jsonify({
        "success": True,
        "data": results,
        "raw": json.dumps(results, indent=2, sort_keys=True),
        "preferred_view": "table",
        "cli": f"s1am list-console-modules --profiles {','.join(p for p in profiles if p)}",
    })


@app.route("/api/console-modules-stream", methods=["POST"])
def console_modules_stream():
    """Stream results via SSE, querying all consoles in parallel via threads."""
    data = request.get_json() or {}
    profiles = _resolve_profiles(data.get("profiles", []))
    if not profiles:
        return jsonify({"success": False, "error": "No consoles selected."}), 400

    labels = _load_labels()
    result_q = queue.Queue()

    def worker(profile):
        result_q.put(_process_one_console(profile, labels))

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in profiles]
    for t in threads:
        t.start()

    def generate():
        total = len(profiles)
        yield f"data: {json.dumps({'__total__': total})}\n\n"
        for _ in range(total):
            try:
                row = result_q.get(timeout=180)
            except queue.Empty:
                row = {"console": "?", "accounts": None,
                       "bundles": "TIMEOUT", "modules": "—", "settings": "—"}
            yield f"data: {json.dumps(row)}\n\n"
        yield 'data: {"__done__": true}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _process_one_agent_count(profile, labels):
    """Count agents per platform on one console via GET /agents/count. Never raises."""
    from s1am.cli import _agent_counts_by_platform
    from s1am.api.client import SentinelOneClient

    display = labels.get(profile, profile) if profile else "default"
    t0 = time.monotonic()
    try:
        cfg = _load_config_silent(profile=profile)
        if not cfg or not cfg.api_token:
            raise ValueError("No API token configured for this profile")
        client = SentinelOneClient(
            base_url=cfg.base_url,
            api_token=cfg.api_token,
            api_path=cfg.api_path,
            timeout=cfg.timeout,
            verify=cfg.verify_ssl,
            auth_prefix=cfg.auth_prefix,
        )
        row = {"console": display}
        row.update(_agent_counts_by_platform(client))
        _log.info("[agent-count] console=%s total=%s elapsed=%.2fs",
                  display, row["Total"], time.monotonic() - t0)
        return row
    except Exception as exc:
        _log.error("[agent-count] console=%s error=%s", display, exc)
        return {"console": display, "Total": f"ERROR: {exc}"}


@app.route("/api/agent-count-stream", methods=["POST"])
def agent_count_stream():
    """Stream per-console agent counts by platform via SSE."""
    data = request.get_json() or {}
    profiles = _resolve_profiles(data.get("profiles", []))
    if not profiles:
        return jsonify({"success": False, "error": "No consoles selected."}), 400

    labels = _load_labels()
    result_q = queue.Queue()

    def worker(profile):
        result_q.put(_process_one_agent_count(profile, labels))

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in profiles]
    for t in threads:
        t.start()

    def generate():
        total = len(profiles)
        yield f"data: {json.dumps({'__total__': total})}\n\n"
        for _ in range(total):
            try:
                row = result_q.get(timeout=180)
            except queue.Empty:
                row = {"console": "?", "Total": "TIMEOUT"}
            yield f"data: {json.dumps(row)}\n\n"
        yield 'data: {"__done__": true}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _process_one_agent_version(profile, labels):
    """
    Query one console for the latest 64-bit GA agent package version per OS.

    Endpoint : GET /web/api/v2.1/update/agent/packages
    Filters  : packageTypes=Agent, platformTypes=windows,linux,macos,
               status=ga, osArches=64 bit
    Picks the highest semantic version per OS.  Never raises.
    """
    display  = labels.get(profile, profile) if profile else "default"
    endpoint = "update/agent/packages"
    t0 = time.monotonic()

    row = {
        "console": display,
        "Windows": "Not Available",
        "Linux":   "Not Available",
        "macOS":   "Not Available",
    }

    params = {
        "packageTypes": "Agent",
        "status":        "ga",
        "osArches":      "64 bit",
        "limit":         1000,
    }

    _log.info(
        "[agent-versions] console=%s endpoint=%s packageTypes=%s status=%s osArches=%s",
        display, endpoint,
        params["packageTypes"],
        params["status"], params["osArches"],
    )

    try:
        cfg = _load_config_silent(profile=profile)
        if not cfg or not cfg.api_token:
            raise ValueError("No API token configured for this profile")

        from s1am.api.client import SentinelOneClient

        client = SentinelOneClient(
            base_url=cfg.base_url,
            api_token=cfg.api_token,
            api_path=cfg.api_path,
            timeout=cfg.timeout,
            verify=cfg.verify_ssl,
            auth_prefix=cfg.auth_prefix,
        )

        def _pver(v):
            """Parse a dotted version string into a comparable tuple of ints."""
            parts = []
            for x in str(v or "0").split("."):
                try:
                    parts.append(int(x))
                except (ValueError, TypeError):
                    parts.append(0)
            return tuple(parts) if parts else (0,)

        pages    = client.paginate(endpoint, params=params)
        packages = [
            item
            for page in pages
            for item in ((page.get("data") or []) if isinstance(page, dict) else [])
        ]

        _log.info("[agent-versions] console=%s packages_returned=%d", display, len(packages))

        # Pick the highest semantic version per OS
        latest = {}
        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            os_type = (pkg.get("osType") or pkg.get("platformType") or "").lower()
            version  = pkg.get("version") or ""
            if os_type in ("windows", "linux", "macos") and version:
                if os_type not in latest or _pver(version) > _pver(latest[os_type]):
                    latest[os_type] = version

        row["Windows"] = latest.get("windows") or "Not Available"
        row["Linux"]   = latest.get("linux")   or "Not Available"
        row["macOS"]   = latest.get("macos")   or "Not Available"

        _log.info(
            "[agent-versions] console=%s windows=%s linux=%s macos=%s elapsed=%.2fs",
            display, row["Windows"], row["Linux"], row["macOS"],
            time.monotonic() - t0,
        )

    except Exception as exc:
        _log.error("[agent-versions] console=%s error=%s", display, exc)
        row["Windows"] = "Error"
        row["Linux"]   = "Error"
        row["macOS"]   = "Error"

    return row


def _powerquery_request(cfg, query, start_time="36h"):
    """POST a PowerQuery to /sdl/api/powerQuery and return the parsed JSON response."""
    import requests as _requests
    url = f"{cfg.base_url.rstrip('/')}/sdl/api/powerQuery"
    headers = {
        "Authorization": f"Bearer {cfg.powerquery_token}",
        "Content-Type": "application/json",
    }
    resp = _requests.post(
        url,
        json={"query": query, "startTime": start_time},
        headers=headers,
        timeout=60,
        verify=cfg.verify_ssl,
    )
    resp.raise_for_status()
    return resp.json()


def _pq_rows_to_dicts(pq_response):
    """Convert columnar PowerQuery response {columns, values} to list of dicts."""
    cols = [
        (c["name"] if isinstance(c, dict) else str(c))
        for c in (pq_response.get("columns") or [])
    ]
    return [
        {cols[i]: v for i, v in enumerate(row) if i < len(cols)}
        for row in (pq_response.get("values") or [])
    ]


_PROBE_QUERY = (
    'dataSource.vendor != "SentinelOne" AND NOT isempty(dataSource.vendor)\n'
    '| limit 1'
)

_DETAIL_QUERY = (
    'dataSource.vendor != "SentinelOne" AND NOT isempty(dataSource.vendor)\n'
    '| group SiteCount = count() by account.id, account.name, site.id, site.name\n'
    '| sort -SiteCount\n'
    '| limit 1000'
)


def _probe_third_party_ingest(profile, labels, start_time):
    """
    Run the third-party ingest probe on one console.
    Returns a list of site-level rows (empty list if no third-party data or no PQ token).
    Never raises.
    """
    display = labels.get(profile, profile) if profile else "default"
    try:
        cfg = _load_config_silent(profile=profile)
        if not cfg or not cfg.powerquery_token:
            _log.info("[third-party-ingest] console=%s skipped — no PQ token", display)
            return []

        _log.info("[third-party-ingest] console=%s probing start_time=%s", display, start_time)
        probe = _powerquery_request(cfg, _PROBE_QUERY, start_time)
        if not (probe.get("values") or []):
            _log.info("[third-party-ingest] console=%s no third-party data found", display)
            return []

        _log.info("[third-party-ingest] console=%s third-party data found — running detail query", display)
        detail = _powerquery_request(cfg, _DETAIL_QUERY, start_time)
        rows = _pq_rows_to_dicts(detail)
        for row in rows:
            row["console"] = display
        _log.info("[third-party-ingest] console=%s site_rows=%d", display, len(rows))
        return rows
    except Exception as exc:
        _log.error("[third-party-ingest] console=%s error=%s", display, exc)
        return [{"console": display, "account.id": "ERROR", "account.name": str(exc),
                 "site.id": "—", "site.name": "—", "SiteCount": 0}]


@app.route("/api/third-party-ingest-stream", methods=["POST"])
def third_party_ingest_stream():
    """Stream third-party data ingest results (one SSE message per console) via SSE."""
    data = request.get_json() or {}
    profiles = _resolve_profiles(data.get("profiles", []))
    start_time = (data.get("start_time") or "36h").strip()

    if not profiles:
        return jsonify({"success": False, "error": "No consoles selected."}), 400

    labels = _load_labels()
    result_q = queue.Queue()

    def worker(profile):
        result_q.put(_probe_third_party_ingest(profile, labels, start_time))

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in profiles]
    for t in threads:
        t.start()

    def generate():
        total = len(profiles)
        yield f"data: {json.dumps({'__total__': total})}\n\n"
        for _ in range(total):
            try:
                rows = result_q.get(timeout=120)
            except queue.Empty:
                rows = []
            yield f"data: {json.dumps(rows)}\n\n"
        yield 'data: {"__done__": true}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/agent-versions-stream", methods=["POST"])
def agent_versions_stream():
    """Stream agent version rows (one per console) via SSE."""
    data = request.get_json() or {}
    profiles = _resolve_profiles(data.get("profiles", []))
    if not profiles:
        return jsonify({"success": False, "error": "No consoles selected."}), 400

    labels = _load_labels()
    result_q = queue.Queue()

    def worker(profile):
        result_q.put(_process_one_agent_version(profile, labels))

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in profiles]
    for t in threads:
        t.start()

    def generate():
        total = len(profiles)
        console_names = [labels.get(p, p) if p else "default" for p in profiles]
        yield f"data: {json.dumps({'__total__': total, '__consoles__': console_names})}\n\n"
        for _ in range(total):
            try:
                row = result_q.get(timeout=180)
            except queue.Empty:
                row = {
                    "console": "?",
                    "Windows": "Error",
                    "Linux":   "Error",
                    "macOS":   "Error",
                }
            yield f"data: {json.dumps(row)}\n\n"
        yield 'data: {"__done__": true}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/agent-versions-export", methods=["POST"])
def agent_versions_export():
    """Return an XLSX file built from the provided agent-version row data."""
    import io
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return jsonify({"success": False, "error": "openpyxl is required for Excel export."}), 500

    data = request.get_json() or {}
    rows = data.get("rows") or []

    COLUMNS = [
        ("console", "Console"),
        ("Windows", "Windows"),
        ("Linux",   "Linux"),
        ("macOS",   "macOS"),
    ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Agent Versions"

    hdr_fill = PatternFill("solid", fgColor="1e2538")
    hdr_font = Font(bold=True, color="e2e8f0")

    for col_idx, (_, label) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="left")

    for row_idx, row in enumerate(rows, 2):
        for col_idx, (key, _) in enumerate(COLUMNS, 1):
            ws.cell(row=row_idx, column=col_idx, value=row.get(key, ""))

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=agent-versions.xlsx"},
    )


@app.route("/api/bulk-lookup-file", methods=["POST"])
def bulk_lookup_file():
    uploaded = request.files.get("excel_file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded."}), 400

    fname = uploaded.filename.lower()
    try:
        if fname.endswith(".csv"):
            import csv
            import io as _io
            text = uploaded.read().decode("utf-8-sig")
            rows = list(csv.reader(_io.StringIO(text)))
        elif fname.endswith((".xlsx", ".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
            ws = wb.active
            rows = [
                [str(cell.value).strip() if cell.value is not None else "" for cell in row]
                for row in ws.iter_rows()
            ]
            wb.close()
        else:
            return jsonify({
                "success": False,
                "error": "Unsupported file type. Please upload a .xlsx, .xls, or .csv file.",
            }), 400
    except Exception as exc:
        return jsonify({"success": False, "error": f"Failed to parse file: {exc}"}), 400

    # Skip header row
    if rows and rows[0] and rows[0][0].strip().lower() in (
        "console url", "console_url", "url", "console", "column a"
    ):
        rows = rows[1:]

    pairs = [
        (r[0].strip(), r[1].strip())
        for r in rows
        if len(r) >= 2 and r[0].strip() and r[1].strip()
    ]
    if not pairs:
        return jsonify({
            "success": False,
            "error": "No valid rows found. Expected two columns: Console URL, Account Name.",
        }), 400

    from s1am.cli import _console_name, _find_profile_for_url
    from s1am.api.client import SentinelOneClient
    from s1am.api.exceptions import SentinelOneError

    results = []
    for console_url, account_name in pairs:
        console_name = _console_name(console_url)
        profile = _find_profile_for_url(console_url)

        if profile is None:
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": None,
                "status": "NO_PROFILE",
            })
            continue

        try:
            cfg = _load_config_silent(profile=profile)
            if not cfg or not cfg.api_token:
                raise ValueError("No API token configured for this profile")
            client = SentinelOneClient(
                base_url=cfg.base_url,
                api_token=cfg.api_token,
                api_path=cfg.api_path,
                timeout=cfg.timeout,
                verify=cfg.verify_ssl,
                auth_prefix=cfg.auth_prefix,
            )
            pages = client.paginate("accounts", params={"name": account_name, "limit": 100})
            accounts = [item for page in pages for item in (page.get("data") or [])]
            match = next(
                (a for a in accounts if a.get("name", "").lower() == account_name.lower()),
                None,
            )
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": match["id"] if match else None,
                "status": "FOUND" if match else "NOT_FOUND",
            })
        except (SentinelOneError, ValueError) as exc:
            results.append({
                "console_name": console_name,
                "account_name": account_name,
                "account_id": None,
                "status": f"ERROR: {exc}",
            })

    return jsonify({
        "success": True,
        "data": results,
        "raw": json.dumps(results, indent=2, sort_keys=True),
        "preferred_view": "table",
        "cli": f"# Parsed {len(pairs)} row(s) from {uploaded.filename}",
    })


# ─── Entry Point ──────────────────────────────────────────────────────────────

def run(host="127.0.0.1", port=5173, debug=False, open_browser=True):
    if open_browser:
        import threading
        import time
        import webbrowser

        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    app.run(host=host, port=port, debug=debug)
