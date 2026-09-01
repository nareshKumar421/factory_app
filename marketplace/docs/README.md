# Marketplace (Flipkart / Amazon fulfilment) — Backend

Django REST app: `marketplace` (in `C:/Users/gurpa/dev/factory_app/marketplace`).

> Frontend counterpart: [`FactoryFlow/docs/modules/marketplace.md`](../../../FactoryFlow/docs/modules/marketplace.md)

*Verified against code on 2026-07-14. Where this doc and the historical design note
`MARKETPLACE_FLIPKART_SHEET_FLOW.md` disagree, the code (and this README) win.*

---

## Overview — what it does & who uses it

The marketplace app runs the **outbound fulfilment and returns** of orders placed on
Flipkart / Amazon out of a company godown (the Mayapuri pilot). It exists because
**sales billing for these channels does not live in SAP**. The app therefore:

- produces an **internal (non-SAP) billing document** in JI for every dispatched order, while
- **still decrementing SAP stock** through a SAP **Delivery Note** (finished goods) plus a
  **Goods Issue** (packing material) at confirm time.

It is anchored on the marketplace **Order ID** (not a SAP invoice), and is modelled on the
existing `gate_core.sales_dispatch` scan-and-confirm pattern.

Users:
- **Fulfilment operators** — scan finished goods at Outward, scan returns at Inward.
- **Warehouse / godown team** — approve, issue and receive the consolidated stock a batch needs.
- **Packing team** — generate & print item barcodes, mark an order packed.
- **Marketplace admins / managers** — maintain masters (SKU→FG, combos, warehouses) and read the
  reconciliation report.

The whole module is **company-scoped** (every model has a `company` FK) and every endpoint is
**permission-gated** (custom Django permissions bundled into a `Marketplace` auth group).

---

## Key concepts & entities

Defined in `models.py`. All business models extend `gate_core.models.BaseModel`
(`created_at/updated_at/created_by/updated_by/is_active`).

### Master data
- **`MarketplaceWarehouse`** — links a channel to the SAP godown (`sap_warehouse_code`) it ships
  from, plus the SAP business partner (`sap_customer_card_code`) used as the Delivery Note
  `CardCode`. Unique on `(company, channel, sap_warehouse_code)`. *(There are no series/tax/GI
  fields on this model — see the "dead warehouse fields" gap below.)*
- **`ComboDefinition`** + **`ComboComponent`** — a **JI-authored sales BOM** (SAP sales-BOM
  replacement). A combo expands into component lines, each typed **FG** (finished good) or **PM**
  (packing material), with a per-combo-unit `quantity`. `ComboDefinition.image` exists on the model
  but is **not exposed by the serializer**.
- **`SkuMapping`** — maps a marketplace SKU (FSN / ASIN) to either a **RAW** finished good
  (`fg_item_code`) or a **COMBO** (`combo` FK). Unique on `(company, channel, marketplace_sku)`.
  A SKU with no active mapping is **unmapped** and blocks downstream steps. `SkuMapping.image` also
  exists on the model but is not serialized.

### Orders
- **`MarketplaceOrder`** — the anchor entity. Unique on `(company, channel, order_id)`.
  Status `OPEN → DISPATCHED / RETURNED / PARTIAL`. Sheet-imported orders carry an `import_batch`
  FK plus Flipkart columns (shipment id, address, `dispatch_by`, `tracking_id`, `is_cancelled`).
- **`MarketplaceOrderLine`** — one SKU line (`ordered_quantity` + captured sheet fields:
  `fsn`, `unit_price`, `invoice_amount`, `tax_amount`, `raw_row` JSON snapshot).

### Sheet import → warehouse issue
- **`OrderImportBatch`** — one uploaded Flipkart CSV, and one independent scanning session.
  Owns its own copy of every order in the sheet, keeps the raw file, and owns the issue
  request(s). Status
  `PARSED → RESOLVED → REQUESTED → ISSUED → DISPATCHING → CLOSED`.
