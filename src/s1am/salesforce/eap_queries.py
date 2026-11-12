"""Batched SOQL for the EAP tracker, in the style of the dashboard's product_lines queries.

One statement per rule per sync across every pending request: IN lists are chunked by
client.query_in, LIKE rules are OR-chained in smaller chunks. Nothing is queried per row.

Field names were verified with describe() against the N-Able org on 2026-11-12.
Tenant__c.Product__c is a lookup, so the product is Product__r.Name. Every tenant named
1000_… is an EDR product, so candidate tenants need no product filter.
"""
from . import client as sf
from ..eap.forms import normalize_company

TENANT_FIELDS = ("Id, Name, Account__c, Account__r.Name, TenantStatus__c, Product__r.Name, "
                 "BillableCurrentQty__c, CurrentTermEndDate__c")
ACCOUNT_FIELDS = "Id, Name, BillingCountry, Customer_ID__c"
CONTACT_FIELDS = "Id, Name, Email, AccountId, Account.Name"

# TenantDetail__c.ProductServiceName__c values that are agent seats. "Licensed Endpoints"
# is the pre-rename Complete line (390 of 4,000 sampled rows); it counts as Complete.
AGENT_SERVICES = {
    "Singularity Complete": "complete",
    "Licensed Endpoints": "complete",
    "Licensed Endpoints - Control": "control",
}
WEBMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "msn.com", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "yandex.com",
}
LIKE_CHUNK = 40


def _like_escape(value):
    return (str(value).replace("\\", "\\\\").replace("'", "\\'")
            .replace("%", "\\%").replace("_", "\\_"))


def _or_like(select, field, patterns, chunk=LIKE_CHUNK, tail=""):
    rows = []
    unique = [p for p in dict.fromkeys(patterns) if p]
    for part in sf.chunked(unique, chunk):
        where = " OR ".join(f"{field} LIKE '{p}'" for p in part)
        rows += sf.run_soql(f"{select} WHERE ({where}){tail}", use_cache=False)
    return rows


def tenants_by_id(ids):
    return sf.query_in(f"SELECT {TENANT_FIELDS} FROM Tenant__c WHERE Id IN ({{values}})", ids, use_cache=False)


def tenants_by_name(names):
    return sf.query_in(f"SELECT {TENANT_FIELDS} FROM Tenant__c WHERE Name IN ({{values}})", names, use_cache=False)


def tenants_by_account_id_suffix(account_ids):
    """Tenants whose name ends in _<S1 account id> (1000_<CODE>_<id>)."""
    return _or_like(f"SELECT {TENANT_FIELDS} FROM Tenant__c", "Name",
                    [f"1000\\_%\\_{_like_escape(i)}" for i in account_ids])


def contacts_by_email(emails):
    return sf.query_in(f"SELECT {CONTACT_FIELDS} FROM Contact WHERE Email IN ({{values}}) AND AccountId != NULL",
                       [e.strip() for e in emails if e], use_cache=False)


def contacts_by_domain(domains):
    doms = [d.lower() for d in domains if d and d.lower() not in WEBMAIL_DOMAINS]
    return _or_like(f"SELECT {CONTACT_FIELDS} FROM Contact", "Email",
                    [f"%@{_like_escape(d)}" for d in doms], chunk=20, tail=" AND AccountId != NULL LIMIT 2000")


def accounts_by_name(companies):
    cores = {normalize_company(c) for c in companies if c}
    return _or_like(f"SELECT {ACCOUNT_FIELDS} FROM Account", "Name",
                    [f"%{_like_escape(c)}%" for c in sorted(cores) if len(c) >= 3], chunk=20, tail=" LIMIT 2000")


def accounts_by_id(ids):
    return sf.query_in(f"SELECT {ACCOUNT_FIELDS} FROM Account WHERE Id IN ({{values}})", ids, use_cache=False)


def tenants_for_accounts(account_ids):
    return sf.query_in(f"SELECT {TENANT_FIELDS} FROM Tenant__c WHERE Account__c IN ({{values}}) "
                       f"AND Name LIKE '1000\\_%'", account_ids, use_cache=False)


def agent_counts(tenant_ids):
    """{tenant Id: {"complete", "control", "total"}} summed from TenantDetail__c.

    ProductServiceName__c cannot be GROUP BY'd in SOQL, so rows are summed here."""
    rows = sf.query_in(
        f"SELECT Tenant__c, ProductServiceName__c, CurrentQty__c FROM TenantDetail__c "
        f"WHERE Tenant__c IN ({{values}}) AND IsDeleted = false "
        f"AND ProductServiceName__c IN ({sf.soql_in_list(AGENT_SERVICES)})",
        tenant_ids, use_cache=False)
    out = {}
    for r in rows:
        kind = AGENT_SERVICES.get(r.get("ProductServiceName__c"))
        if not kind:
            continue
        counts = out.setdefault(r["Tenant__c"], {"complete": 0, "control": 0, "total": 0})
        qty = int(r.get("CurrentQty__c") or 0)
        counts[kind] += qty
        counts["total"] += qty
    return out
