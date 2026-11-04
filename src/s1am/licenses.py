"""SKU (bundle), add-on (module) and settings catalog for account licensing.

Internal API names were taken from live GET /accounts/{id} responses. Both the CLI
and the web UI use this module, so the dependency rules are enforced in one place.
"""
import re
from urllib.parse import urlparse

# SKUs shown in s1am. "surface" is the unit of measurement; count -1 means Unlimited.
SKUS = [
    {"name": "complete",                          "displayName": "Endpoint Security - Complete", "surface": "Total Agents",   "unlimited": True},
    {"name": "control",                           "displayName": "Endpoint Security - Control",  "surface": "Total Agents",   "unlimited": True},
    {"name": "unified_identity",                  "displayName": "Identity Security",            "surface": "Total Users",    "unlimited": True},
    {"name": "hyperautomation",                   "displayName": "Hyperautomation",              "surface": "Action Packs",   "unlimited": True},
    {"name": "singularity_data_lake",             "displayName": "Data Ingest",                  "surface": "Average GB/Day", "unlimited": False},
    {"name": "cloud_native_security_pro",         "displayName": "CNS Pro",                      "surface": "Workloads",      "unlimited": True},
    {"name": "cloud_native_security_foundations", "displayName": "CNS Foundations",              "surface": "Workloads",      "unlimited": True},
]

# Prompt Security bundles. Shown as checkboxes with a count (or Unlimited) apart from the SKU
# cards. Names/surfaces were confirmed on live Staging accounts (GET /accounts, 2026-11): the
# "ai_security_*_usage" strings in the MSSP provisioning guide are the METERING report names, not
# bundle names — the API rejects them ("Bundle is not available in your scope"). They are kept as
# read-aliases only. majorVersion is not hardcoded: existing bundles keep theirs, new ones omit it.
PROMPT_BUNDLES = [
    {"name": "prompt_for_ai_code_assistants", "aliases": ["ai_security_developers_usage"],
     "displayName": "Prompt for AI Code Assistants", "surface": "Total Developers", "unlimited": True},
    {"name": "prompt_employees",              "aliases": ["ai_security_employees_usage"],
     "displayName": "Prompt for Employees",         "surface": "Total Employees",  "unlimited": True},
]

# The guide says promptFields (tenant name + first users) is required the first time Prompt is
# added, and the editor has no form for it yet, so adding Prompt to an account without a tenant
# stays blocked. Set to False only once that form exists.
PROMPT_ADD_REQUIRES_FIELDS = True

# promptFields.promptRegion — the API enum (Staging, 2026-11: "Must be one of: useast, eunorth,
# apsouth, apsoutheast"). It is the S1 region group; note there is NO "uswest" value.
PROMPT_REGIONS = {
    "useast":      {"displayName": "US East",      "console": "https://useast.prompt.security"},
    "eunorth":     {"displayName": "EU North",     "console": "https://eunorth.prompt.security"},
    "apsouth":     {"displayName": "AP South",     "console": "https://apsouth.prompt.security"},
    "apsoutheast": {"displayName": "AP SouthEast", "console": "https://apsoutheast.prompt.security"},
}

# S1 cloud region → promptRegion, from SentinelOne's cluster table. USA and Canada both send "useast".
PROMPT_REGION_BY_S1_REGION = {
    "us-east-1":      "useast",        # USA
    "ca-central-1":   "useast",        # Canada
    "eu-central-1":   "eunorth",       # Germany / EU
    "ap-southeast-1": "apsouth",       # Singapore
    "ap-south-1":     "apsouth",       # Mumbai
    "ap-southeast-2": "apsoutheast",   # Sydney
}

# Where the tenant is actually hosted. The platform routes US MSSP partner tenants (and the
# customer accounts/sites beneath them) to US West; everything else follows promptRegion.
# Canadian MSSP tenants stay in US East.
PROMPT_CONSOLE_US_MSSP = "https://uswest.prompt.security"

# S1 cluster → S1 cloud region (cluster names as given: aprod-<token>-s1).
S1_REGION_BY_CLUSTER = {
    "aprod-use1-s1":  "us-east-1",
    "aprod-cac1-s1":  "ca-central-1",
    "aprod-euc1-s1":  "eu-central-1",
    "aprod-apse1-s1": "ap-southeast-1",
    "aprod-aps1-s1":  "ap-south-1",
    "aprod-apse2-s1": "ap-southeast-2",
}
_S1_REGION_BY_CLUSTER_TOKEN = {c.split("-")[1]: r for c, r in S1_REGION_BY_CLUSTER.items()}


def s1_region(base_url):
    """
    S1 cloud region for a console URL, or None if unknown.

    Matches the cluster token (use1, cac1, euc1, apse1, aps1, apse2) as the first hostname
    label, e.g. https://cac1-xxx.sentinelone.net → ca-central-1. Only the clusters in
    S1_REGION_BY_CLUSTER are recognised; other hostname spellings are deliberately not inferred.
    """
    host = urlparse(base_url if "//" in (base_url or "") else f"https://{base_url}").hostname or ""
    token = host.split(".")[0].split("-")[0].lower()
    return _S1_REGION_BY_CLUSTER_TOKEN.get(token)


