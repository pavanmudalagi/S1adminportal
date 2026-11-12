"""Read the Microsoft Forms workbook that collects Prompt Security EAP requests.

Forms headers are long, change wording, and carry non-breaking spaces and newlines, so
columns are matched by prefix after whitespace normalisation. Joe's working copy adds two
hand-edited columns ("Name" = a Salesforce link, "Provision Status") and calls the contact
name column Name1; a raw Forms export calls it Name2 and has neither extra column. Both
shapes are handled. openpyxl must load these files in normal (not read-only) mode: Forms
writes a bad dimension record and read-only mode then returns no rows.
"""
import datetime as _dt
import re

# (field, header prefix, lower-cased). First matching prefix wins; first column per field wins.
_HEADER_PREFIXES = (
    ("id", "id"),
    ("start_time", "start time"),
    ("completion_time", "completion time"),
    ("last_modified", "last modified time"),
    ("responder_email", "email"),
    ("joe_status", "provision status"),
    ("company", "company"),
    ("contact_email", "contact email"),
    ("super_admin_raw", "super admin"),
    ("products_raw", "which products"),
    ("details_raw", "please provide"),
)
_CONTACT_NAME_RE = re.compile(r"^name\d+$")


def normalize_header(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def map_headers(headers):
    """{column index: field name}. Unknown columns are ignored."""
    mapping = {}
    for i, raw in enumerate(headers):
        h = normalize_header(raw).lower()
        if not h:
            continue
        if h == "name":
            mapping[i] = "joe_ref"
            continue
        if _CONTACT_NAME_RE.match(h):
            mapping[i] = "contact_name"
            continue
        for field, prefix in _HEADER_PREFIXES:
            if h.startswith(prefix) and field not in mapping.values():
                mapping[i] = field
                break
    return mapping


def _cell(value):
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).replace("\xa0", " ").strip()
    return text or None


def read_workbook(path, sheet=None):
    """Rows of the Forms workbook as dicts keyed by field name (see map_headers)."""
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True)  # not read_only — see module docstring
    ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
    rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
    if not rows:
        return []
    mapping = map_headers(rows[0])
    if "id" not in mapping.values():
        raise ValueError(f"No 'Id' column in {path}; headers: {[normalize_header(h) for h in rows[0]]}")
    out = []
    for r in rows[1:]:
        rec = {field: (_cell(r[i]) if i < len(r) else None) for i, field in mapping.items()}
        try:
            rec["id"] = int(float(rec["id"]))
        except (TypeError, ValueError):
            continue  # a stray non-data row
        out.append(rec)
    return out


# ── Products ────────────────────────────────────────────────────────────────

PRODUCTS = ("Prompt Security", "Identity Security", "Hyperautomation", "Wayfinder MDR Essentials")
_PRODUCT_PREFIXES = (
    ("prompt security", "Prompt Security"),
    ("identity security", "Identity Security"),
    ("idenity security", "Identity Security"),   # seen in the Customer List
    ("hyperautomation", "Hyperautomation"),
    ("wayfinder", "Wayfinder MDR Essentials"),
)


def normalize_products(raw):
    """';'-separated Forms choices → canonical product names, de-duplicated, order kept."""
    out = []
    for part in re.split(r"[;\n]+", raw or ""):
        text = part.strip()
        if not text:
            continue
        low = text.lower()
        name = next((canon for prefix, canon in _PRODUCT_PREFIXES if low.startswith(prefix)), text)
        if name not in out:
            out.append(name)
    return out


# ── Super admin ─────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NAME_TRIM = " -,;:<>()[]|/\"'"


def parse_super_admin(raw, contact_name=None, contact_email=None):
    """Split the free-text Super Admin answer into name + email.

    Shapes seen: "email", "Name email", "Name - email", "Name, email", a bare name, or empty
    (the question was added after the first responses). Empty falls back to the form
    contact; a name without an email is flagged for review.
    Returns {"name", "email", "source", "needs_review", "note"}.
    """
    text = (raw or "").replace("\xa0", " ").strip()
    contact_email = (contact_email or "").strip() or None
    if not text:
        email = contact_email if contact_email and EMAIL_RE.fullmatch(contact_email) else None
        return {"name": contact_name, "email": email, "source": "contact", "needs_review": email is None,
                "note": "Super Admin not given; using the form contact" if email
                else "Super Admin not given and the contact email is not valid"}

    m = EMAIL_RE.search(text)
    if not m:
        return {"name": text, "email": None, "source": "super_admin", "needs_review": True,
                "note": "Super Admin given without an email address"}

    email = m.group(0)
    name = text[:m.start()].strip(_NAME_TRIM) or None
    source = "super_admin"
    if not name:
        name, source = contact_name, "mixed"
    note = ""
    if contact_email and "@" in contact_email and email.rsplit("@", 1)[-1].lower() != contact_email.rsplit("@", 1)[-1].lower():
        note = "Super Admin email domain differs from the contact's"
    return {"name": name, "email": email, "source": source, "needs_review": False, "note": note}


