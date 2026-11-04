"""Account-level fields editable from the Edit Account modal, and their validation.

Verified against docs/swagger_2.1.md (definitions accounts.schemas_AccountPutSchema,
accounts.schemas_PostAccountSchema, accounts.schemas_AccountViewSchema_200). If the
docs and a sample response disagree, the Swagger schema wins. Re-verify here first:

  Field                    PUT  POST  GET   Type / constraint
  name                     yes  yes   yes   string, minLength 1, pattern ^[^<>]+$ (PUT: nullable)
  accountType              yes  yes   yes   enum Trial | Paid (PUT: nullable)
  usageType                yes  yes   yes   enum customer | mssp | ir
  billingMode              yes  yes   yes   enum subscription | consumption
  expiration               yes  yes   yes   ISO date-time string, nullable
  unlimitedExpiration      yes  yes   yes   boolean (POST: nullable)
  salesforceId             yes  no    yes   string, NOT nullable
  externalId               yes  yes   yes   string, nullable ("ID of CRM external system")
  licenses.bundles[]       yes  yes   yes   {name, majorVersion (int>=0, nullable),
                                             surfaces[{name, count (int>=-1)}]}
  licenses.modules[]       yes  yes   yes   {name}
  licenses.settings[]      yes  yes   yes   {groupName, setting}  (GET adds display fields)
  promptFields             NO*  YES*  NO*   * absent from the Swagger copy in docs/, but PRESENT in the
                                             POST /accounts schema served by the Staging console
                                             (2026-11): promptRegion, promptTenantName, promptUserName,
                                             promptUserEmail, promptUsers[]. See docs/prompt-security-api.md
  promptFields.promptRegion                  DO NOT SEND. Listed in the POST schema with enum useast |
                                             eunorth | apsouth | apsoutheast, but Staging answers
                                             "prompt_region field is not available" (2026-11): the platform
                                             assigns the region from the console. Shown read-only in the UI
                                             (licenses.prompt_region / prompt_console); PROMPT_SEND_REGION
                                             re-enables sending if a console ever requires it

Consequences:
  * PUT accepts usageType and billingMode; both are editable in the modal. LOCK_ON_EDIT can
    re-lock either one with a single entry if a console turns out to reject the change.
  * promptFields is not documented anywhere, so it is display-only: never sent on PUT.
  * "Custom Account Identifier" is written to salesforceId (the field the sample account
    carried and the one `update-account` looks accounts up by). It is not exposed on POST,
    so externalId is the alternative if that ever needs to change.
  * usageType "ir" (Incident Response) is deliberately not offered as a choice; an
    existing "ir" account is shown read-only.
  * Prompt bundle names are prompt_employees / prompt_for_ai_code_assistants (live Staging GET);
    the guide's ai_security_*_usage strings are metering identifiers and are rejected as bundles.
  * Create (POST) has no salesforceId, so the identifier is written to externalId there.
    POST defaults: accountType Paid, usageType customer, billingMode subscription, inherits true.
    promptFields is required iff a Prompt bundle is in licenses.bundles (guide: "All prompt
    fields are required when Prompt SKU is used"). One admin → promptUserName/promptUserEmail;
    several → promptUsers[] (never both).
"""
import re

from . import licenses

ACCOUNT_TYPES = ["Trial", "Paid"]
USAGE_TYPES = ["customer", "mssp", "ir"]
BILLING_MODES = ["subscription", "consumption"]

# Fields shown read-only on edit. PUT accepts usageType and billingMode (Swagger), and the
# user asked for both to be editable, so nothing is locked by default. Add a name to lock it.
LOCK_ON_EDIT = []

_NAME_RE = re.compile(r"^[^<>]+$")


def build_put_data(body, locked=None):
    """Turn the editor's request body into the `data` of PUT /accounts/{id}.

    Only keys present in `body` are considered, so the caller controls what changed.
    Returns (data, errors). `licenses` is handled by the caller.
    """
    locked = LOCK_ON_EDIT if locked is None else locked
    data, errors = {}, []

    if "name" in body:
        name = (body["name"] or "").strip() if isinstance(body["name"], str) else ""
        if not name:
            errors.append("Account name is required")
        elif not _NAME_RE.match(name):
            errors.append("Account name cannot contain < or >")
        else:
            data["name"] = name

    for key, allowed in (("accountType", ACCOUNT_TYPES),
                         ("usageType", USAGE_TYPES),
                         ("billingMode", BILLING_MODES)):
        if key not in body:
            continue
        if key in locked:
            errors.append(f"{key} cannot be changed on an existing account")
        elif body[key] not in allowed:
            errors.append(f"{key} must be one of: {', '.join(allowed)}")
        else:
            data[key] = body[key]

    if "salesforceId" in body:
        value = body["salesforceId"]
        if not isinstance(value, str):
            errors.append("Account identifier must be text")
        else:
            data["salesforceId"] = value.strip()

    unlimited = body.get("unlimitedExpiration")
    if unlimited is True:
        data["unlimitedExpiration"] = True
        data["expiration"] = None
    else:
        if unlimited is False:
            data["unlimitedExpiration"] = False
            if not body.get("expiration"):
                errors.append("Expiration date is required unless the account does not expire")
        if body.get("expiration"):
            data["expiration"] = body["expiration"]

    return data, errors


