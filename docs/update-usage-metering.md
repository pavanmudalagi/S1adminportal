Updated SentinelOne Usage Metering & API Guide

1. Purpose & Audience

This guide explains how to pull usage and entitlement data from SentinelOne for MSSP
billing, chargeback, and internal reporting, and how to combine the Usage Metering
PowerQuery API with legacy MGMT APIs and example automation (Postman & PowerShell).

2. Which APIs to Use

2.1 Usage Metering Platform (PowerQuery metering datasource)

Use the Usage Metering platform when you need normalized, billable usage for products
that are already onboarded to metering (EDR, CWS, Identity, SDL, Log Analytics,
Hyperautomation, etc.).

Key characteristics:

-  Accessed via PowerQuery API against the metering datasource.
-  Returns canonical usage metrics that align with SentinelOne billing.
-  Supports daily/periodic aggregations and standard filters (account, site, date range,

etc.).

-  Each report is SKU‑specific (endpoints vs users vs bytes vs actions).

2.2 Legacy MGMT APIs

Use MGMT APIs when:

-  A SKU is not yet represented in the Usage Metering platform (especially some legacy

add‑ons).

-  You need configuration context, such as which bundles or modules/add‑ons are

applied at the account or site level.

-  You need raw device lists or detail beyond what metering exposes.

Key characteristics:

-  REST APIs exposed from the Management console: Help → API Hub → MGMT API /

API Doc.
-  Best suited for:

-  Listing primary bundles and add‑on modules (e.g., RSO, ROF, Network

Discovery, Vulnerability Mgmt).

-  Pulling raw endpoint/device lists for detailed inventory.

Recommended pattern for full billing alignment:

1.  Use Usage Metering for quantitative per‑SKU counts (agents, users, bytes, actions).
2.  Use MGMT APIs to discover which SKUs/modules are enabled where.
3.  Combine counts + configuration to build a complete billing model.

3. SKUs Covered in the Usage Metering Platform

The Usage Metering platform exposes one or more reports per product/SKU via the
metering datasource.

3.1 Core Metering Reports (from Usage Metering Guide)

Product(s) Covered

New Metering API Report Name

Data Ingest – SDL Unified Data Lake

xdr_ingested_bytes

Hyperautomation

hyper_automate_workflow_actions_r
an

Log Analytics

log_analytics_ingested_bytes

SDL/Data Ingest Long‑Range Queries

ltdr_queries

Cloud Native Security Foundations/Pro

cns_workloads

Cloud Workload Security (Container Hosts)

container_hosts

Cloud Workload Security (Serverless
Containers)

serverless_containers

Cloud Workload Security (Servers)

server_endpoints

Threat Detection for Datastores

threat_detection_s3

Threat Detection for NetApp

windows_storage_endpoints

Singularity Mobile

enrolled_mobile_devices

Product(s) Covered

New Metering API Report Name

EDR for Workstations (Core, Control,
Complete)

workstation_endpoints

Identity Detection and Response (ITDR)

singularity_identity_endpoints

Identity Security Posture Management /
Identity for IdPs

singularity_identity_users

3.2 MSSP‑Focused Mapping & Units

From the MSSP license usage note (summarized):

-  EDR Workstations

-  Report: workstation_endpoints
-  Unit: agents (snapshot of installed & not decommissioned agents)

-  CWS – Servers/VMs

-  Report: server_endpoints
-  Unit: agents
-  CWS – Container Hosts

-  Report: container_hosts
-  Unit: agents
-  CWS – Serverless

-  Report: serverless_containers
-  Unit: agents (EKS Fargate pods / ECS Fargate tasks)

-

Identity – IDR Endpoints

-  Report: singularity_identity_endpoints
-  Unit: agents (max deployed & not decommissioned)

-

Identity – Users (ISPM / Identity for IdPs)

-  Report: singularity_identity_users
-  Unit: users (snapshot of active on‑prem AD users)

-  AI SIEM / SDL Ingest

-  Report: xdr_ingested_bytes
-  Unit: bytes

-  Log Analytics

-  Report: log_analytics_ingested_bytes
-  Unit: bytes
-  Hyperautomation

-  Report: hyper_automate_workflow_actions_ran
-  Unit: actions