def prompt_region(base_url):
    """promptRegion value for a console URL, or None if the console's cluster is unknown."""
    return PROMPT_REGION_BY_S1_REGION.get(s1_region(base_url) or "")


def prompt_console(region, s1_region_name=None, usage_type=None):
    """Prompt console URL a tenant lands in, for display. None if region is unknown."""
    if s1_region_name == "us-east-1" and (usage_type or "").lower() == "mssp":
        return PROMPT_CONSOLE_US_MSSP
    info = PROMPT_REGIONS.get(region or "")
    return info["console"] if info else None


# Add-ons shown in s1am. Add-ons greyed out in the console are intentionally omitted.
ADDONS = [
    {"name": "cloud_funnel",                     "displayName": "Cloud Funnel"},
    {"name": "xdr_30d_retention",                "displayName": "Data Ingest - Retention up to 30 days"},
    {"name": "xdr_90d_retention",                "displayName": "Data Ingest - Retention up to 90 days"},
    {"name": "xdr_180d_retention",               "displayName": "Data Ingest - Retention up to 180 days"},
    {"name": "xdr_365d_retention",               "displayName": "Data Ingest - Retention up to 1 year"},
    {"name": "ranger",                           "displayName": "Network Discovery"},
    {"name": "purple_ai_agentic_investigations", "displayName": "Purple AI Agentic Investigations"},
    {"name": "purple_ai",                        "displayName": "Purple AI Foundations"},
    {"name": "purple_ai_soc_analyst",            "displayName": "Purple AI SOC Analyst"},
    {"name": "rso",                              "displayName": "Remote Script Orchestration"},
    {"name": "remote_ops_forensics",             "displayName": "RemoteOps Forensics"},
    {"name": "threat_intel",                     "displayName": "Threat Intelligence"},
    {"name": "rogues",                           "displayName": "Unprotected Endpoints Discovery"},
    {"name": "vigilance",                        "displayName": "Vigilance MDR"},
    {"name": "vulnerability_management",         "displayName": "Vulnerability Management"},
    {"name": "wayfinder_mdr_essentials",         "displayName": "Wayfinder MDR Essentials"},
    {"name": "wayfinder_threat_hunting",         "displayName": "Wayfinder Threat Hunting"},
]

# Only Deep Visibility Data Retention is editable; all other settings are sent back unchanged.
# It comes with Endpoint Security - Complete only, and defaults to 14 Days when Complete is added.
DV_RETENTION_GROUP = "dv_retention"
DV_RETENTION_OPTIONS = ["14 Days", "30 Days", "90 Days", "180 Days", "365 Days"]
DV_RETENTION_DEFAULT = "14 Days"

# Dependency rules: {"requires_any": [...]} → at least one bundle; {"requires_all": [...]} → every module.
SKU_RULES = {
    "unified_identity": {"requires_any": ["complete", "control"]},
}
ADDON_RULES = {
    "wayfinder_mdr_essentials": {"requires_all": ["purple_ai", "wayfinder_threat_hunting"]},
}

# Settings that only apply while a given SKU is on the account; dropped (and hidden) otherwise.
SETTING_REQUIRES_SKU = {
    "ranger_ad_mode": "unified_identity",   # Identity Security Posture Mode → Identity Security
}

# Deep Visibility Data Retention is offered (and defaulted) only with Endpoint Security -
# Complete. It is deliberately NOT in SETTING_REQUIRES_SKU: live Control accounts have been
# seen carrying a dv_retention value, and pruning it on an unrelated save would silently
# change them. An existing value is therefore kept; only *adding* one needs Complete.
DV_RETENTION_REQUIRES_SKU = "complete"

# Settings added when their SKU is added and the account doesn't have them yet.
# "Full" is the only ranger_ad_mode value seen on live accounts (USEA1/USEA2).
SKU_DEFAULT_SETTINGS = {
    "unified_identity": [
        {"groupName": "ranger_ad_mode", "setting": "Full",
         "settingGroupDisplayName": "Identity Security Posture Mode"},
    ],
    "complete": [
        {"groupName": DV_RETENTION_GROUP, "setting": DV_RETENTION_DEFAULT,
         "settingGroupDisplayName": "Deep Visibility Data Retention"},
    ],
}

# Add-ons ticked by default when a SKU is newly added. Applied at the moment of adding (UI and
# CLI), not on every save, so an operator can still untick them afterwards.
SKU_DEFAULT_MODULES = {
    "complete": ["purple_ai", "rogues"],   # Purple AI Foundations, Unprotected Endpoints Discovery
}

_SKU_BY_NAME   = {s["name"]: s for s in SKUS}
_COUNTED_BY_NAME = {s["name"]: s for s in SKUS + PROMPT_BUNDLES}
_COUNTED_BY_NAME.update({alias: p for p in PROMPT_BUNDLES for alias in p["aliases"]})
_ADDON_BY_NAME = {a["name"]: a for a in ADDONS}