# promptFields rules. The guide says lowercase letters, digits, "-" and "_"; the API itself
# accepts capitals (a live Staging tenant is named "PavanTestAccount"), so only spaces and
# special characters are blocked here and lowercase is recommended in the UI.
_TENANT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROMPT_MAX_USERS = 10
# promptRegion is assigned by the platform; sending it is rejected ("prompt_region field is not
# available"). Flip to True only for a console that demands it.
PROMPT_SEND_REGION = False


def build_prompt_fields(pf, required, default_region=None):
    """Validate the editor's Prompt Security details into a promptFields object.

    `pf` is {"promptTenantName": str, "promptUsers": [{"promptUserName", "promptUserEmail"}]}
    (a single promptUserName/promptUserEmail pair is accepted too). A promptRegion, if given,
    is validated but only sent when PROMPT_SEND_REGION is True (then falling back to
    `default_region`). Returns (dict|None, errors).
    """
    if not pf or not isinstance(pf, dict):
        return None, (["Prompt Security details are required when a Prompt SKU is selected"] if required else [])
    errors = []
    region = (pf.get("promptRegion") or "").strip().lower()
    if region and region not in licenses.PROMPT_REGIONS:
        errors.append("Prompt region must be one of: " + ", ".join(licenses.PROMPT_REGIONS))
    if PROMPT_SEND_REGION and not region:
        region = (default_region or "").strip().lower()
        if not region:
            errors.append("Prompt region is required (could not be derived from this console)")
    tenant = (pf.get("promptTenantName") or "").strip()
    if not tenant:
        errors.append("Prompt tenant name is required")
    elif not _TENANT_NAME_RE.match(tenant):
        errors.append("Prompt tenant name may only contain letters, numbers, hyphens and underscores (no spaces)")

    users = pf.get("promptUsers")
    if users is None and (pf.get("promptUserName") or pf.get("promptUserEmail")):
        users = [{"promptUserName": pf.get("promptUserName"), "promptUserEmail": pf.get("promptUserEmail")}]
    users = [u for u in (users or []) if isinstance(u, dict)]
    cleaned, seen = [], set()
    for u in users:
        name = (u.get("promptUserName") or "").strip()
        email = (u.get("promptUserEmail") or "").strip()
        if not name or not email:
            errors.append("Each Prompt first user needs a name and an email")
            continue
        if not _EMAIL_RE.match(email):
            errors.append(f"Invalid Prompt first user email: {email}")
        if email.lower() in seen:
            errors.append(f"Duplicate Prompt first user email: {email}")
        seen.add(email.lower())
        cleaned.append({"promptUserName": name, "promptUserEmail": email})
    if not cleaned and not errors:
        errors.append("At least one Prompt first user is required")
    if len(cleaned) > PROMPT_MAX_USERS:
        errors.append(f"At most {PROMPT_MAX_USERS} Prompt first users are allowed")
    if errors:
        return None, errors

    out = {"promptTenantName": tenant}
    if PROMPT_SEND_REGION and region:
        out["promptRegion"] = region
    if len(cleaned) == 1:
        out.update(cleaned[0])
    else:
        out["promptUsers"] = cleaned
    return out, []


def build_create_payload(body, base_url=None):
    """Turn the editor's (or CLI's) request body into the `data` of POST /accounts.

    Applies the POST defaults, validates enums, licenses rules and promptFields, and returns
    (data, errors). `base_url` (the console) lets promptRegion default to the derived region.
    The name-uniqueness check needs the API and is done by the caller.
    """
    errors = []
    data = {"inherits": True}

    name = body.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        errors.append("Account name is required")
    elif not _NAME_RE.match(name):
        errors.append("Account name cannot contain < or >")
    else:
        data["name"] = name

    for key, allowed, default in (("accountType", ACCOUNT_TYPES, "Paid"),
                                  ("usageType", USAGE_TYPES, "customer"),
                                  ("billingMode", BILLING_MODES, "subscription")):
        value = body.get(key) or default
        if value not in allowed:
            errors.append(f"{key} must be one of: {', '.join(allowed)}")
        else:
            data[key] = value

    identifier = body.get("externalId", body.get("salesforceId"))
    if isinstance(identifier, str) and identifier.strip():
        data["externalId"] = identifier.strip()

    if body.get("unlimitedExpiration"):
        data["unlimitedExpiration"] = True
    elif body.get("expiration"):
        data["unlimitedExpiration"] = False
        data["expiration"] = body["expiration"]
    else:
        errors.append("Expiration date is required unless the account does not expire")

    bundles = body.get("bundles") or []
    modules = body.get("modules") or []
    settings = licenses.add_default_settings(bundles, licenses.prune_settings(bundles, body.get("settings") or []))
    errors += licenses.validate_licenses(bundles, modules)
    data["licenses"] = {"bundles": bundles, "modules": modules, "settings": settings}

    has_prompt = licenses.has_prompt_bundle(bundles)
    default_region = licenses.prompt_region(base_url) if base_url else None
    pf, pf_errors = build_prompt_fields(body.get("promptFields"), required=has_prompt,
                                        default_region=default_region)
    errors += pf_errors
    if pf and not has_prompt:
        errors.append("Prompt Security details were given but no Prompt SKU is selected")
    elif pf:
        data["promptFields"] = pf

    return data, errors