CWS licensing gotchas (MSSP‑specific):

-  Customers can use multiple CWS SKUs; each workload type bills separately.
-  Expect separate charges for:

-  Servers/VMs (server_endpoints)
-  Container Hosts (container_hosts)
-  Serverless (serverless_containers)

-

If a tenant is only paying for workstation licenses but deploying on servers or containers,
correct CWS licensing will increase billing to match coverage.

4. Using the Usage Metering Platform via PowerQuery API

4.1 Prerequisites

Before calling the PowerQuery API for metering:

-  Use the correct console/region (US, EU, APAC, etc.).
-  Obtain an API token with:

-  Metering Reports → View permission.
-  Appropriate scope (Global or Account).

-  Review API Hub / PowerQuery documentation in your console or the Usage Metering

reports in the PowerQuery API KB.

4.2 Endpoint & Headers

PowerQuery metering endpoint (per console KB):

None

POST https://<console_hostname>/sdl/api/powerQuery
Authorization: Bearer <API_TOKEN>
Content-Type: application/json

Where <API_TOKEN> is your SDL / PowerQuery token with Metering Reports → View.

4.3 Basic Query Patterns (PowerQuery syntax)

All examples below assume the metering datasource.

4.3.1 List all available usage metering reports

None

| datasource 'metering' from 'reports'
| columns report_name, product_category, product_name, unit, description

4.3.2 Pull a specific report by Account Name

None

organization_level_1_name == '<Account Name>'
| datasource 'metering' from '<Report Name>'
| columns endpoint_bundle,
         value,
         organization_level_1_name,
         organization_level_1_scope_level,
         organization_level_1_scope_id,
         organization_level_2_name,
         organization_level_2_scope_level,
         organization_level_2_scope_id

4.3.3 Pull a specific report by Account ID

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from '<Report Name>'
| columns endpoint_bundle,
         value,
         organization_level_1_name,
         organization_level_1_scope_level,
         organization_level_1_scope_id,
         organization_level_2_name,
         organization_level_2_scope_level,
         organization_level_2_scope_id

4.3.4 Pull a report for a specific Site (by Account & Site Name)

None

organization_level_1_name == '<Account Name>' AND
organization_level_2_name == '<Site Name>'
| datasource 'metering' from '<Report Name>'
| columns endpoint_bundle,
         value,

         organization_level_1_name,
         organization_level_1_scope_level,
         organization_level_1_scope_id,
         organization_level_2_name,
         organization_level_2_scope_level,
         organization_level_2_scope_id

4.3.5 Pull a report for a specific Site (by Account & Site ID)

None

organization_level_1_scope_id == '<Account ID>' AND
organization_level_2_scope_id == '<Site ID>'
| datasource 'metering' from '<Report Name>'
| columns endpoint_bundle,
         value,
         organization_level_1_name,
         organization_level_1_scope_level,
         organization_level_1_scope_id,
         organization_level_2_name,
         organization_level_2_scope_level,
         organization_level_2_scope_id

5. Device Counts (EDR, CWS) & Detailed Device Lists

You can think of device information in two tiers:

1.  Billing‑level counts via Usage Metering (workstations, servers, workloads).
2.  Per‑device detail via MGMT APIs (/agents, /assets, /accounts, /sites).

5.1 Billing Device Counts via Usage Metering

5.1.1 Workstation endpoint counts by site

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'workstation_endpoints'
| columns endpoint_bundle,
         value,
         organization_level_1_name,

         organization_level_2_name,
         organization_level_2_scope_id

-  endpoint_bundle – bundle/SKU name (e.g., Core, Control, Complete).
-  value – count for the period (typically active endpoints).

5.1.2 Server endpoint counts (CWS Servers)

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'server_endpoints'
| columns endpoint_bundle,
         value,
         organization_level_1_name,
         organization_level_2_name,
         organization_level_2_scope_id

Other device‑oriented reports include:

-  cns_workloads – Cloud Native Security workloads.
-  container_hosts – Container host counts.
-  serverless_containers – Serverless container counts.
-  enrolled_mobile_devices – Singularity Mobile devices.

5.2 Detailed Device Lists via MGMT APIs