- **`MarketplaceIssueRequest`** + **`MarketplaceIssueLine`** — a request to a warehouse to issue
  the batch's consolidated stock list. Mirrors `warehouse.BOMRequest`: per-line
  `required / available / approved / issued / received` with partial approval.
  Status `DRAFT → SENT → APPROVED / PARTIALLY_APPROVED / REJECTED → ISSUED → RECEIVED`.

### Packing
- **`MarketplacePacking`** — one-to-one with an order. `PENDING → PACKING → PACKED`.
  An order is **only dispatchable once PACKED**.
- **`MarketplacePackBarcode`** — a unique, printable **`PACK-<date>-<orderpk>-<seq>`** barcode,
  one per finished-good line. At Outward it resolves to its `item_code` + `quantity` so a single
  scan can complete a line.

### Dispatch (Outward) + returns (Inward)
- **`MarketplaceDispatch`** — outward session for one order. `DRAFT → SCANNING → READY →
  CONFIRMED / CANCELLED`. Carries the SAP DN doc entry/num, the linked internal billing, and the
  SAP-post outcome **`sap_post_status` (`PENDING / POSTED / FAILED / AWAITING_APPROVAL /
  NOT_REQUIRED`) + `sap_error`**. `NOT_REQUIRED` is a repeat of an order already shipped on an
  earlier sheet — scanned and confirmed here, but no second delivery note; `dn_covered_by` points
  at the dispatch whose note issued the stock.
- **`MarketplaceScan`** — one FG scan. **Unique on `(dispatch, barcode_raw)`** — this is how a
  duplicate scan is detected.
- **`MarketplaceReturn`** — inward session. `DRAFT → SCANNING → SUBMITTED / CANCELLED`.
  `internal_credit_doc_num` holds the **Return Note** number (`RTN-…`).
- **`MarketplaceReturnScan`** — unique on `(mp_return, barcode_raw)`.
- **`MarketplaceReturnPhoto`** — a model for condition-evidence photos (1..N per return scan)
  **that is not wired up**: there is no serializer, no view/endpoint, and it is not even registered
  in `admin.py`. Today it is a schema-only stub — nothing in the API or UI can create or read a
  return photo. Treat "returns with photos" as *planned, not implemented*.

### Internal billing
- **`MarketplaceOrderBilling`** — the JI-side (non-SAP) invoice, number
  **`MKT-YYYYMMDD-NNNNN`** (globally unique). Records the SAP DN doc entry/num for traceability
  but is itself never posted to SAP.

---

## End-to-end flows

The module supports a **sheet-driven pipeline** (the real Flipkart flow) plus lighter manual
entry. The six-step pipeline mirrors the frontend `MpFlowSteps` bar (Upload → Review & map →
Send to warehouse → Issue & receive → Pack → Dispatch).

### 1. Import a Flipkart order sheet
`order_import_service.ingest` (`POST /orders/import/`).
1. Parse the CSV (`parse_rows`) — headers matched case/space-insensitively; requires at least
   `Order Id`, `SKU`, `Quantity` or raises `BAD_SHEET`.
2. Group rows by `Order Id`; create **this batch's** `MarketplaceOrder` rows + their lines.
   **Every order in the sheet is imported.** Uniqueness is per `(company, channel, order_id,
   import_batch)`, so an order already present on an EARLIER sheet gets a second row here —
   its own lines, its own dispatch, its own scans. Nothing is moved, refreshed, hidden or
   skipped, and the earlier sheet stays exactly as it was. `skip_duplicates` is still accepted
   on the endpoint and **ignored**.
3. Rows whose `Order State` contains "cancel" (for **all** rows of an order) mark it `is_cancelled`;
   the order still gets status `OPEN`, so a later re-approval sheet carries a clean row for it.
4. `OrderImportPreviewView` (`/orders/import/preview/`) runs `analyze()` — a **dry run** reporting
   how many orders were seen on an earlier sheet + unmapped SKUs, writing nothing. Informational
   only: the counts do not change what is imported. (`duplicate_*` keeps its name for API
   compatibility and now means "also on an earlier sheet".)

**Each sheet is an independent scanning session.** The same order can read `CONFIRMED` on the
sheet it shipped on and `PENDING` on a newer one, and be scanned and confirmed again there. The
one thing not repeated is the **SAP delivery note**: the goods left inventory once, so confirming
the repeat marks it `sap_post_status=NOT_REQUIRED` with `dn_covered_by` pointing at the note that
did move them (`confirm_service._already_shipped_elsewhere`); the bulk cut skips it too.

