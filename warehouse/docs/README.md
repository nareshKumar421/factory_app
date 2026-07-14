# Warehouse — Backend (BOM Issue Requests & Finished-Goods Receipt)

> Django app: `warehouse` · Base URL: `/api/v1/warehouse/`
> Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/warehouse.md`

The `warehouse` app is the store-side counterpart to Production. It owns two
workflows plus a read-only analytics surface:

1. **BOM Issue Requests** — production asks the warehouse store for the raw
   materials a run needs; the store reviews stock, approves/partially-approves,
   and posts a **Goods Issue** to SAP against the production order.
2. **Finished-Goods (FG) Receipt** — after a run completes and passes Final QC,
   the store receives the produced goods and posts a **Goods Receipt** to SAP.
3. **WMS read dashboards** (`/warehouse/wms/*`) — company-scoped, read-only SAP
   HANA queries powering stock, movement, transfer, batch-expiry, sales-backlog
   and billing screens.

> The same Django app also hosts **Branch Stock Transfer (BST)** (`models_bst.py`,
> `views_bst.py`, `services/bst_service.py`) — documented separately in
> [`warehouse/docs/bst.md`](./bst.md). **GRPO** is a *different* app (`grpo/`).
> This document covers BOM/FG + the WMS read layer only.

---

## Overview — what it does & who uses it

| Workflow | Actor | Outcome |
|----------|-------|---------|
| Submit BOM request | **Production** (run screen) | A `PENDING` `BOMRequest` with one `BOMRequestLine` per material, scaled to the run's `required_qty`. |
| Review / approve / reject | **Warehouse store** ("BOM & FG Store" group) | Line-level approved quantities, capped by live SAP stock. |
| Issue materials to SAP | Warehouse store | SAP `InventoryGenExits` (Goods Issue) against the production order; `issued_qty` tracked per line. |
| Create FG receipt | Production / warehouse | A `PENDING` `FinishedGoodsReceipt` for a **COMPLETED** run whose **Final QC** is approved/PASS. |
| Receive FG | Warehouse store | Receipt → `RECEIVED`. |
| Post FG to SAP | Warehouse store | SAP `InventoryGenEntries` (Goods Receipt); receipt → `SAP_POSTED`. |
| WMS dashboards | Anyone with `warehouse.can_view_bom_request` | Read-only stock/movement/billing analytics from SAP HANA. |

Everything is **company-scoped**: the acting company comes from
`request.company.company.code` (`HasCompanyContext`), and both `WarehouseService`
and `WMSHanaReader` are constructed with that `company_code`, which selects the
company's SAP schema / HANA connection.

---

## Key concepts & entities

Defined in `warehouse/models.py`:

- **`BOMRequest`** — one per production run (an active guard prevents duplicates,
  see rules). Fields: `company`, `production_run`, `sap_doc_entry` (SAP production
  order DocEntry), `required_qty` (units to produce), `status`,
  `material_issue_status`, `sap_issue_doc_entries` (JSON list of posted Goods
  Issues), `requested_by`, `reviewed_by`.
- **`BOMRequestLine`** — one per material. Key fields: `item_code`,
  `per_unit_qty`, `required_qty` (scaled = per-unit × run qty), `available_stock`
  (snapshot at review), `approved_qty`, `issued_qty`, `warehouse`, `uom`,
  **`base_line`** (WOR1 `LineNum`, used to link the SAP issue), `status`.
- **`FinishedGoodsReceipt`** — `produced_qty`, `good_qty` (produced − rejected),
  `rejected_qty`, `warehouse`, `posting_date`, `status`, `sap_receipt_doc_entry`,
  `sap_error`, `received_by`/`received_at`.

**Status enums**

- `BOMRequestStatus`: `PENDING → APPROVED | PARTIALLY_APPROVED | REJECTED`
- `BOMLineStatus`: `PENDING → APPROVED | REJECTED`
- `MaterialIssueStatus`: `NOT_ISSUED → PARTIALLY_ISSUED → FULLY_ISSUED`
- `FGReceiptStatus`: `PENDING → RECEIVED → SAP_POSTED`, plus `FAILED`

**Services**

- `WarehouseService` (`services/warehouse_service.py`) — all BOM/FG business
  logic and SAP write calls. Company-scoped; lazily resolves `Company` by code.
- `WMSHanaReader` (`services/wms_hana_reader.py`) — direct `hdbcli` HANA reads for
  the WMS dashboards. Each call opens and closes a fresh HANA connection.

---

## End-to-end flows

### Flow 1 — BOM issue request (create → approve → issue)

1. **Create** (`WarehouseService.create_bom_request`, `POST bom-requests/create/`).
   - Loads the `ProductionRun` (must belong to the company, must not be
     `COMPLETED`).
   - Rejects if an **active** request already exists (`PENDING`/`APPROVED`/
     `PARTIALLY_APPROVED`).
   - Builds lines. If the run has saved `material_usages`, lines come from that
     snapshot (`_build_bom_lines_from_material_usage`), and SAP components are
     fetched only for **metadata** (`warehouse`, `uom`, `base_line`); matching is
     by item code via a FIFO deque so repeated codes map in order. If SAP metadata
     can't be fetched, lines still build with `base_line = index`, blank warehouse.
     With no `material_usages`, it falls back to SAP components
     (`_build_bom_lines_from_sap_components`) and fails if none exist.
   - Persists the request + lines, sets `run.warehouse_approval_status = PENDING`,
     `run.required_qty`. Signal → notifies the `warehouse` auth group.
2. **Review** (`BOMRequestDetailAPI`). The detail endpoint enriches each line
   **in-memory** with live stock from `get_stock_for_items` (SAP `OITM×OITW`,
   summed across all warehouses) — `available_stock`, `available_qty`,
   `stock_warehouses`. The stored `available_stock` is not overwritten here.
3. **Approve** (`approve_bom_request`, `POST …/approve/`). Only a `PENDING`
   request. For each line the caller sends `{line_id, approved_qty, status}`.
   Server re-fetches stock (`_get_stock_for_lines`, sums `OnHand>0` across
   warehouses) and **rejects the whole request** (400) if any approved line's
   `approved_qty` is ≤ 0, exceeds `required_qty`, **or exceeds in-stock qty**.
   Overall status becomes `APPROVED` (all lines fully approved), else
   `PARTIALLY_APPROVED` (some approved), else `REJECTED`. `run.warehouse_approval_status`
   mirrors it. Signal → notifies the original requester.
4. **Reject** (`reject_bom_request`, `POST …/reject/`) — whole request → `REJECTED`
   with a reason; all lines set `REJECTED`.
5. **Issue to SAP** (`issue_materials_to_sap`, `POST …/issue/`). Only
   `APPROVED`/`PARTIALLY_APPROVED`. Issues either the caller's `{line_id, quantity}`
   lines (capped at `approved_qty − issued_qty`) or, by default, all approved
   lines' remaining qty. Logs into the SAP **Service Layer** and posts one
   `InventoryGenExits` with lines `{Quantity, BaseType: 202, BaseEntry: <order
   DocEntry>, BaseLine: <base_line>}` — item & warehouse are **derived by SAP** from
   the production order, so they are deliberately **not** sent. Adds
   `BPL_IDAssignedToInvoice` when the warehouse maps to a branch (`OWHS.BPLid`).
   On success, bumps `issued_qty`, appends to `sap_issue_doc_entries`, sets
   `material_issue_status` to `FULLY_ISSUED`/`PARTIALLY_ISSUED`. The whole method
   is `@transaction.atomic`, so a SAP failure rolls the qty updates back.

### Flow 2 — Finished-goods receipt (create → receive → post)

1. **Create** (`create_fg_receipt`, `POST fg-receipts/create/`).
   - Run is `select_for_update`-locked; must be `COMPLETED`.
   - **Gate:** requires an active `ProductionQCSession` of type `FINAL` with
     `workflow_status = APPROVED` and `overall_result = PASS`, else 400
     ("Final QC must be approved with PASS…").
   - `produced_qty = run.total_production`, `good_qty = produced − rejected_qty`.
     Item/warehouse pulled from the SAP order header if not supplied.
   - **Idempotency:** if a locked/received receipt already exists → 400. Otherwise
     it **reuses** the single editable `PENDING` receipt and **deletes duplicate
     pending rows** (guards against double-submit), else creates a new one.
2. **Receive** (`receive_finished_goods`, `POST …/receive/`). Only `PENDING`/
   `FAILED`. Guards against a sibling receipt already received for the run and
   deletes duplicate pendings. Sets `RECEIVED`, `received_by/at`.
3. **Post to SAP** (`post_fg_receipt_to_sap`, `POST …/post-to-sap/`). Only
   `RECEIVED`/`FAILED`. Requires `sap_doc_entry`, `item_code`, `warehouse`, and
   `good_qty > 0`. Posts one `InventoryGenEntries` line `{Quantity, BaseType: 202,
   BaseEntry: <order>, BaseLine: 0}` (+ branch id). On success →
   `SAP_POSTED`, stamps `sap_receipt_doc_entry`, and mirrors the run's
   `sap_receipt_doc_entry`/`sap_sync_status = SUCCESS`. Signal → notifies the
   warehouse group (`FG_RECEIPT_POSTED` / `FG_RECEIPT_FAILED`).

### Flow 3 — WMS read dashboards

`views_wms.py` → `WMSHanaReader`. Each endpoint builds a parameterised HANA query
and returns JSON. All are `IsAuthenticated + HasCompanyContext` only (no
per-feature permission). See the API surface below.

---

## Critical business rules & invariants

- **One active BOM request per run.** Creation blocks if a `PENDING`/`APPROVED`/
  `PARTIALLY_APPROVED` request exists.
- **Approval is capped by live SAP stock.** `approved_qty` may not exceed
  `required_qty` **or** current on-hand (summed across warehouses with `OnHand>0`).
  Any violating line rejects the entire approve call.
- **Issue never exceeds approval.** Per line, issue qty ≤ `approved_qty − issued_qty`.
- **FG requires COMPLETED run + approved Final QC (PASS).** No receipt otherwise.
- **FG good qty is derived, not entered.** `good_qty = total_production − rejected_qty`.
- **SAP derives item/warehouse from the order.** Both Goods Issue and Goods
  Receipt use `BaseType = 202` and send only quantities + base-line links; SAP
  resolves item, warehouse and batch from the production order.
- **Company isolation.** Every queryset filters `company=self.company`; the SAP
  schema is chosen by company code. There is no cross-company read/write here.
- **Idempotency guards** on FG create/receive delete duplicate `PENDING` rows and
  block double-processing of an already-received/posted run.
- **State machines are one-way.** Only `PENDING` BOM requests are approvable/
  rejectable; FG posting is only from `RECEIVED`/`FAILED`.

---

## Integrations & cross-module boundaries

- **SAP Service Layer (writes).** `sap_client.client.SAPClient` →
  `context.service_layer` for login; direct `requests` POSTs to
  `InventoryGenExits` (issue) and `InventoryGenEntries` (FG). `verify=False`, a
  fresh login per call, 10s login / 30s post timeouts.
- **SAP HANA (reads).** `production_execution.services.sap_reader.ProductionOrderReader`
  for BOM/stock (`OITM`, `OITW`, `OWHS`), and `WMSHanaReader` for the dashboards
  (`OITM/OITW/OITB`, `OINM`, `OWTR/WTR1`, `OBTN/OBTQ`, `ORDR/RDR1`, `OPDN/PDN1`).
- **Production Execution.** `ProductionRun` is the anchor. This app writes back
  `warehouse_approval_status`, `required_qty`, `sap_receipt_doc_entry`,
  `sap_sync_status`. Mutations invalidate `['production-execution']` on the client.
- **Quality Control.** `ProductionQCSession` (FINAL/APPROVED/PASS) gates FG receipt.
- **Notifications.** `warehouse/notifications.py` + `signals.py` fire on
  `transaction.on_commit` to the `warehouse` group (BOM created, FG posted/failed)
  and to the requester (BOM reviewed).
- **Overlap with Barcode & GRPO.** GRPO (separate `grpo` app) also posts SAP goods
  receipts against POs; BST (this app) moves physical boxes and shares the
  `barcode` box/pallet models. FG receipt here is *production output*, not PO/
  transfer receipt — keep the three distinct when tracing "goods receipt" issues.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Stock is in a different warehouse than the BOM line.** → Approval caps by
   **total** on-hand across all warehouses, but SAP issues from the *order's*
   warehouse. → Approve succeeds, then **SAP issue fails** ("insufficient stock in
   WhsCode…"). → *Gap:* approval stock check is company-wide, not warehouse-scoped.

2. **Stock drops between review and issue.** → Approval snapshot is stale; issue
   re-hits SAP live. → "SAP issue failed" 400 at the Issue step. → Re-approve is
   impossible (request no longer `PENDING`); operator is stuck until stock returns
   or the request is worked around.

3. **BOM built from the production snapshot without SAP metadata.** → SAP fetch
   failed at create; `base_line` falls back to row index and `warehouse` is blank.
   → Issue posts with a guessed `BaseLine` → SAP rejects or issues against the
   wrong WOR1 line. → *Risk:* silent mis-linking of the goods issue.

4. **FG post fails at SAP (down / validation reject).** → `post_fg_receipt_to_sap`
   sets `FAILED` + `sap_error` then **re-raises inside `@transaction.atomic`**, so
   that write is **rolled back**. → The row stays **`RECEIVED`** (not `FAILED`),
   `sap_error` is empty, and **no `FG_RECEIPT_FAILED` notification fires**; the
   operator only sees a transient error toast and simply retries. → *Bug/gap:* the
   intended failure state never persists (see Failure modes).

5. **Double-submit of an FG receipt** (re-scan / retry). → Create/receive delete
   duplicate `PENDING` rows and block if any sibling is already received. → Second
   attempt 400s cleanly ("already received"). → Safe; no duplicate SAP post.

6. **SAP created the doc but the HTTP response was lost** (30s timeout). → Django
   raises and rolls back its own state, but SAP already holds the Goods Issue/
   Receipt. → Retry posts a **second** SAP document. → *Risk:* double issue/receipt;
   there is no idempotency key.

7. **Missing item code or warehouse on FG.** → Post pre-validates and 400s ("Missing
   item code or warehouse"). → Operator sees the error; must fix the source order
   linkage. → Safe fail-fast.

8. **Final QC not yet approved.** → FG create 400s. → "Final QC must be approved
   with PASS…". → Correct gate; occasionally confusing when QC is approved but not
   the *active* FINAL session.

9. **WMS dashboard with no warehouse filter on a large company.** → Stock overview
   pulls the **entire** `OITM×OITW` set into Python and paginates in memory; the
   dashboard runs 5 full aggregations. → Slow first load / high memory. → Filtering
   by warehouse is the intended mitigation (see Improvement opportunities).

---

## Failure modes / what can break

- **SAP Service Layer login fails** (creds/network). Issue → 400 "SAP login failed".
  FG → *intends* to mark `FAILED` but the atomic rollback discards it, leaving
  `RECEIVED`.
- **SAP validation reject** (e.g. `SBO_SP_TransactionNotification` `(2000xx)` rules,
  gross-weight/batch requirements). Surfaces as the SAP message in the 400. These
  come from SAP, not this code.
- **FG `FAILED` state is effectively unreachable** through `post_fg_receipt_to_sap`
  because the `FAILED` save is rolled back by the wrapping `@transaction.atomic`
  when the method re-raises. Consequence: the "Failed" filter/badge rarely appears,
  the `FAILED` notification path rarely fires, and operators distinguish failures
  only by the toast. **This is the single most load-bearing gap in the module.**
- **HANA unavailable.** `WMSHanaReader._execute` raises `SAPConnectionError`/
  `SAPDataError`; the WMS views return HTTP 500 with `{"error": …}`.
- **No connection pooling.** Every WMS query opens+closes a HANA connection; under
  load this is a latency/observability cost (see the prod-server memory note re:
  "slow API" = DRF/SAP round-trips, not infra).
- **BOM/FG SAP paths have no automated tests.** `warehouse/tests.py` covers **BST
  only**; the atomic/rollback and SAP-payload behaviour above is unverified, so
  regressions there are silent.

---

## Improvement opportunities & known gaps

- **Fix FG failure persistence.** Persist `FAILED`/`sap_error` outside the rolled-
  back transaction (e.g. mark failure in a separate atomic block or after the SAP
  call outside `@transaction.atomic`) so the badge, retry affordance, and
  `FG_RECEIPT_FAILED` notification work.
- **Warehouse-scope the approval stock check.** Cap `approved_qty` by the BOM line's
  own warehouse on-hand, not company-wide totals, to stop "approve-then-issue-fails".
- **Idempotency on SAP posts.** Attach a unique reference/key so a lost response
  doesn't yield a duplicate Goods Issue/Receipt on retry.
- **Push pagination into SQL** for `get_stock_overview` (currently fetch-all +
  Python slice) and consider HANA connection reuse.
- **Add coverage** for BOM approve caps, issue payload shaping, and FG create/receive/
  post state transitions.

---

## Permissions & roles

Dedicated `warehouse.*` object permissions (declared on the models, enforced in
`warehouse/permissions.py`) keep warehouse access independent of production:

| Permission | Class | Guards |
|------------|-------|--------|
| `warehouse.can_view_bom_request` | `CanViewBOMRequest` | List/detail BOM; **gates the whole Warehouse + WMS UI** |
| `warehouse.can_create_bom_request` | `CanCreateBOMRequest` | Create (production side) |
| `warehouse.can_approve_bom_request` | `CanApproveBOMRequest` | Approve / reject |
| `warehouse.can_issue_materials` | `CanIssueMaterials` | Issue to SAP |
| `warehouse.can_view_fg_receipt` | `CanViewFGReceipt` | List/detail FG |
| `warehouse.can_create_fg_receipt` | `CanCreateFGReceipt` | Create FG |
| `warehouse.can_receive_fg` | `CanReceiveFG` | Receive FG |
| `warehouse.can_post_fg_to_sap` | `CanPostFGToSAP` | Post FG to SAP |

**Group split** (migration `0010_bom_fg_store_group`):

- **"BOM & FG Store"** — view/approve/issue BOM + view/receive/post FG. The
  warehouse store operators.
- **`production_execution`** group — only the *production-side* perms
  (`can_create_bom_request`, `can_create_fg_receipt`, `can_view_fg_receipt`).
  Deliberately **not** granted `can_view_bom_request`, so the Warehouse module
  stays hidden from production.
- Migration `0011_run_viewers_fg_receipt` back-fills `can_view_fg_receipt` onto any
  group that can view a production run (the run screen loads FG receipts).

All WMS `/wms/*` read endpoints check only `IsAuthenticated + HasCompanyContext`;
the frontend gates them behind `can_view_bom_request`.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/warehouse/`)**

- `models.py` — `BOMRequest`, `BOMRequestLine`, `FinishedGoodsReceipt`, enums.
- `services/warehouse_service.py` — all BOM/FG logic + SAP writes.
- `services/wms_hana_reader.py` — WMS HANA read queries.
- `views.py` — BOM/FG/stock-check APIViews.
- `views_wms.py` — WMS dashboard APIViews.
- `serializers.py` — request/response shapes.
- `permissions.py` — the eight `warehouse.*` permission classes.
- `signals.py` / `notifications.py` — status-change notifications.
- `urls.py` — routing (BOM, material issue, stock check, FG, WMS, BST).
- `admin.py`, `apps.py`, `migrations/0010…`, `migrations/0011…`.
- `models_bst.py`, `views_bst.py`, `services/bst_service.py`, `tests.py` — BST
  (see [`bst.md`](./bst.md)); `tests.py` is BST-only.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/warehouse.md`
- **BST (this app):** [`warehouse/docs/bst.md`](./bst.md) ·
  `C:/Users/gurpa/dev/FactoryFlow/docs/modules/bst.md`
- **GRPO (separate app):** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/grpo.md`
- **Bin/pallet WMS (separate `wms` app):**
  `C:/Users/gurpa/dev/FactoryFlow/docs/modules/wms.md` — not the `/warehouse/wms/*`
  read dashboards documented here.
- **Memory:** cross-company-flow-boundary, sap-transaction-notification-validations,
  prod-server-and-observability, group-perms-vs-frontend-nav-gating.