5.2.1 Agents API – per‑endpoint detail

None

GET /web/api/v2.1/agents?siteIds=<Site ID>&isActive=true

-  Returns a JSON array of agents/endpoints for the given site.
-  Can be filtered by accountIds, OS type, group, active status, etc.
-  Published rate limit: 25 RPS per API token/IP.

5.2.2 Accounts & Sites – licensing/entitlement context

None

GET /web/api/v2.1/accounts/<Account ID>
GET /web/api/v2.1/sites/<Site ID>

Focus on:

-  License bundles – primary SKUs at account/site.
-  Modules – add‑on SKUs (e.g., Network Discovery, Threat Intelligence, Vuln Mgmt).
-  accountType / siteType – Paid vs Trial.
-  activeAgents / activeLicenses – summarized endpoint counts (context only; prefer

metering).

6. Storage Ingestion Usage (SDL / AI SIEM, Log Analytics,
Long‑Range Queries)

"Storage ingestion usage" typically refers to Unified Data Lake ingest volume and related
query counts.

6.1 Key storage‑related reports

From the Usage Metering guide:

-  xdr_ingested_bytes – total bytes ingested into Unified Data Lake (native +

3rd‑party).

-  log_analytics_ingested_bytes – bytes attributed to Log Analytics usage.
-  ltdr_queries – long‑range query counts (Long‑Term Data Retention).

6.2 Example: Unified Data Lake ingest by account and day

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'xdr_ingested_bytes'
| columns organization_level_1_name,
         organization_level_2_name,
         organization_level_2_scope_id,
         value,

         timestamp

Example aggregation to sum bytes per day:

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'xdr_ingested_bytes'
| group by organization_level_1_name, date_trunc("day", timestamp)
| agg sum(value) as total_ingested_bytes

6.3 Example: Log Analytics ingest for a specific site

None

organization_level_1_scope_id == '<Account ID>' AND
organization_level_2_scope_id == '<Site ID>'
| datasource 'metering' from 'log_analytics_ingested_bytes'
| columns organization_level_1_name,
         organization_level_2_name,
         value,
         timestamp

Use these metrics to reconcile AI SIEM / SDL usage bills vs internal dashboards (Usage
Metering, SDL Usage, etc.).

7. Identity Usage (IDR Endpoints & Identity Users)

Identity usage spans two main concepts:

1.  Identity Detection & Response endpoints (ITDR) –

singularity_identity_endpoints

2.  Identity posture / AD users (ISPM / Identity for IdPs) –

singularity_identity_users

7.1 Identity usage via Usage Metering

7.1.1 IDR – protected endpoints

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'singularity_identity_endpoints'
| columns endpoint_bundle,
         value,
         organization_level_1_name,
         organization_level_2_name,
         organization_level_2_scope_id

-  value – count of endpoints with Identity protection features enabled/billed.

7.1.2 Identity Security Posture / Identity for IdPs – active AD users

None

organization_level_1_scope_id == '<Account ID>'
| datasource 'metering' from 'singularity_identity_users'
| columns value,
         organization_level_1_name,
         organization_level_2_name,
         organization_level_2_scope_id

-  Usage is based on active AD users, not endpoints.

If your internal AD export shows different active user counts, guidance is to export actual AD
users via PowerShell, reconcile OU filters and disabled/service accounts, and then compare to
singularity_identity_users output.

7.2 Identity‑specific MGMT APIs

There is a dedicated identity API surface under:

None