Because a re-listed parcel carries the same Tracking ID on every sheet it appears on, the Outward
scan endpoints take an optional **`batch_id`** — the sheet being worked — so a scan lands on that
sheet's copy rather than the newest one. Return scans instead resolve to the row that actually
shipped (one holding a `CONFIRMED` dispatch).

### 2. Build the consolidated stock list
`batch_resolve_service.build_stock_list` (`GET /batches/<id>/stock-list/`). Runs `resolve_order`
across every non-cancelled order in the batch and aggregates the resolved component lines by
`(item_code, component_type)` into one FG/PM list. Mappings are loaded once for the whole batch.

**Resolution rule** (`resolve_service.resolve_order`):
- RAW SKU → one FG line, `qty = ordered_quantity`.
- COMBO SKU → one line per component, `qty = ordered_quantity × component.quantity`.
- SKUs with no active mapping are returned in `unmapped_skus` and **block** issue-request creation,
  packing and dispatch confirm.

### 3. Send to warehouse
`issue_request_service.create_from_batch` (`POST /issue-requests/`). Snapshots best-effort on-hand
stock (`warehouse.services.warehouse_service.WarehouseService`, `{}` if SAP unreachable), creates a
`SENT` request with one line per stock-list line, and moves the batch to `REQUESTED`.

### 4. Warehouse review → issue → receive
- `review` — per-line approve / partial / reject; approved qty must be `>0` and `≤ required`.
  Rolls the request up to `APPROVED / PARTIALLY_APPROVED / REJECTED`.
- `issue` — copies `approved_qty → issued_qty`, moves the request to `ISSUED` and the batch to
  `ISSUED`. **Decision A**: issue is an internal picking/accountability record — it posts **no SAP
  document**; SAP stock is decremented later, once, by the dispatch Delivery Note. (`_post_issue`
  is the single seam where a Decision-B stock transfer would go.)
- `receive` — records `received_qty` (defaults to `issued_qty`), moves to `RECEIVED`.

### 5. Packing
`packing_service` (`/packing/*`).
1. `start_or_get` — opens a packing session; requires the order's batch to be issued
   (`order_is_issued`) or raises `NOT_ISSUED`.
2. `generate_barcodes` — one `PACK-…` barcode per FG line (idempotent; blocks on unmapped SKUs;
   raises `EMPTY` if the order has no FG lines). Moves the session to `PACKING`.
3. `complete` — requires barcodes, moves to `PACKED`. The order is now dispatchable in Outward.

### 6. Outward dispatch + confirm
- `POST /dispatches/` — requires the order to be **PACKED** (`order_is_packed`, else `NOT_PACKED`).
  Reuses the latest non-cancelled dispatch if one exists (so re-opening an order is safe).
- `POST /dispatches/<id>/scans/` → `scan_service.record_dispatch_scan`:
  - A `PACK-…` barcode resolves to its `item_code` + `quantity`.
  - The item must match an FG line of the resolved order, else **`ITEM_NOT_ON_ORDER` (400)**.
  - A repeat `barcode_raw` returns the existing scan with **`duplicate: true` (HTTP 200)**.
  - Exceeding the line's required quantity raises **`OVER_SCAN` (400)**.
  - Status advances to `SCANNING`, then `READY` when every FG line is `COMPLETE`.
  - **Operators scan FG only**; packing materials are consumed automatically at confirm.
- `POST /dispatches/<id>/confirm/` → `confirm_service.confirm_dispatch`:
  1. Guards: not already confirmed/cancelled, order not marketplace-cancelled, order **packed**,
     no **unmapped SKUs**, and (unless `override_deviation`) no **scan deviation** vs the order.
  2. In one transaction, marks the dispatch `CONFIRMED` and the order `DISPATCHED` — **this always
     persists, even if SAP is down**.
  3. Best-effort SAP post (`_try_post_delivery_note` → `_post_delivery_note`): `verify_stock` →
     **Delivery Note** (FG) → **Goods Issue** (PM) → create the **internal billing** doc. On success
     `sap_post_status=POSTED`; on **any** exception `sap_post_status=FAILED` + `sap_error`, without
     rolling back the dispatch.
