# SAP Portal → JI: the SAP plumbing

> Django app: `sap_client` · Lookups at `/api/v1/sap-lookups/`
> Source: SAP Portal (`backend_v1`, Node/Express), snapshot of the live server
> tree committed as `efa29d8` on its `snapshot/server-2026-09-26` branch.

SAP Portal talked to the same SAP as JI (same HANA host, same Service Layer
host and service user, same three company databases), so nothing on the SAP
side moves. What moves is behaviour: the reads and writes the portal's screens
used, ported into `sap_client` so the apps built for the merge
(`sap_finance`, `sap_documents`, `sap_approvals`, `bom_changes`,
`partner_onboarding`) go through the one door to SAP.

## What was added

| Piece | File | Replaces in the portal |
|--|--|--|
| Master-data lookups | `hana/lookup_reader.py`, `views_lookups.py`, `urls_lookups.py` | `/api/sap/lookup/*`, `/api/{customers,vendors}/lookup/*`, `next-cardcode` |
| Service Layer client with a session cache | `service_layer/entity_client.py` | `sapRequest` (one cached cookie per company) |
| Business partners | `service_layer/business_partner_writer.py` | `createCustomer` / `createVendor` |
| BOMs | `service_layer/product_tree_writer.py` | `pushBomToSap` |
| Budget UDO | `service_layer/budget_writer.py` | `/api/sap/budget*` |
| Production-order release / close | `service_layer/production_order_writer.py` | `/production-order/:id/{release,close}` |
| Decide with a typed password; withdraw | `service_layer/approval_writer.py` | `sapRequestAs` + `/approval-requests/:id{,/cancel}` |
| Attachment downloads | `service_layer/file_service_client.py` | `services/fileServiceClient.js` |
| "Is this request really pending?" | `approval_status.py` | `effectiveOwddStatus` / `effectiveStatusSql` |
| Sandbox settings | `config/sap_sandbox_settings.py` | `npm run start:test` + `.env.test` |
| Finance reads (journal entries, ledger, chart of accounts) | `hana/finance_reader.py` | `/api/sap/journal-entries`, `/gl-ledger`, `/chart-of-accounts` |
| Production orders of every status, with issued/received totals | `hana/production_order_reader.py` | `/api/sap/production-orders*` |
| Issue for / receipt from production | `service_layer/production_movement_writer.py` | `/api/sap/issue-production`, `/receipt-production` |

All of it goes through `SAPClient` (one method per read/write), so views patch
`SAPClient` in their own module exactly as the existing queues do.

## Differences from the portal, on purpose

* **Every lookup uses the caller's company.** The portal's customer and vendor
  lookups always read the default company (Oil).
* **Values are bound, never formatted into SQL**, and OData keys and filters
  are quoted with `odata_string()`. The portal hand-escaped search text into
  SQL and put BOM codes into `ProductTrees('<code>')` unescaped.
* **A failed read raises.** The portal fell back to the Service Layer and, for
  tax codes, to a hard-coded list. The two user-defined tables `@MAIN_GROUP`
  and `@CHAIN` stay fail-soft because not every company has them.
* **No invented card codes.** The portal made one up from the clock when SAP
  couldn't be read. `next_card_code()` raises instead.
* **401/403 from SAP is a refusal (400), not "SAP unavailable".**
  `ServiceLayerEntityClient` logs in again once on 401 (a cached session can
  expire) and treats a second refusal as SAP's answer.

## Signing approvals with a typed password

JI's queues sign a decision as the stage's own authorizer with the password
held in `SAP_APPROVER_CREDENTIALS` ([sap_identities.md](sap_identities.md)).
SAP Portal instead asked the approver for their own SAP password on every
decision. The general approvals inbox keeps the portal's way for the approvers
it brought over; the writer now accepts either:

* `decide(..., approver=code, password=typed)` — the typed password signs this
  one call. It is never stored, logged or cached; SAP checks it.
* `decide(..., approver=code)` — as before, the stored password.

Either way the caller must still **be** that authorizer
(`SapApproverIdentity`); a typed password does not let anyone act as someone
else. `cancel()` withdraws a pending request as its originator, with the
value the portal proved live: `Status = "arsCancelled"`.

`ServiceLayerEntityClient(..., cache_session=False)` exists for any future
call made with typed credentials, so a later call cannot ride an earlier
session instead of proving the password again.