def _lookup(catalog, by_name, value):
    key = (value or "").strip().lower()
    if key in by_name:
        return by_name[key]
    for item in catalog:
        if item["displayName"].lower() == key:
            return item
    return None


def resolve_sku(value):
    """Return the SKU catalog entry for an internal or display name, or None."""
    return _lookup(SKUS, _SKU_BY_NAME, value)


def resolve_addon(value):
    """Return the add-on catalog entry for an internal or display name, or None."""
    return _lookup(ADDONS, _ADDON_BY_NAME, value)


def _display(name):
    item = _SKU_BY_NAME.get(name) or _ADDON_BY_NAME.get(name)
    return item["displayName"] if item else name


def is_prompt_bundle(name):
    """True if name (canonical or alias) is one of the Prompt Security bundles."""
    key = (name or "").strip().lower()
    return any(key == p["name"] or key in p["aliases"] for p in PROMPT_BUNDLES)


def has_prompt_bundle(bundles):
    return any(is_prompt_bundle(b.get("name")) for b in bundles or [])


def validate_licenses(bundles, modules):
    """Return a list of human-readable rule violations (empty when valid)."""
    bundle_names = {(b.get("name") or "").lower() for b in bundles or []}
    module_names = {(m.get("name") or "").lower() for m in modules or []}
    errors = []

    for sku, rule in SKU_RULES.items():
        if sku in bundle_names and not bundle_names & set(rule["requires_any"]):
            errors.append(
                f"{_display(sku)} requires one of: "
                + " or ".join(_display(n) for n in rule["requires_any"])
            )

    for addon, rule in ADDON_RULES.items():
        missing = [n for n in rule["requires_all"] if n not in module_names]
        if addon in module_names and missing:
            errors.append(
                f"{_display(addon)} requires: " + ", ".join(_display(n) for n in missing)
            )

    for b in bundles or []:
        sku = _COUNTED_BY_NAME.get((b.get("name") or "").lower())
        if not sku or sku["unlimited"]:
            continue
        for s in b.get("surfaces") or []:
            if s.get("name") == sku["surface"] and s.get("count") == -1:
                errors.append(f"{sku['displayName']} does not support an Unlimited {sku['surface']}")

    return errors


def prune_settings(bundles, settings):
    """Drop settings whose required SKU is not among bundles."""
    bundle_names = {(b.get("name") or "").lower() for b in bundles or []}
    kept = []
    for s in settings or []:
        required = SETTING_REQUIRES_SKU.get(s.get("groupName") or s.get("settingGroup"))
        if required is None or required in bundle_names:
            kept.append(s)
    return kept


def add_default_settings(bundles, settings):
    """Append SKU_DEFAULT_SETTINGS for present SKUs whose setting group is missing."""
    bundle_names = {(b.get("name") or "").lower() for b in bundles or []}
    out = list(settings or [])
    groups = {s.get("groupName") or s.get("settingGroup") for s in out}
    for sku, defaults in SKU_DEFAULT_SETTINGS.items():
        if sku not in bundle_names:
            continue
        for d in defaults:
            if d["groupName"] not in groups:
                out.append({"groupName": d["groupName"], "setting": d["setting"]})
                groups.add(d["groupName"])
    return out


def add_default_modules(added_skus, modules):
    """Return modules plus SKU_DEFAULT_MODULES for each newly added SKU, skipping ones present."""
    out = list(modules or [])
    present = {(m.get("name") or "").lower() for m in out}
    for sku in added_skus or []:
        for name in SKU_DEFAULT_MODULES.get((sku or "").lower(), []):
            if name not in present:
                out.append({"name": name})
                present.add(name)
    return out


def catalog():
    """Serializable catalog for the web UI."""
    return {
        "skus": SKUS,
        "addons": ADDONS,
        "promptBundles": PROMPT_BUNDLES,
        "promptAddRequiresFields": PROMPT_ADD_REQUIRES_FIELDS,
        "promptRegions": PROMPT_REGIONS,
        "promptRegionOrder": list(PROMPT_REGIONS),   # JSON objects lose order; US East first
        "promptRegionByS1Region": PROMPT_REGION_BY_S1_REGION,
        "promptConsoleUsMssp": PROMPT_CONSOLE_US_MSSP,
        "s1RegionByCluster": S1_REGION_BY_CLUSTER,
        "dvRetention": {"groupName": DV_RETENTION_GROUP, "options": DV_RETENTION_OPTIONS,
                        "default": DV_RETENTION_DEFAULT, "requiresSku": DV_RETENTION_REQUIRES_SKU},
        "skuRules": SKU_RULES,
        "addonRules": ADDON_RULES,
        "settingRequiresSku": SETTING_REQUIRES_SKU,
        "skuDefaultSettings": SKU_DEFAULT_SETTINGS,
        "skuDefaultModules": SKU_DEFAULT_MODULES,
    }