- `POST /dispatches/<id>/retry-delivery-note/` → `retry_delivery_note` re-attempts the SAP post for
  a `CONFIRMED` dispatch whose post is not yet `POSTED`.
- `POST /dispatches/<id>/cancel/` → cancels a non-confirmed dispatch (a `CONFIRMED` one cannot be
  cancelled → `INVALID_STATE`).

### 7. Inward returns
- `POST /returns/` → open (or reuse) a return for an order.
- `POST /returns/<id>/scans/` → `record_return_scan` — same `PACK-…` resolution and
  duplicate / not-on-order handling as Outward; returned goods carry the same pack labels.
- `POST /returns/<id>/submit/` → `return_service.submit_return` assigns the **Return Note** number
  `RTN-YYYYMMDD-NNNNN`, marks the return `SUBMITTED`. **Internal only — posts nothing to SAP and
  moves no stock** (restocking is handled outside this flow). Idempotent.

### 8. Reconciliation
`reconciliation_service.build_report` (`GET /reconciliation/`). For each order with a confirmed
dispatch and/or a return, compares outward-scanned vs inward-scanned (`outward_vs_inward_deviation`)
and portal-ordered vs physical net-shipped = outward − inward (`portal_vs_physical_deviation`).

---

## Critical business rules & invariants

- **Internal billing, SAP stock.** A dispatch produces an internal `MarketplaceOrderBilling`
  (`MKT-…`) — never posted to SAP — while stock is decremented in SAP via the Delivery Note (FG)
  and Goods Issue (PM). (`confirm_service._post_delivery_note`)
- **SAP posting is best-effort and never blocks fulfilment.** Once the pre-conditions pass, the
  order is `DISPATCHED` regardless of SAP. A failed post is flagged `FAILED` + `sap_error`
  (truncated to 2000 chars) and is retryable. An SAP outage does not stop the floor.
- **Dispatch gate.** Only orders that came from a sheet (`import_batch` set) are gated: they must be
  **PACKED** to create/confirm a dispatch (`dispatch_gate.order_is_packed` /
  `packed_subquery`). Manual/legacy orders (no batch) are **not** gated and pass through.
- **Packing gate.** Packing can only start once the batch's issue request is `ISSUED`/`RECEIVED`
  (`order_is_issued`), else `NOT_ISSUED`.
- **Unmapped SKUs block** issue-request creation, barcode generation and confirm
  (`UNMAPPED_SKUS`).
- **Scan integrity.** Duplicate scan = unique-constraint hit → 200 `duplicate:true` (no error).
  Over-scan and off-order scans hard-fail (400). Deviating scan counts block confirm unless
  `override_deviation=True`.
- **Idempotency.** `confirm_dispatch` (already-CONFIRMED returns unchanged), `submit_return`,
  `generate_barcodes`, `complete`, and sheet `ingest` are all idempotent.
- **Invoice / note numbering** is per-company, per-day sequential:
  `MKT-YYYYMMDD-NNNNN` (billing) and `RTN-YYYYMMDD-NNNNN` (return note). Both count by
  `startswith(prefix)` on the day's documents.
- **Warehouse selection for the DN.** `_warehouse_for` picks the **first active**
  `MarketplaceWarehouse` for the channel (by id). None configured → `NO_WAREHOUSE` (surfaces as a
  failed post, not a dispatch block).
- **Simulate mode.** `settings.MARKETPLACE_SIMULATE_SAP` (`config('MARKETPLACE_SIMULATE_SAP',
  default=DEBUG)`) skips all SAP calls and returns synthetic `SIMDN-<ref>` / `SIMGI-<ref>` numbers,
  so scan→confirm can be demoed/tested without a live SAP. Production (`DEBUG=False`) posts for real.

---

## Integrations & cross-module boundaries