/web/api/v2.1/identity/*

-  Rate limit: 50 RPS, burst 100 per published API limits.

-  Use for detailed identity entities and relationships (users, groups, exposures) and

correlating posture with endpoint usage, but treat Usage Metering as source of truth
for billing.

8. Legacy & Non‑Metered Add‑ons via MGMT APIs

For SKUs that do not appear in the metering reports catalog, or for legacy add‑ons, use
MGMT APIs to determine:

-  Which primary bundles are applied at Account/Site.
-  Which modules/add‑ons are enabled per Site.

Example MGMT pattern to enumerate modules such as Remote Script Orchestration,
RemoteOps Forensics, Network Discovery, Vulnerability Management and then combine
them with metering counts for the underlying meters (workstation/server endpoints, SDL ingest,
etc.).

9. Why You Can’t Do "All in One Call" Today

From the MSSP working note:

-  Each PowerQuery statement like:

None

| datasource 'metering' from '<report_name>'

is bound to one report, so you must run one query per SKU (EDR, CWS, Identity
endpoints, Identity users, SDL, Log Analytics, Hyperautomation, etc.).

-  Different reports measure different units (agents vs users vs bytes vs actions).

-  Some add‑ons only exist as MGMT modules (no metering report), so you must still call
/web/api/v2.1/accounts and /web/api/v2.1/sites to see where these are
enabled and then combine that with metering data.

Accurate, billing‑aligned pattern:

1.  One PowerQuery call per metered product/report.
2.  Optional MGMT calls to enrich with legacy/non‑metered modules.
3.  Merge results into a single CSV/report per account.

10. Postman collection & PowerShell script examples

These example metering calls produce a single merged CSV per account.

NOTE: Only xdr_ingested_bytes, log_analytics_ingested_bytes, and
hyper_automate_workflow_actions_ran have first‑class usage meters today.

RSO, ROF, Network Discovery, and Vulnerability Management are modules you must discover
via MGMT API (licenses.modules) and then combine with the underlying meters
(workstation/server endpoints, etc.).

Example Postman collection (add‑ons + modules)

Includes:

-  New metering calls: xdr_ingested_bytes, log_analytics_ingested_bytes,

hyper_automate_workflow_actions_ran

-  One MGMT call to enumerate RSO/ROF/Network Discovery/Vuln Mgmt modules per

site

POSTMAN Collection

Import this JSON as a collection. It includes your original 4 calls plus 3 new metering calls and 1
MGMT call.

Use environment vars:

-  {{console_hostname}} – e.g. usea1-yourtenant.sentinelone.net
-  {{api_token}} – SDL / PowerQuery token (Bearer)
-  {{mgmt_api_token}} – MGMT token (ApiToken header)
-  {{account_id}} – Account ID (organization_level_1_scope_id)

JSON

{

  "info": {
    "name": "S1 Usage Metering \u2013 EDR+CWS+Identity+AddOns",
    "_postman_id": "11111111-2222-3333-4444-555555555555",
    "schema":
"https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "EDR \u2013 workstation_endpoints",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from 'workstation_endpoints'\\n|
columns endpoint_bundle, value, organization_level_1_name,
organization_level_1_scope_id, organization_level_2_name,
organization_level_2_scope_id\",\n  \"startTime\": \"31d\"\n}"
        }
      }
    },
    {
      "name": "CWS \u2013 server_endpoints",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],

          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from 'server_endpoints'\\n| columns
endpoint_bundle, value, organization_level_1_name,
organization_level_1_scope_id, organization_level_2_name,
organization_level_2_scope_id\",\n  \"startTime\": \"31d\"\n}"
        }
      }
    },
    {
      "name": "Identity \u2013 singularity_identity_endpoints (IDR endpoints)",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from
'singularity_identity_endpoints'\\n| columns endpoint_bundle, value,
organization_level_1_name, organization_level_1_scope_id,
organization_level_2_name, organization_level_2_scope_id\",\n  \"startTime\":
\"31d\"\n}"
        }
      }
    },
    {
      "name": "Identity \u2013 singularity_identity_users (user counts)",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },

          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from
'singularity_identity_users'\\n| columns value, organization_level_1_name,
organization_level_1_scope_id, organization_level_2_name,
organization_level_2_scope_id\",\n  \"startTime\": \"31d\"\n}"
        }
      }
    },
    {
      "name": "SDL \u2013 xdr_ingested_bytes (Unified Data Lake ingest)",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from 'xdr_ingested_bytes'\\n|
columns value, organization_level_1_name, organization_level_1_scope_id,
organization_level_2_name, organization_level_2_scope_id\",\n  \"startTime\":
\"31d\"\n}"
        }
      }
    },
    {
      "name": "Log Analytics \u2013 log_analytics_ingested_bytes",

      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from
'log_analytics_ingested_bytes'\\n| columns value, organization_level_1_name,
organization_level_1_scope_id, organization_level_2_name,
organization_level_2_scope_id\",\n  \"startTime\": \"31d\"\n}"
        }
      }
    },
    {
      "name": "Hyperautomation \u2013 hyper_automate_workflow_actions_ran",
      "request": {
        "method": "POST",
        "header": [
          { "key": "Authorization", "value": "Bearer {{api_token}}", "type":
"text" },
          { "key": "Content-Type", "value": "application/json", "type": "text"
}
        ],
        "url": {
          "raw": "https://{{console_hostname}}/sdl/api/powerQuery",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["sdl", "api", "powerQuery"]
        },
        "body": {
          "mode": "raw",
          "raw": "{\n  \"query\": \"organization_level_1_scope_id ==
'{{account_id}}'\\n| datasource 'metering' from
'hyper_automate_workflow_actions_ran'\\n| columns value,
organization_level_1_name, organization_level_1_scope_id,

organization_level_2_name, organization_level_2_scope_id\",\n  \"startTime\":
\"31d\"\n}"
        }
      }
    },
    {
      "name": "MGMT \u2013 Sites & modules (RSO/ROF/Network Discovery/Vuln
Mgmt)",
      "request": {
        "method": "GET",
        "header": [
          { "key": "Authorization", "value": "ApiToken {{mgmt_api_token}}",
"type": "text" }
        ],
        "url": {
          "raw":
"https://{{console_hostname}}/web/api/v2.1/sites?accountIds={{account_id}}&site
Type=Paid",
          "protocol": "https",
          "host": ["{{console_hostname}}"],
          "path": ["web", "api", "v2.1", "sites"],
          "query": [
            { "key": "accountIds", "value": "{{account_id}}" },
            { "key": "siteType", "value": "Paid" }
          ]
        },
        "description": "Inspect response. Under each site's licenses.modules,
look for add-ons like Remote Script Orchestration, RemoteOps Forensics, Network
Discovery, Vulnerability Management, then combine with metering counts from
workstation_endpoints/server_endpoints/xdr_ingested_bytes."
      }
    }
  ]
}

Example PowerShell script (meters + note on modules)

This extends $meters to include the three add‑on meters. RSO/ROF/Network Discovery/Vuln
Mgmt are handled via MGMT APIs, so we added a small helper you can call separately.

None

# ---- These are examples only please use and test at your own risk ----

param(
    [Parameter(Mandatory = $true)]
    [string]$ConsoleHostname, # e.g. usea1-example.sentinelone.net

    [Parameter(Mandatory = $true)]
    [string]$ApiToken, # Bearer token with Metering Reports -> View

    [Parameter(Mandatory = $true)]
    [string]$AccountId, # organization_level_1_scope_id

    [string]$MgmtApiToken, # optional: MGMT ApiToken for modules
    [int]$DaysBack = 31,
    [string]$OutFile =
".\s1-usage-$((Get-Date).ToString('yyyyMMdd-HHmmss')).csv"
)

# ---- helper: call PowerQuery for a given report ----
function Invoke-S1PowerQuery {
    param(
        [string]$ReportName
    )
    $uri = "https://$ConsoleHostname/sdl/api/powerQuery"

    $query = @"
organization_level_1_scope_id == '$AccountId'
| datasource 'metering' from '$ReportName'
| columns endpoint_bundle, value,
organization_level_1_name, organization_level_1_scope_id,
organization_level_2_name, organization_level_2_scope_id
"@

    $body = @{
        query = $query
        startTime = "${DaysBack}d"
    } | ConvertTo-Json -Depth 5

    $headers = @{
        Authorization = "Bearer $ApiToken"
        "Content-Type" = "application/json"
    }

    $resp = Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body
$body

    $cols = $resp.columns.name
    $rows = $resp.values

    $objects = foreach ($row in $rows) {
        $obj = [ordered]@{}
        for ($i = 0; $i -lt $cols.Count; $i++) {
            $obj[$cols[$i]] = $row[$i]
        }
        [pscustomobject]$obj
    }
    return $objects
}

# ---- meters covered by Usage Metering ----
$meters = @(
    @{ ReportName = 'workstation_endpoints'; Product = 'EDR Workstations'; Unit
= 'agents' },
    @{ ReportName = 'server_endpoints'; Product = 'CWS Servers'; Unit =
'agents' },
    @{ ReportName = 'singularity_identity_endpoints'; Product = 'Identity – IDR
Endpoints'; Unit = 'agents' },
    @{ ReportName = 'singularity_identity_users'; Product = 'Identity – Users';
Unit = 'users' },
    @{ ReportName = 'xdr_ingested_bytes'; Product = 'SDL – Data Ingest'; Unit =
'bytes' },
    @{ ReportName = 'log_analytics_ingested_bytes'; Product = 'Log Analytics';
Unit = 'bytes' },
    @{ ReportName = 'hyper_automate_workflow_actions_ran'; Product =
'Hyperautomation – Actions'; Unit = 'actions' }
)

$all = @()

foreach ($m in $meters) {
    Write-Host "Querying report '$($m.ReportName)' for account $AccountId..."
-ForegroundColor Cyan
    $rows = Invoke-S1PowerQuery -ReportName $m.ReportName
    foreach ($r in $rows) {
        $all += [pscustomobject]@{
            Product = $m.Product
            ReportName = $m.ReportName
            Unit = $m.Unit
            AccountName = $r.organization_level_1_name
            AccountId = $r.organization_level_1_scope_id
            SiteName = $r.organization_level_2_name
            SiteId = $r.organization_level_2_scope_id
            EndpointBundle = $r.endpoint_bundle
            Value = [int64]$r.value
        }
    }

}

$all | Sort-Object Product, SiteName | Tee-Object -Variable Usage |
Format-Table -AutoSize
Write-Host "`nWriting CSV to $OutFile" -ForegroundColor Green
$Usage | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8

# ---- OPTIONAL: pull site modules for RSO/ROF/Network Discovery/Vuln Mgmt ----
if ($MgmtApiToken) {
    Write-Host "`nQuerying MGMT API for site modules (RSO/ROF/Network
Discovery/Vuln Mgmt)..." -ForegroundColor Yellow
    $sitesUri =
"https://$ConsoleHostname/web/api/v2.1/sites?accountIds=$AccountId&siteType=Pai
d"
    $sitesHeaders = @{ Authorization = "ApiToken $MgmtApiToken" }
    $sitesResp = Invoke-RestMethod -Method Get -Uri $sitesUri -Headers
$sitesHeaders

    $moduleNamesOfInterest = @('Remote Script Orchestration', 'RemoteOps
Forensics', 'Network Discovery', 'Vulnerability Management')

    $siteModules = foreach ($s in $sitesResp.data) {
        foreach ($mod in $s.licenses.modules) {
            if ($moduleNamesOfInterest -contains $mod.displayName) {
                [pscustomobject]@{
                    AccountId = $s.accountId
                    SiteId = $s.id
                    SiteName = $s.name
                    ModuleName = $mod.displayName
                    ModuleSku = $mod.name
                    SiteType = $s.siteType
                }
            }
        }
    }

    $siteModules | Sort-Object SiteName, ModuleName | Format-Table -AutoSize
    $modulesOutFile =
".\s1-modules-$((Get-Date).ToString('yyyyMMdd-HHmmss')).csv"
    Write-Host "Writing module map CSV to $modulesOutFile" -ForegroundColor
Green
    $siteModules | Export-Csv -Path $modulesOutFile -NoTypeInformation
-Encoding UTF8
}

11. Troubleshooting Highlights

Common issues captured in the Usage Metering guide:

-  Authentication / RBAC

-  401/403 errors or results only working in API Doc "Try It".
-  Check you’re using a token from the correct console & scope with required

permissions (Metering Reports → View, SDL Usage → View, SDL Ingestion API
permissions where relevant).

-  Rate limiting (429)

-

Implement client‑side throttling / exponential backoff and batch queries
across time windows or scope sets.

-  Region / endpoint mismatch

-  Ensure you’re using the correct regional base URL and that firewalls allow

outbound TCP 443 to documented SentinelOne domains for management,
metering, identity, and SDL endpoints.

-  No data for a given report

-  Use the reports catalog query to confirm the report exists.
-  Verify the SKU is deployed and billable (check licenses.bundles /

licenses.modules).

-  Check time window vs metering retention.


