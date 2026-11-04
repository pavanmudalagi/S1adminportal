# Provisioning Prompt Security via the Management API

Sources: "Prompt Security Provisioning & Usage — MSSP Walkthrough" (SentinelOne MSSP partner guide)
and the POST /accounts schema served by the Staging console (usea1-nabl-staging, 2026-11), which
includes `promptFields` {promptRegion, promptTenantName, promptUserName, promptUserEmail, promptUsers[]}.
The Swagger copy in `docs/swagger_2.1.md` predates this and lacks it. Auth: `Authorization: ApiToken <token>`.

## Endpoints

| Action | Method and path |
|---|---|
| Create account | `POST /web/api/v2.1/accounts` |
| Update account | `PUT  /web/api/v2.1/accounts/{account_id}` |
| Create site | `POST /web/api/v2.1/sites` |
| Update site | `PUT  /web/api/v2.1/sites/{site_id}` |
| Read (before any PUT) | `GET /web/api/v2.1/accounts/{id}` / `GET /web/api/v2.1/sites/{id}` |

Usage (billing): the existing metering / PowerQuery API (`docs/update-usage-metering.md`).

## Request additions (all wrapped in `{"data": {...}}`)

| Field | Required | Notes |
|---|---|---|
| `licenses.bundles[].name` | yes | **`prompt_employees`** (Prompt for Employees) or **`prompt_for_ai_code_assistants`** (Prompt for AI Code Assistants) — confirmed on live Staging accounts (2026-11). The guide's `ai_security_employees_usage` / `ai_security_developers_usage` are the *metering* report names; sent as bundle names the API answers "Bundle is not available in your scope" |
| `licenses.bundles[].surfaces[]` | yes | `{"name": "Total Employees", "count": N}` or `{"name": "Total Developers", "count": N}`; `-1` = unlimited (MSSP accounts and their sites only) |
| `promptFields.promptTenantName` | yes | unique; letters, numbers, `-`, `_` (guide says lowercase; the API accepts capitals — a live tenant is `PavanTestAccount`); permanent |
| `promptFields.promptRegion` | **no — do not send** | In the POST schema (enum `useast` \| `eunorth` \| `apsouth` \| `apsoutheast`, no `uswest`), but Staging rejects it: *"prompt_region field is not available"* (2026-11). The platform assigns the region from the console; see "Prompt region" below |
| `promptFields.promptUserName` + `promptUserEmail` | yes* | one first admin |
| `promptFields.promptUsers[]` | yes* | up to 10 `{promptUserName, promptUserEmail}`, emails unique |
| `usageType` | accounts | `mssp` for the partner account |

\* Send either the single-admin pair or `promptUsers`, never both (rejected).

Tenant naming: account `<account_name>`; site `<account_name>-<site_name>`.

## Prompt region (`promptFields.promptRegion`)

**Platform-assigned — not sent.** The POST schema lists it with enum `useast`, `eunorth`, `apsouth`,
`apsoutheast` (the S1 region group; no `uswest`), and sending an invalid value fails that enum check —
but sending a valid one fails too: *"prompt_region field is not available"*. Existing accounts return
`promptRegion: null`. The table below is therefore informational: where a tenant ends up. US West is
where the platform *hosts* US MSSP partner tenants (and the customer accounts/sites beneath them).

| Country | S1 cloud region | Cluster | `promptRegion` (sent) | Hosted at (MSSP) | Hosted at (Customer) |
|---|---|---|---|---|---|
| USA | us-east-1 | aprod-use1-s1 | `useast` | uswest.prompt.security | useast.prompt.security |
| Canada | ca-central-1 | aprod-cac1-s1 | `useast` | useast.prompt.security | useast.prompt.security |
| Germany (EU) | eu-central-1 | aprod-euc1-s1 | `eunorth` | eunorth.prompt.security | eunorth.prompt.security |
| Indonesia (Singapore) | ap-southeast-1 | aprod-apse1-s1 | `apsouth` | apsouth.prompt.security | apsouth.prompt.security |
| — (Mumbai) | ap-south-1 | aprod-aps1-s1 | `apsouth` | apsouth.prompt.security | apsouth.prompt.security |
| Australia (Sydney) | ap-southeast-2 | aprod-apse2-s1 | `apsoutheast` | apsoutheast.prompt.security | apsoutheast.prompt.security |

A US MSSP account mistakenly set to `customer` is hosted in US East and does not appear in US West;
only Support can fix it. Code: `licenses.prompt_region(base_url)` (value to send, from the cluster
token `use1`/`cac1`/… in the console hostname) and `licenses.prompt_console(region, s1_region, usage_type)`
(where it is hosted, for display).

## Rules that affect the code

- `GET /accounts/{id}` returns `promptFields` (tenant name, the single-user pair **and** `promptUsers[]`,
  `promptRegion` — `null` on accounts created before the field existed).
- A PUT replaces the scope's license list: GET first, add the bundles to the existing bundles **and**
  modules/settings, send the full lists back.
- One Prompt tenant per account/site. `promptFields` is only needed the first time; once a tenant exists,
  bundles and counts can change without it. Re-sending the **stored values exactly** is accepted;
  **different values return 400** ("Prompt fields cannot be changed").
- A site can only get a Prompt bundle if its parent account already has it.
- Partner account must be `usageType: mssp`; a `customer` account lands in US East and its sites can fail
  ("This scope is not eligible to receive Prompt AI").
- Tenant name and first users cannot be edited or cleared afterwards. Removing the SKU does not delete the tenant.
- Provisioning is asynchronous (5–20 min, no progress shown), and no welcome email is sent.

## Errors seen

- `Bundle is not available in your scope because it is not available in the parent scope` — add to parent first.
- `All prompt fields are required when Prompt SKU is used` — bundle sent without complete `promptFields`.
- `This scope is not eligible to receive Prompt AI` — wrong usage type.
- 400 on update — changed `promptFields` values.

## Examples

Add Prompt to an existing account (full license lists plus `promptFields`):

```json
PUT /web/api/v2.1/accounts/<ACCOUNT_ID>
{"data": {
  "licenses": {
    "bundles": [
      {"name": "complete", "surfaces": [{"name": "Total Agents", "count": -1}]},
      {"name": "prompt_employees", "surfaces": [{"name": "Total Employees", "count": -1}]}
    ],
    "modules": [ "...existing..." ],
    "settings": [ "...existing..." ]
  },
  "promptFields": {
    "promptTenantName": "example_msp",
    "promptUsers": [{"promptUserName": "Alex Admin", "promptUserEmail": "alex.admin@example.com"}]
  }
}}
```

Create a site: `POST /sites` with `accountId`, `name`, `siteType`, `inheritAccountExpiration`, `licenses`, `promptFields`.

## Usage / metering identifiers (uqlRef)

`ai_security_employees_usage`, `ai_security_developers_usage` (daily seat counts: aggregate with **max**),
`ai_security_homegrown_app_calls`, `ai_security_agentic_ai_usage`, `ai_security_red_teaming_usage`
(usage counts: **sum**). Match to customers with `account_scope_id` / `site_scope_id`; data for day X is
available after ~12:00 UTC on X+1 (schedule loaders for 13:00 UTC or later). Needs a console user token with
Metering Reports > View; SDL keys don't work.

```
| datasource "metering" from "ai_security_employees_usage"
| group seats=max(value) by account_scope_id, site_scope_id
```