- **SAP write path** — `services/sap_gateway.MarketplaceSapGateway` wraps `sap_client.SAPClient`
  (`create_delivery_note`, `create_goods_issue`). The DN payload sends `CardCode`, `DocDate`, and
  `DocumentLines` (`ItemCode` / `Quantity` / `WarehouseCode`) **only** — no series or tax group. The
  GI payload sends `DocDate`, a `Comments` line, and the PM `DocumentLines`.
- **SAP stock read** — `warehouse.services.wms_hana_reader.WMSHanaReader` for `verify_stock`
  (best-effort; skipped if the reader / `get_available_stock` is unavailable) and
  `warehouse.services.warehouse_service.WarehouseService` for the issue-request on-hand snapshot.
- **SAP item / warehouse masters** — `production_execution.services.sap_reader.ProductionOrderReader`
  backs `GET /sap-items/` (item search for masters). The frontend reuses the PO warehouses endpoint
  to pick a real godown code.
- **Company scoping** — `company.permissions.HasCompanyContext` is required on every endpoint;
  `self.company = request.company.company`. There is no cross-company read/write here — everything is
  resolved from the single active company context (consistent with the repo's
  cross-company-flow-boundary rule: writes resolve the company from the record).
- **Barcode module** — packing labels follow the Barcode module `PREFIX-YYYYMMDD-…` convention and
  the frontend reuses its printable-label components.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

- **SAP down / rejecting at confirm.** Trigger: SAP unreachable or rejects the DN.
  Behaviour: order is still `DISPATCHED`; the exception is swallowed, `sap_post_status=FAILED`,
  `sap_error` stores the message (truncated to 2000 chars). Symptom: dispatch shows FAILED with a
  Retry button. Risk: **SAP stock is not decremented until someone retries** — stock can drift if
  the retry is forgotten. There is no automatic retry/queue.
- **Insufficient SAP stock (real mode).** Trigger: an item is short on-hand at confirm.
  Behaviour: `verify_stock` raises `INSUFFICIENT_STOCK` **inside** the best-effort post, so the
  order is already `DISPATCHED` and the post is flagged FAILED. Symptom: FAILED DN with a shortage
  detail. Risk: an order can dispatch physically while its SAP DN can never post until stock exists —
  reconciliation and SAP go out of step.
- **No warehouse configured for the channel.** Trigger: `MarketplaceWarehouse` missing/inactive.
  Behaviour: `NO_WAREHOUSE` raised inside the post → FAILED (dispatch still succeeds). Symptom:
  every confirm shows FAILED. Risk: silent until someone reads the error; masters must be seeded
  first.
- **Order cancelled on the marketplace after import.** Trigger: a re-imported sheet marks the order
  cancelled, or `order.is_cancelled` is true at confirm. Behaviour: confirm raises
  `ORDER_CANCELLED`. Re-import sets `RETURNED` only if the order was still `OPEN` — an
  already-dispatched order keeps its status. Symptom: operator cannot confirm a cancelled order.
- **Duplicate / re-scanned box.** Trigger: the same `PACK-…`/barcode scanned twice. Behaviour:
  unique constraint → the existing scan is returned with `duplicate:true` (HTTP 200), no new scan.
  Symptom: a "duplicate" toast; counts don't move. Low risk — this is the intended guard.
- **Over-scan.** Trigger: scanning more of an item than the order needs. Behaviour: `OVER_SCAN`
  (400) before any write. Symptom: error toast; scan rejected. Correct behaviour.
- **Order booked/packed after the batch was issued (late order).** Trigger: an order added to a
  batch that was already issued. Behaviour: `order_is_issued` keys off the **batch's** issue-request
  status, so any order on an issued batch is considered issued; packing/dispatch proceed. Risk: a
  late order can pass the gate without its own line having been physically issued — the consolidated
  stock list won't reflect it.
- **Partial approval / short issue.** Trigger: warehouse approves less than required. Behaviour:
  request becomes `PARTIALLY_APPROVED`; dispatch is **not** blocked by shortfall — the gate only
  checks PACKED, not issued quantity. Symptom: shortfall shows in Warehouse Insights; a dispatch can
  still confirm and over-draw SAP stock. Risk/gap: issued-vs-required is advisory only.
