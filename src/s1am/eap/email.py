"""The customer email sent once Prompt Security is enabled: template, recipients, draft links.

Nothing is sent from here. The app renders a draft (subject, body, To, CC) that the user
opens in Outlook on the web or the default mail app, edits, and sends themselves.

Template: ~/.config/s1am/eap_email_template.html when present (editable from the UI),
else the built-in email_template.html next to this module. First line "Subject: …",
blank line, then the HTML body. Placeholders are {name} from PLACEHOLDERS; unknown ones
render empty; values are HTML-escaped in the body. A plain-text rendering is derived from
the HTML for clients that cannot take rich text.

Neither mailto: nor the Outlook compose deep link accepts HTML, so the UI opens the draft
with To/CC/subject and puts the formatted body on the clipboard for one paste.

Recipients: To = the form contact. CC = the fixed addresses in [eap] email_cc (default
Joe Kern) plus the Salesforce Account Team members whose role is in [eap] email_cc_roles.
Every active team member is offered in the draft so the user can tick others in.
"""
import html
import re
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).with_name("email_template.html")
CUSTOM_TEMPLATE = Path.home() / ".config" / "s1am" / "eap_email_template.html"

FIXED_CC = ["Joe.Kern@n-able.com"]
# Salesforce AccountTeamMember.TeamMemberRole values (the UI shows "Customer Success" as "Partner Success").
DEFAULT_CC_ROLES = [
    "Technical Account Owner", "Customer Success", "Growth Strategist",
    "Velocity (Backup AE & Security AE)", "Security AE", "Backup AE", "Tools AE",
    "Channel Manager", "Account Manager",
]
ROLE_LABELS = {"Customer Success": "Partner Success"}

PLACEHOLDERS = {
    "company": "customer company name",
    "contact_name": "form contact's full name",
    "contact_first_name": "form contact's first name",
    "contact_email": "form contact's email",
    "super_admin_name": "super admin name",
    "super_admin_email": "super admin email",
    "products": "products requested, comma separated",
    "sf_tenant": "Salesforce tenant (1000_…)",
    "tenant_clause": "' (tenant 1000_…)' when a tenant is known, else empty",
    "prompt_tenant_name": "Prompt tenant name from the provisioning details",
    "first_users": "first users, 'Name <email>' comma separated",
    "console": "console code, e.g. USEA3",
    "console_url": "https://<console host>",
    "account_id": "SentinelOne account ID",
    "s1_account_name": "SentinelOne account name",
    "date_enabled": "date enabled (provisioning details)",
    "enabled_by": "who enabled it (provisioning details)",
    "sender_name": "the signed-in user's name",
    "sender_email": "the signed-in user's email",
    "today": "today's date, YYYY-MM-DD",
}
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


# ── template ────────────────────────────────────────────────────────────────

def template_path():
    return CUSTOM_TEMPLATE if CUSTOM_TEMPLATE.exists() else DEFAULT_TEMPLATE


def load_template():
    """{"text", "path", "custom"} — the raw template and where it came from."""
    path = template_path()
    return {"text": path.read_text(encoding="utf-8"), "path": str(path), "custom": path == CUSTOM_TEMPLATE}


def save_template(text):
    """Store a custom template (empty text removes it and goes back to the built-in one)."""
    if not (text or "").strip():
        if CUSTOM_TEMPLATE.exists():
            CUSTOM_TEMPLATE.unlink()
        return load_template()
    split_template(text)  # validates
    CUSTOM_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_TEMPLATE.write_text(text.strip() + "\n", encoding="utf-8")
    return load_template()


def split_template(text):
    """(subject template, body template). The subject is the first 'Subject:' line."""
    lines = (text or "").strip("\n").splitlines()
    if not lines or not lines[0].lower().startswith("subject:"):
        raise ValueError("The template must start with a 'Subject: …' line")
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return subject, body


def render(text, ctx, escape=False):
    """Fill {placeholders}; unknown or empty ones become ''. escape=True HTML-escapes the values."""
    def value(m):
        v = str(ctx.get(m.group(1), "") or "")
        return html.escape(v) if escape else v
    return _PLACEHOLDER_RE.sub(value, text or "")


class _TextExtractor(HTMLParser):
    """HTML → readable plain text: paragraphs separated by blank lines, <li> as "- " or "1.",
    links as "text (url)" when the text is not the URL itself."""
    _BLOCK = {"p", "div", "ul", "ol", "br", "h1", "h2", "h3", "h4", "table", "tr"}

    def __init__(self):
        super().__init__()
        self.out, self.lists, self.href, self.link_text = [], [], None, []

    def _nl(self, n=1):
        text = "".join(self.out)
        trailing = len(text) - len(text.rstrip("\n"))
        if trailing < n:
            self.out.append("\n" * (n - trailing))

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._nl(1)
        elif tag == "li":
            self._nl(1)
            if self.lists:
                self.lists[-1][1] += 1
                kind, n = self.lists[-1]
                self.out.append("   " * (len(self.lists) - 1) + (f"{n}. " if kind == "ol" else "- "))
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.link_text = []
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "tr"):
            self._nl(2 if tag != "tr" else 1)

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._nl(2 if not self.lists else 1)
        elif tag == "li":
            self._nl(1)
        elif tag == "a":
            text = "".join(self.link_text).strip()
            if self.href and text and text.rstrip("/.") != self.href.rstrip("/."):
                self.out.append(f" ({self.href})")
            self.href, self.link_text = None, []
        elif tag in ("p", "div", "h1", "h2", "h3", "h4"):
            self._nl(2)

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data)
        if text.strip() == "" and (not self.out or self.out[-1].endswith("\n")):
            return
        if self.href is not None:
            self.link_text.append(text)
        self.out.append(text)

    def text(self):
        return re.sub(r"[ \t]+\n", "\n", "".join(self.out)).strip() + "\n"