# ── Details text ────────────────────────────────────────────────────────────

_TENANT_RE = re.compile(r"\b1000_[A-Za-z0-9]+_\d{16,19}\b")
_MNAME_RE = re.compile(r"\b1000M-\d{4}-\d{4}-[A-Za-z]{2}_[0-9a-fA-F-]{36}\b")
_ACCOUNT_ID_RE = re.compile(r"(?<![\d-])(\d{16,19})(?![\d-])")
_HOST_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+sentinelone\.net)\b", re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
_GUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_LABEL_RE = re.compile(
    r"(?i)\b(current sentinelone details|sentinelone account number or account name|sentinelone account id"
    r"|console url|account name|account id|account is|console is|account|console|id)\s*:")
NO_S1_PHRASES = ("don't know", "dont know", "do not know", "not sure", "we do not have", "don't have",
                 "dont have", "do not have", "not currently", "no sentinel", "not a sentinelone",
                 "not an sentinelone", "n/a", "none")


def parse_details(raw):
    """Pull console hosts, account/scope IDs, GUIDs, tenants and S1 account names out of the
    free-text details answer. Returns {"hosts", "account_ids", "guids", "tenants", "s1_names",
    "leftover", "no_s1_claimed"}; leftover is whatever text remains (often an account name)."""
    out = {"hosts": [], "account_ids": [], "guids": [], "tenants": [], "s1_names": [],
           "leftover": "", "no_s1_claimed": False}
    text = (raw or "").replace("\xa0", " ")
    if not text.strip():
        return out

    def take(regex, key, transform=lambda s: s, repl=" "):
        nonlocal text
        for m in regex.finditer(text):
            value = transform(m.group(1) if regex.groups else m.group(0))
            if value not in out[key]:
                out[key].append(value)
        text = regex.sub(repl, text)

    take(_TENANT_RE, "tenants", str.upper)        # before IDs: a tenant contains an account ID
    take(_ACCOUNT_ID_RE, "account_ids", repl="")  # before URLs: ?_scopeId=<id> lives inside one, keep it whole
    take(_MNAME_RE, "s1_names")
    for m in _HOST_RE.finditer(text):           # note hosts first, then drop whole URLs (paths, queries)
        if m.group(1).lower() not in out["hosts"]:
            out["hosts"].append(m.group(1).lower())
    text = _URL_RE.sub(" ", text)
    text = _HOST_RE.sub(" ", text)              # bare hosts written without a scheme
    take(_GUID_RE, "guids", str.lower)

    low = raw.lower()
    identified = any(out[k] for k in ("hosts", "account_ids", "guids", "tenants", "s1_names"))
    out["no_s1_claimed"] = not identified and any(p in low for p in NO_S1_PHRASES)

    text = _LABEL_RE.sub(" ", text)
    text = re.sub(r"[()\[\]|:,;]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -|")
    out["leftover"] = text if len(text) >= 3 else ""
    return out


# ── Names ───────────────────────────────────────────────────────────────────

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(pty\.?\s*ltd\.?|limited partnership|limited|ltd\.?|llc|l\.l\.c\.|inc\.?|incorporated"
    r"|b\.?\s?v\.?|bvba|gmbh|corp\.?|corporation|co\.?|plc|s\.?a\.?|sarl|group)\b\.?", re.I)


def normalize_company(name):
    """Company name without legal suffixes/punctuation, for a LIKE match against Account.Name."""
    core = re.sub(r"[^\w\s&-]", " ", (name or "").replace("\xa0", " "))
    core = _LEGAL_SUFFIX_RE.sub(" ", core)
    core = re.sub(r"\s+", " ", core).strip(" -&")
    return core if len(core) >= 3 else (name or "").strip()


def suggest_tenant_name(company):
    """Guide convention for a Prompt tenant name is the account name; same cleanup as the editor."""
    return re.sub(r"[^a-z0-9_-]", "", re.sub(r"\s+", "_", (company or "").strip().lower()))[:64]