- **Unmapped SKU discovered late.** Trigger: a SKU with no mapping reaches packing/confirm.
  Behaviour: `UNMAPPED_SKUS` blocks the step. Symptom: operator is told to add the mapping in
  Masters first. Correct, but it stalls that order until masters are fixed.
- **Scan deviation at confirm.** Trigger: some FG lines under/over-scanned. Behaviour:
  `SCAN_DEVIATION` (409) with the deviating lines unless `override_deviation=True`. Symptom: confirm
  blocked; operator must scan the rest or explicitly override. Risk: override lets a mismatched
  shipment through (audited via `remarks`).
- **Return of a damaged item — no photo evidence.** Trigger: operator wants to attach a condition
  photo to a returned item. Behaviour: **not possible** — `MarketplaceReturnPhoto` has no endpoint.
  Symptom: the return records the item and a Return Note but no evidence. Risk: disputes over item
  condition cannot be substantiated from the system.

---

## Failure modes / what can break

- **SAP delivery-note posting fails** → dispatch is FAILED (see edge cases). Operator/manager
  notices a red FAILED badge; SAP stock is not moved until Retry succeeds.
- **`WMSHanaReader` / `WarehouseService` unavailable** → stock checks and on-hand snapshots silently
  degrade to no-op / empty. Symptom: no stock validation happens and "available" shows 0 — a
  dispatch can be confirmed with no stock guardrail.
- **Malformed / wrong CSV** → `BAD_SHEET` (empty sheet, or missing `Order Id`/`SKU`/`Quantity`).
  Symptom: import rejected with a clear message. Rows without an Order Id are silently skipped
  (counted in `summary.skipped`).
- **Combo referenced by a mapping is deleted** → `ProtectedError` → `PROTECTED` (409). Symptom:
  "Combo is referenced by a SKU mapping; unlink it first."