def html_to_text(markup):
    p = _TextExtractor()
    p.feed(markup or "")
    p.close()
    return p.text()


def unknown_placeholders(text):
    return sorted({m for m in _PLACEHOLDER_RE.findall(text or "") if m not in PLACEHOLDERS})


# ── context from a request row ──────────────────────────────────────────────

def _users(value):
    return ", ".join(f"{u.get('name') or ''} <{u.get('email') or ''}>".strip() for u in value or [] if u.get("email") or u.get("name"))


def context(row, target=None, sender=None, today=None, sender_email=None):
    """Placeholder values for one request. `target` is {"console", "account_id", "host"} when known."""
    target = target or {}
    tenant = row.get("sf_tenant") or ""
    host = target.get("host") or ""
    return {
        "company": row.get("company") or row.get("sf_account_name") or "",
        "contact_name": row.get("contact_name") or "",
        "contact_first_name": (row.get("contact_name") or "").split(" ")[0],
        "contact_email": row.get("contact_email") or "",
        "super_admin_name": row.get("super_admin_name") or row.get("contact_name") or "",
        "super_admin_email": row.get("super_admin_email") or row.get("contact_email") or "",
        "products": ", ".join(row.get("products") or []),
        "sf_tenant": tenant,
        "tenant_clause": f" (tenant {tenant})" if tenant else "",
        "prompt_tenant_name": row.get("prompt_tenant_name") or row.get("s1_prompt_tenant") or "",
        "first_users": _users(row.get("first_users")) or _users(row.get("s1_prompt_users")),
        "console": target.get("console") or row.get("s1_console") or "",
        "console_url": f"https://{host}" if host else "",
        "account_id": str(target.get("account_id") or row.get("s1_account_id") or ""),
        "s1_account_name": row.get("s1_account_name") or "",
        "date_enabled": row.get("date_enabled") or "",
        "enabled_by": row.get("enabled_by") or "",
        "sender_name": sender or "",
        "sender_email": sender_email or "",
        "today": today or "",
    }


# ── recipients ──────────────────────────────────────────────────────────────

def team_from_salesforce(records):
    """AccountTeamMember rows → [{"name", "email", "role"}], active users with an email only."""
    out, seen = [], set()
    for r in records or []:
        email = (r.get("User.Email") or "").strip()
        if not email or r.get("User.IsActive") is False:
            continue
        key = (email.lower(), r.get("TeamMemberRole") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": r.get("User.Name") or email, "email": email, "role": r.get("TeamMemberRole") or ""})
    return out


def role_label(role):
    return ROLE_LABELS.get(role, role)


def recipients(row, team, fixed_cc=None, cc_roles=None):
    """{"to": [...], "cc": [{"name","email","role","checked"}], "missing": [...]}.

    The contact is To. CC offers every fixed address and team member; `checked` is the
    default selection (fixed addresses, the super admin when different, roles in cc_roles).
    """
    fixed_cc = FIXED_CC if fixed_cc is None else fixed_cc
    order = {r.lower(): i for i, r in enumerate(DEFAULT_CC_ROLES if cc_roles is None else cc_roles)}
    cc_roles = set(order)
    contact = (row.get("contact_email") or "").strip()
    to = [{"name": row.get("contact_name") or "", "email": contact, "role": "Contact"}] if contact else []
    cc, seen = [], {contact.lower()} if contact else set()

    def add(name, email, role, checked):
        email = (email or "").strip()
        if not email or email.lower() in seen:
            return
        seen.add(email.lower())
        cc.append({"name": name or "", "email": email, "role": role, "checked": bool(checked)})

    admin = (row.get("super_admin_email") or "").strip()
    add(row.get("super_admin_name"), admin, "Super admin", True)
    for addr in fixed_cc:
        add("", addr, "Always copied", True)
    # configured roles first, in the configured order; everyone else after, alphabetically
    for m in sorted(team or [], key=lambda t: (order.get(t["role"].lower(), len(order)), t["role"], t["name"])):
        add(m["name"], m["email"], role_label(m["role"]), m["role"].lower() in cc_roles)
    missing = []
    if not contact:
        missing.append("The request has no contact email")
    if not team:
        missing.append("No Salesforce account team found" if row.get("sf_account_id") else "No Salesforce account matched yet")
    return {"to": to, "cc": cc, "missing": missing}


def valid_email(value):
    return bool(_EMAIL_RE.match((value or "").strip()))


# ── links ───────────────────────────────────────────────────────────────────

def _q(value):
    return urllib.parse.quote(value or "", safe="")


def _query(params):
    return "&".join(f"{k}={_q(v)}" for k, v in params if v)


def mailto_link(to, cc, subject, body):
    return "mailto:" + ",".join(to) + "?" + _query([("cc", ",".join(cc)), ("subject", subject), ("body", body)])


def outlook_link(to, cc, subject, body):
    """Compose deep link for Outlook on the web (opens a new draft in the signed-in mailbox)."""
    return "https://outlook.office.com/mail/deeplink/compose?" + _query(
        [("to", ",".join(to)), ("cc", ",".join(cc)), ("subject", subject), ("body", body)])