## Lookups API

`GET /api/v1/sap-lookups/<name>/` — login and `Company-Code` only (names and
codes, like `/api/v1/po/vendors/`). Each answers a JSON list except
`next-card-code`.

| Name | Query | Reads |
|--|--|--|
| `items` | `search` (2+ chars), `limit` | OITM |
| `sac-codes` | `search` | OSAC |
| `locations` | `search` | OLCT (active) |
| `warehouses` | — | OWHS (active) |
| `tax-codes` | — | OSTC (unlocked) |
| `costing-codes` | `dimension` 1–5, `search` | OPRC (unlocked) |
| `branches` | — | OBPL (enabled) |
| `resources` | `search` | ORSC |
| `batches` | `item_code`, `warehouse` | OIBT (via the batch-stock reader) |
| `gl-accounts` | `search` | OACT (postable) |
| `ar-accounts` / `ap-accounts` | — | OACT under 1101000 / 2101000 + 211*, not cash |
| `business-partners` | `search`, `type` C/S | OCRD (active) |
| `bp-groups` | `type` C/S | OCRG |
| `sales-employees` | — | OSLP (unlocked, > 0) |
| `payment-terms` | — | OCTG |
| `states` | `country` (IN) | OCST |
| `banks` | `country` (IN) | Service Layer `Banks` |
| `main-group` / `chain` | — | `@MAIN_GROUP` / `@CHAIN` (empty if missing) |
| `next-card-code` | `type` C/S, `prefix` (CUSTA / VENDA) | OCRD max + 1 |

## Settings

| Setting | Default | Purpose |
|--|--|--|
| `SAP_FILE_SERVICE_BASE_URL` | empty (downloads off) | the portal's `FILE_SERVICE_BASE` |
| `SAP_FILE_SERVICE_TIMEOUT_SECONDS` | 60 | |
| `SAP_FILE_SERVICE_COMPANY_ID_{JIVO_OIL,JIVO_BEVERAGES,JIVO_MART}` | 1, 2, 3 | the file service's own ids |

None of these is a secret. Set `SAP_FILE_SERVICE_BASE_URL` in the server's
`.env` to the portal's `FILE_SERVICE_BASE` value to switch downloads on.

## Not ported

* **Reading attachments straight off the share** (`smbclient` fallback). It
  needs a system package plus share credentials on this server, and the file
  service is the portal's primary path. Downloads fail with a clear message
  when the file service can't serve a file.
* **Setting Complete/Reject on a receipt from production by updating SAP's
  `IGN1` table directly.** JI never writes SAP tables. If the Service Layer
  accepts a transaction type on receipt lines, add it to the receipt payload.
* **Re-deciding a request that is already decided** — ported since, for the
  SAP approvals inbox only: `ApprovalRequestWriter.decide(change=True)` accepts
  an approved or rejected request and the other decision. Every other caller
  is still pending-only. See `sap_approvals/docs/README.md`.

## Verify on the sandbox before relying on it

Run with `--settings=config.sap_sandbox_settings` (real SAP hosts from `.env`,
`JIVO_OIL` → `TEST_JIVO_OIL_HANADB`, the other two companies refused):

1. `close_production_order` sends `boposClosed`; the portal sent `'L'`.
2. `withdraw_approval_request` sends `arsCancelled` (proven by the portal) —
   confirm on the sandbox too.
3. `ProductTrees` PUT replaces the lines of an existing BOM.
4. `BUDGET` create / PATCH with `B1S-ReplaceCollectionsOnPatch` / delete.
5. `BusinessPartners` create with the payload `partner_onboarding` builds.
6. HANA reads on the sandbox schema need a grant for JI's HANA user.
7. `decide_approval_request(change=True)`: change an approved request to
   rejected and back, and see whether SAP accepts each change and what it does
   to `OWDD.Status` and the `WDD1` line. Never proven against SAP yet.

## Tests

`sap_client/tests_sap_portal.py` — the lookup reader (bound parameters, card
codes, fail-soft user tables), the pending-status rule with the portal's own
cases, the entity client (session reuse, re-login, refusal vs outage, write
timeouts), each writer's request, the typed-password and withdraw paths, the
download client (raw, ZIP, compressed fallback, latin-1 names) and the lookups
API. Everything SAP-side is mocked.