- **Large sheet against a remote DB** — `ingest` and `build_stock_list` are set-based (bulk
  create/update, mappings loaded once) to stay fast; a regression to per-order queries would make
  imports crawl (see the repo's performance-audit note on DRF N+1).

---

## Improvement opportunities & known gaps

- **No automatic SAP retry.** FAILED DNs rely on a human pressing Retry. A background retry
  (Celery/cron) would close the stock-drift window.
- **Dispatch gate ignores issued quantity.** The gate checks only PACKED, so a partially-approved or
  late-added order can dispatch and over-draw SAP stock. Consider gating on issued≥required.
- **Return photos are a schema-only stub.** `MarketplaceReturnPhoto` exists but has no serializer,
  view, URL or admin — so the advertised "returns with photos" cannot be captured or viewed. Either
  build the upload/read endpoints (+ a `MarketplaceReturnPhotoSerializer` nested on the return-scan
  serializer) or drop the model.
- **Combo / SKU reference images are unwired.** `ComboDefinition.image` and `SkuMapping.image` are
  real `ImageField`s (migration `0003`) but the serializers omit them — the pick-and-pack reference
  photo cannot be set or shown through the API.
- **Frontend sends warehouse fields the backend ignores.** The Masters warehouse form posts
  `sap_series`, `sap_tax_code` and `post_goods_issue`, but the model/serializer/gateway have **no
  such fields** — they are silently dropped. The "Also post packing-material Goods Issue" toggle has
  **no effect** (a Goods Issue always posts when PM lines exist), and no tax/series is applied to the
  DN. Either wire these through the gateway or remove them from the UI.
- **Return restocking is out of scope** — the Return Note moves no stock, so returned goods must be
  re-stocked manually/elsewhere.
- **`analyze` preview is not persisted** — a preview then a changed sheet at import time can diverge;
  the import re-parses independently.

---

## Permissions & roles

Custom permission codenames are declared in model `Meta.permissions` and bundled into the
**`Marketplace`** auth group by data migrations `0002`, `0004`, `0006`. `permissions.py` exposes a
`_perm()` DRF permission-class factory (checks `request.user.has_perm(codename)`). `views.MpBaseView`
applies `read_perms` on SAFE methods and `write_perms` otherwise, always with `IsAuthenticated` +
`HasCompanyContext`.

| Codename (`marketplace.…`) | Gates |
| --- | --- |
| `view_dispatch` / `add_dispatch` / `scan_dispatch` / `confirm_dispatch` / `cancel_dispatch` | Orders list, create dispatch, scan, confirm/retry, cancel |
| `view_return` / `add_return` / `submit_return` | View/scan returns, submit Return Note |
| `view_master` / `change_master` | Warehouses, SKU mappings, combos, SAP item search |
| `view_reconciliation` | Reconciliation report |
| `import_orders` / `view_batch` | Import sheets, view batches/stock list/export, view issue requests |
| `send_issue_request` / `review_issue_request` / `issue_materials` / `receive_issue` | Create / review-reject / issue / receive issue requests |
| `view_packing` / `pack_order` | Packing queue / open-generate-complete-print |

Note: `scan_dispatch` also guards the scan-**delete** endpoint; `import_orders` (not `view_batch`)
guards both import preview and import. Nav gating on the frontend keys off the `marketplace` module
prefix — see the paired frontend doc and the repo's "group perms vs frontend nav gating" note.

---

## Developer file map

### Backend (`C:/Users/gurpa/dev/factory_app/marketplace`)
- `models.py` — all entities, statuses, custom permissions.
- `views.py` — masters, orders, dispatch, returns, reconciliation APIViews + `MpBaseView`
  (permission/company base, pagination helper).
- `views_sheet.py` — sheet import, batch, issue-request, packing, warehouse-insights, SAP-item views.
- `urls.py` — full URL map (`/api/marketplace/…`).
- `serializers.py` / `serializers_sheet.py` — I/O serializers (List vs Detail; Detail computes
  resolved lines + scan progress).
- `permissions.py` — `_perm()` factory + `Can…` classes.
- `services/`
  - `resolve_service.py` — SKU/combo → FG/PM line expansion (`resolve_order`, `fg_lines`, `pm_lines`).
  - `order_import_service.py` — CSV parse + idempotent `ingest` / `analyze`.
  - `batch_resolve_service.py` — batch → consolidated stock list.
  - `issue_request_service.py` — create/review/reject/issue/receive (Decision A note in `_post_issue`).
  - `packing_service.py` — packing queue, barcode generation, label data, complete.
  - `scan_service.py` — scan capture + progress for dispatch and returns.
  - `confirm_service.py` — confirm dispatch + best-effort SAP DN/GI/billing + retry.
  - `return_service.py` — submit return → Return Note.
  - `sap_gateway.py` — SAP boundary + simulate mode.
  - `dispatch_gate.py` — issued/packed gates + `Exists` subqueries.
  - `reconciliation_service.py`, `warehouse_insights_service.py`, `issuance_export_service.py`.
  - `errors.py` — `MarketplaceError(message, code, status_code, detail)` → `{code, error, detail}`.
- `migrations/0002,0004,0006` — build the `Marketplace` group.
- `management/commands/seed_marketplace_demo.py` — idempotent demo masters + orders
  (`--company JIVO_MART`); `clear_marketplace_sheet_data.py` — wipe sheet-flow data.
- `admin.py` — registers masters/orders/dispatch/return/billing (note: return scans/photos are not
  registered).
- `tests.py`, `tests_sheet_flow.py` — flow tests (many under `MARKETPLACE_SIMULATE_SAP=True`).

### Key frontend files (`C:/Users/gurpa/dev/FactoryFlow/src/modules/marketplace`)
- `module.config.tsx` — routes + nav + permission gates.
- `api/marketplace.api.ts` / `api/marketplace.queries.ts` — endpoints + TanStack hooks.
- `pages/Mp*Page.tsx` — Overview, Import, BatchDetail, IssueRequests(+Detail), Packing, Outward,
  Inward, Masters, Reconciliation.
- `types/marketplace.types.ts` — DRF-mirroring types.

---

## Related docs
- Frontend: [`FactoryFlow/docs/modules/marketplace.md`](../../../FactoryFlow/docs/modules/marketplace.md)
- Code comments reference `MARKETPLACE_FLIPKART_SHEET_FLOW.md` for the sheet flow's original design
  decisions (Decision A/B); that design note is the historical companion to this README — trust the
  code here where they differ.
