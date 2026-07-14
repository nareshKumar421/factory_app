# Branch Stock Transfer (BST) — Backend

> Django REST backend for the warehouse-driven, scan-based branch stock transfer.
> Frontend companion doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/bst.md`.
>
> **Trust this doc + the code, not older notes.** The previously-authoritative
> `FactoryFlow/src/docs/bst_flow.md` predates the cross-company **INVOICE** source
> type and the multi-document (`BSTTransferDoc`) rework; where it says "intra-company
> only", the code no longer agrees.

---

## Overview — what it does & who uses it

Branch Stock Transfer moves **barcoded finished-goods boxes/pallets** out of one
warehouse and settles them somewhere else, driven end-to-end by the warehouse team
and checked box-by-box against a **SAP source document**. It is a two-sided,
scan-based flow:

1. A **sender** (warehouse user in the source company) creates a BST against one or
   more SAP documents, **scans** the physical boxes/pallets, and **approves** the
   scanning.
2. If the goods leave the factory on a vehicle, a **gate** user marks the vehicle out.
3. A **receiver** (warehouse user at the destination) **scans** the arriving boxes and
   resolves each as **accepted** or **rejected**, then finalizes the receipt.

A BST has one of two `source_type`s, which decides how stock settles on receipt:

| `source_type` | Meaning | SAP document | On accept |
|---|---|---|---|
| `STOCK_TRANSFER` (default) | **Intra-company** move between two warehouses of the same company | SAP stock transfer (OWTR/WTR1) | Box `current_warehouse` → `sap_to_warehouse`; **company unchanged** |
| `INVOICE` | **Cross-company** sale (e.g. `JIVO_OIL` → `JIVO_MART`) | SAP A/R invoice | Box **ownership reassigned** to `destination_company` (+ item-code remap); warehouse unchanged |

**Neither path posts anything back to SAP** — settlement is app-side only (box
warehouse / company + movement + audit history). The SAP document is the *bill* the
scanning is validated against.

Primary code: the **`warehouse`** app (`models_bst.py`, `services/bst_service.py`,
`views_bst.py`, `serializers_bst.py`). A **separate, older gate-desk flow** lives in
`gate_core` (`BSTGateOut/BSTGateIn/BSTGateReturn`) — see
[Integrations](#integrations--cross-module-boundaries); it is not part of the scan
flow above and its UI is retired.

**Roles** (see [Permissions](#permissions--roles)): *BST Operator* (create/scan/
approve/receive), *BST Gate* (mark vehicle out), destination-company warehouse user
(receive).

---

## Key concepts & entities

All models live in `warehouse/models_bst.py`.

### `BSTTransfer` — the head record (one shipment)
- `company` — the **source / owning** company. Reads and sender writes are scoped to it.
- `entry_no` — human id `BST-YYYYMMDD-NNNN` (`generate_entry_no()`).
- `source_type` — `STOCK_TRANSFER` or `INVOICE` (see table above).
- `destination_company` — the **receiving** company; set **only** for `INVOICE`, null
  for `STOCK_TRANSFER`.
- `customer_code` / `customer_name` — snapshot of the SAP invoice customer (INVOICE only).
- `sap_doc_entry / sap_doc_num / sap_doc_date / sap_reference` — mirror of the **primary
  (first) document** for quick display.
- `sap_from_warehouse` / `sap_to_warehouse` — the entry's **shared route** (all documents
  in the entry must agree on it; `sap_to_warehouse` is empty for INVOICE).
- `invoice_no` — the number the operator typed to look the document up.
- `vehicle` / `driver` — only when `requires_gate` (stock leaves the factory).
- `requires_gate` — true when a gate movement is needed.
- `status` — see the lifecycle below.
- Audit stamps: `created_by`, `scan_approved_by/at`, `dispatched_by/at`,
  `gated_out_by/at`, `gated_in_by/at`, `received_by/at`, `cancelled_by/at`, `cancel_reason`.

### `BSTTransferDoc` — one SAP document inside the entry
A BST entry can **combine several SAP documents** onto one physical shipment. Each is a
`docs` row (`sap_doc_entry`, `sap_doc_num`, ...), unique per `(transfer, sap_doc_entry)`.
The head mirrors the first doc; the rest are only in `docs`.

### `BSTTransferItem` — expected line (the "bill")
A snapshot of one SAP line: `item_code`, `item_name`, `quantity`, `uom`,
`from_warehouse`, `to_warehouse`, and **`expected_boxes`** (the box target). Linked to
its `doc`. Unique per `(transfer, doc, line_num)` — `line_num` repeats across documents,
which is why `doc` is part of the key.

### `BSTBoxScan` — one physical box (send **and** receive state on one row)
Created when the **sender** scans a box; the **receiver** later stamps the same row.
- Send: `box`/`pallet` FKs (`SET_NULL`), `box_barcode`, denormalized `item_code`,
  `batch_number`, `quantity`, `uom`, `warehouse_code`, `pallet_code`, `scanned_by/at`.
- Receive: `receive_status` (`PENDING`/`ACCEPTED`/`REJECTED`), `reject_reason`,
  `is_unexpected`, `received_by/at`.
- Unique per `(transfer, box_barcode)` — a box can appear once per transfer.

### Status lifecycle (`BSTTransferStatus`)

```
                          approve()                         mark_gate_out()
 create() ─▶ SCANNING ───────────────┬── requires_gate ──▶ AWAITING_GATE_OUT ──▶ IN_TRANSIT
   (SCANNING)                         └── no gate ─────────────────────────────▶ IN_TRANSIT
                                                                                     │ receive_scan()
                                                                                     ▼
                                                                                 RECEIVING
                                                                    receive_complete()
                                                        ┌── all dispatched accepted ─▶ RECEIVED
                                                        └── any rejected / short ────▶ PARTIALLY_RECEIVED
 (any non-terminal state) ─▶ CANCELLED
```

**Reachable vs. dormant states — verified against `bst_service.py`:**
- Reachable: `SCANNING`, `AWAITING_GATE_OUT`, `IN_TRANSIT`, `RECEIVING`, `RECEIVED`,
  `PARTIALLY_RECEIVED`, `CANCELLED`.
- **Defined but never assigned** (dead today): `DRAFT` (the model default, but
  `create_transfer` immediately sets `SCANNING`), `DISPATCHED`, `GATED_OUT`, `GATED_IN`,
  `CLOSED`.
- **`AWAITING_GATE_IN` / `ARRIVED` — a wired-but-dormant destination gate-in.**
  `mark_gate_in()`, `gate_inwards_queryset()`, the `bst/gate/expected-inwards/` +
  `bst/<id>/gate/mark-in/` endpoints, and the `useMarkBSTGateIn` hook all exist, but
  **no transition ever sets `AWAITING_GATE_IN`** (`mark_gate_out` jumps straight to
  `IN_TRANSIT`). So `mark_gate_in` can never pass its guard and `ARRIVED` is unreachable.
  Treat destination gate-in as **not implemented** despite the scaffolding.

### Status sets used as guards (top of `bst_service.py`)
- `IN_FLIGHT_STATUSES` — a box on a transfer in any of these acts as a **soft lock** so
  the same box can't be scanned onto two active BSTs.
- `EDITABLE_STATUSES = {DRAFT, SCANNING}` — sender can scan/edit only here.
- `RECEIVABLE_STATUSES = {IN_TRANSIT, ARRIVED, RECEIVING}` — receiver can act only here.

---

## End-to-end flows (as the server sees them)

All business logic is in **`BSTService`** (`warehouse/services/bst_service.py`),
constructed per request as `BSTService(request.company.company.code, request.user)`.

### 1. SAP source-document lookup
- `list_sap_documents(document_type, search, from_date, to_date, limit)` /
  `get_sap_document(doc_entry, document_type)`.
- `STOCK_TRANSFER` → `SAPClient(company_code).list_stock_transfers / get_stock_transfer`.
- `INVOICE` → `gate_core.services.sales_dispatch_documents.SalesDispatchDocumentService`
  (the same reader the dispatch/docking flow uses), then `_normalize_invoice()` reshapes
  it into the stock-transfer shape so the create path treats both uniformly.
- **Box count per line** for invoices: prefer SAP's box total, else fall back to
  `quantity ÷ pack-size` parsed from the item name (`_boxes_from_pack_size`,
  `_PACK_SIZE_RE` = `\d+ (PCS|SETS|TINS|BOTTLES)`), mirroring the dispatch flow and the
  frontend `expectedBstItemBoxes`. Returns 0 only when neither is known.

### 2. Create — `create_transfer(data)` (`@transaction.atomic`)
1. Dedupe `sap_doc_entries` (order-preserving); require ≥1.
2. Fetch every SAP document live.
3. **Multi-doc route validation:**
   - `STOCK_TRANSFER`: all docs must share one `from_warehouse` **and** one `to_warehouse`.
   - `INVOICE`: `destination_company` is required and must differ from the source; all
     invoices must share one `from_warehouse` **and** one `card_code` (customer); and the
     cross-company **item-code map is pre-resolved** (`resolve_destination_item_code_map`)
     so a missing/duplicate mapping fails **at create**, not at the receiver's accept.
4. Create the head (status **`SCANNING`**), a `BSTTransferDoc` per SAP doc, and a
   `BSTTransferItem` per line (`expected_boxes` from the doc's `box_count`).

### 3. Scan — `scan()` / `scan_batch()` / `remove_scan()` (`@transaction.atomic`)
- `_lock()` re-fetches the transfer `select_for_update()` **re-scoped to the owning
  company**, then `_ensure_editable()`.
- `_resolve_scan()` uses `ScanService.lookup_barcode`; a **pallet expands to all its
  active, un-dispatched boxes**.
- Per box, `_validate_box()` enforces: belongs to `company`; status `ACTIVE`/`PARTIAL`;
  not dispatched; **item is on the combined bill**; **physically at `sap_from_warehouse`**.
  Plus `_box_locked_elsewhere()` (not on another in-flight BST) and an **over-count guard**
  — scanned boxes per item may not exceed the bill's summed `expected_boxes`.
- Duplicates (same `box_barcode` already on this transfer) are counted, not errored.
- `scan_batch()` **never fails the whole call** — it returns `{saved, failed}` with a
  per-barcode reason, which is what powers the frontend's retry queue.

### 4. Approve — `approve()` (`@transaction.atomic`)
Warehouse's final confirmation. Requires ≥1 box. Stamps `scan_approved_by/at`, then:
- `requires_gate` → **`AWAITING_GATE_OUT`** (handed to the gate);
- else → **`IN_TRANSIT`** + `dispatched_by/at` (immediately receivable).

### 5. Gate out — `mark_gate_out()` (perm-gated)
`AWAITING_GATE_OUT` → **`IN_TRANSIT`**, stamping `gated_out_*` and `dispatched_*`. (The
symmetric `mark_gate_in` is dormant — see the lifecycle note.)

### 6. Receive — `receive_scan()` then `receive_complete()` (`@transaction.atomic`)
- `_lock(as_receiver=True)` re-scopes to the **receivable set** (`_receivable_scope()`):
  own `STOCK_TRANSFER`s **or** `INVOICE`s where `destination_company == self.company`.
- The barcode is resolved with `ScanService(transfer.company.code)` — i.e. in the
  **source** company, because the box hasn't changed hands yet.
- **Receiving is restricted to the dispatched set.** A pallet receives only its boxes that
  were dispatched on this transfer (ignoring the rest); an explicitly scanned box (or raw
  barcode) that wasn't dispatched here is a **400 error**. `is_unexpected` exists on the
  model but the current path never sets it (returns `unexpected: []`).
- First receive scan flips the head to **`RECEIVING`**.
- `receive_complete()` runs `_apply_accepted_moves()`, then sets **`RECEIVED`** iff every
  dispatched box is accepted (none pending/rejected, ≥1 accepted), else
  **`PARTIALLY_RECEIVED`**, and stamps `received_by/at`.

### 7. Settlement — `_apply_accepted_moves()`
- **`STOCK_TRANSFER`** → `_apply_accepted_transfer_moves`: `Box.current_warehouse` →
  `sap_to_warehouse`, write a `BoxMovement(TRANSFER)`; **company never changes**.
- **`INVOICE`** → `_apply_accepted_invoice_moves`: `select_for_update` the boxes,
  re-resolve the item-code map at settle time (authoritative), then
  `reassign_boxes_to_company()` (updates `Box.company` + `BarcodeMaster`, and for
  `JIVO_OIL → JIVO_MART` remaps `item_code` via OITM `U_Oil_ItemCode`), and write a
  `BarcodeAuditLog(TRANSFER_COMPLETED)` per box. **Warehouse is left unchanged**;
  **nothing is posted to SAP.**

### 8. Cancel — `cancel(reason)`
Any non-terminal transfer (not `RECEIVED`/`PARTIALLY_RECEIVED`/`CLOSED`/`CANCELLED`) can be
cancelled **unless any box is already `ACCEPTED`** at the destination. Sets `CANCELLED` +
`cancel_*`.

---

## Critical business rules & invariants

1. **Scanning is bill-bounded.** Only items on the combined SAP bill, only from the source
   warehouse, and never more boxes per item than the summed `expected_boxes`.
2. **One box, one active BST.** `IN_FLIGHT_STATUSES` + `_box_locked_elsewhere` prevent a box
   from riding two live transfers.
3. **One box row per transfer.** `unique(transfer, box_barcode)`; re-scans are idempotent
   duplicates, not new rows.
4. **Receiving is closed to the dispatched set.** You can only accept/reject boxes the
   sender actually put on the transfer.
5. **Company resolution (the classic cross-company trap).** *Reads* on the receiver side use
   `_receivable_scope()` so an `INVOICE` is visible in the **destination** company (not the
   owner). *Writes* resolve `source = transfer.company` and `destination =
   transfer.destination_company` **from the record**, never from the request's company
   context. Getting either wrong shows the transfer/boxes as blank in the sibling company.
6. **Cross-company item mapping fails early.** Missing/duplicate `U_Oil_ItemCode` is caught
   at **create** and again at **settle** — a receiver never silently accepts an unmappable box.
7. **Atomicity + row locks.** Every mutation is `@transaction.atomic` and re-fetches the head
   `select_for_update()` so check-then-act can't race.
8. **SAP is read-only here.** No GRPO, no stock-transfer post, no invoice post. Doc numbers
   are references only.
9. **Cancel is safe after partial acceptance is blocked**, not before — once even one box is
   accepted the transfer must be finished via receive-complete.

---

## Integrations & cross-module boundaries

- **SAP (`sap_client`, read-only).** Stock transfers via `SAPClient.list_stock_transfers /
  get_stock_transfer`; invoices via `gate_core`'s `SalesDispatchDocumentService`. Failures
  surface as **HTTP 502** on list/create (`_sap_error`) and **404** for a not-found doc.
- **`barcode`.** `Box`, `Pallet`, `BoxStatus`, `BoxMovement(TRANSFER)`, `BarcodeAuditLog`,
  `ScanService` (barcode → entity lookup), and `services/box_ownership.py`
  (`reassign_boxes_to_company`, `resolve_destination_item_code_map`,
  `requires_item_code_remap`) — **shared with the standalone intercompany-transfer flow**,
  so BST's cross-company handoff behaves identically.
- **`company`.** Source vs. destination company (the boundary rules above).
- **`vehicle_management` / `driver_management`.** `vehicle`/`driver`, required only when
  `requires_gate`.
- **`gate_core` — two separate touch-points:**
  1. The **invoice reader** (`SalesDispatchDocumentService`) reused for INVOICE sourcing.
  2. A **legacy gate-desk BST** (`BSTGateOut`/`BSTGateOutItem`, `BSTGateIn`/`BSTGateInItem`,
     `BSTGateReturn`) with its own `gate_core/views.py` classes and `/gate-core/bst-outs|
     bst-ins|bst-returns/` URLs. These are **not** part of the scan flow: a `BSTGateOut` is
     raised at the gate against an `EmptyVehicleGateIn(reason="BST")` + `VehicleEntry` for a
     stock transfer **already posted in SAP**, snapshotting WTR1 lines (no box scanning).
     Their **frontend was retired** — the routed Gate "BST Out" page instead calls the
     *warehouse* endpoints (`bst/gate/expected-outwards`, `bst/<id>/gate/mark-out`). Treat
     `gate_core`'s BST models/views as **legacy/parallel**, slated for removal; don't confuse
     `BSTGateOut` (gate-desk, `BSTO-…`) with `BSTTransfer.mark_gate_out` (scan flow).

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Partial truck load (some boxes short at receipt).** Receiver accepts fewer than were
   dispatched and finalizes → `receive_complete` sets **`PARTIALLY_RECEIVED`**; short boxes
   keep `receive_status=PENDING` and never settle (stay in the source warehouse / source
   company). Symptom: transfer badge "Partially Received"; pending boxes still show at
   source. Gap: **no reconciliation / re-open** — a later-arriving box can't be received
   against a finalized transfer.
2. **Re-scanned / duplicate box (sender).** Same box scanned twice → counted as a
   `duplicate`, no new row, no error. Symptom: "already scanned" toast; count doesn't move.
   Low risk.
3. **Scanner offline / flaky (sender or receiver).** Scans queue client-side and each is
   POSTed independently; a failed POST returns a per-barcode reason and lands on the
   **failed-scan** list for **Retry**. Symptom: "Syncing N…", then a red failed row. Risk:
   the queue is **in-memory** — a page reload drops unsynced scans (already-synced ones are
   safe on the server).
4. **Box not at the source warehouse.** Stock moved after the bill was made →
   `_validate_box` rejects with "`… is at X, not the source warehouse Y`". Symptom: failed
   scan with that reason. Correct-by-design; operator must fix the physical/SAP location.
5. **Box already on another active BST.** `_box_locked_elsewhere` → "already on another
   active BST". Symptom: failed scan. Prevents double-shipping one box.
6. **Cross-company blank data (INVOICE).** If a read used the source-company scope instead of
   `_receivable_scope`, the destination user's **Incoming** tab would be empty even though the
   transfer exists. Current code scopes correctly; this is the #1 thing to preserve when
   editing querysets.
7. **Unmappable item on a cross-company sale.** `U_Oil_ItemCode` missing/duplicate in JIVO
   MART OITM → **create is blocked** ("Please maintain/correct U_Oil_ItemCode…"); if it breaks
   *after* create, `receive_complete` raises the same 400. Symptom: clear error at create, or
   the receiver can't finalize. Gap: a receiver can accept-scan boxes and only hit the wall at
   **Finalize**.
8. **SAP down during lookup/create.** `SAPConnectionError/SAPDataError` → **HTTP 502**.
   Symptom: "SAP … unavailable" style error on search/create. No partial BST is written
   (create is atomic and SAP is read before the transaction commits).
9. **Bill has 0 expected boxes (invoice missing box total).** SAP box total absent and item
   name has no `… PCS` pattern → `expected_boxes = 0`. Symptom: scan page shows "0 boxes to
   scan" and the **over-count guard is disabled** for that item (limit 0 ⇒ unlimited), so any
   count of on-bill boxes is accepted. Risk: over-scan isn't caught for such lines.
10. **Cancel after acceptance.** Once any box is accepted, `cancel` raises "Some boxes have
    already been received; cannot cancel." Symptom: the Cancel button errors. Correct guard.

---

## Failure modes / what can break

- **SAP unavailable** → 502/404 on search & create; operators can't start a BST but nothing is
  corrupted.
- **Item-code mapping missing (cross-company)** → create blocked, or finalize blocked → a
  cross-company sale stalls until master data is fixed in JIVO MART OITM.
- **Stuck `PARTIALLY_RECEIVED`** → short boxes never settle and there's no reopen path; the
  operation looks "done" but stock is split. Manager-visible as a lingering partial with
  PENDING boxes.
- **Dormant gate-in scaffolding** → if a future change ever routes a transfer to
  `AWAITING_GATE_IN` without also making `mark_gate_out` set it, the transfer would be stuck
  (nothing else advances it). Today it simply can't be reached.
- **Lost unsynced scans** on a mid-scan reload (in-memory queue).
- **Permission surface is uneven** (below) — a create/scan/receive call is **not** blocked
  server-side by the BST model perms, only by company context.

---

## Improvement opportunities & known gaps

1. **Enforce the sender/receiver model permissions server-side.** Only the three gate views
   require `warehouse.can_gate_bst`; create/scan/approve/receive rely on `HasCompanyContext`
   alone. `can_create_bst`, `can_scan_bst`, `can_dispatch_bst`, `can_receive_bst` are defined
   and granted to *BST Operator* but **not checked** in `views_bst.py`.
2. **Remove or wire the dormant states** (`DRAFT`, `DISPATCHED`, `GATED_OUT`, `GATED_IN`,
   `CLOSED`) and either implement destination gate-in (`AWAITING_GATE_IN`/`ARRIVED`) or delete
   its endpoints/hook to avoid a false sense of coverage.
3. **Reopen / reconcile `PARTIALLY_RECEIVED`** so late boxes can be accepted.
4. **Post to SAP** (stock-transfer receipt / invoice) if BST is meant to be the system of
   record rather than a physical-movement checker.
5. **Retire the `gate_core` BST models/views** once this flow is validated (they still carry
   `EmptyVehicleGateIn` entanglement).
6. **Persist the scan queue** (localStorage) to survive reloads.
7. **The batch-scan endpoint is dead weight.** `BSTBoxScanBatchView` (`POST bst/<id>/box-scans/batch/`)
   and `BSTService.scan_batch` exist and are even wired into the frontend API client
   (`bstApi.scanBatch`), but **nothing calls them** — the live scan flow POSTs one box at a time via
   `scan()`/`scanBox`. Either adopt it for burst scanning or remove it and its serializer.
8. **`receive_scan` never records "unexpected" boxes.** It always returns `unexpected: []` and 400s a
   non-dispatched box, so `BSTBoxScan.is_unexpected` is dead and the frontend's "unexpected" chip/
   toast can never fire. Decide whether unexpected boxes should be captured (and set the flag) or
   remove the field and the UI that reads it.

---

## Permissions & roles

Custom model permissions on `BSTTransfer` (`Meta.permissions`): `can_create_bst`,
`can_scan_bst`, `can_dispatch_bst`, `can_receive_bst`, `can_gate_bst`, plus Django's default
`view_bsttransfer`.

`python manage.py setup_bst_group` (`warehouse/management/commands/setup_bst_group.py`) creates
two groups:
- **BST Operator** → `view_bsttransfer`, `can_create_bst`, `can_scan_bst`, `can_dispatch_bst`,
  `can_receive_bst`. Runs the whole warehouse flow.
- **BST Gate** → `can_gate_bst` **only** (no `view_bsttransfer`), so gate staff see just the
  gate "BST Out" step and none of the warehouse pages.

Users also need a `UserCompany` (company access) — not granted by these groups.

**Server-side enforcement today:** only `BSTGateOutwardsListView`, `BSTGateInwardsListView`,
`BSTGateMarkOutView`, `BSTGateMarkInView` add `HasRequiredDjangoPermission` +
`required_permissions = "warehouse.can_gate_bst"`. Every other BST view is
`[IsAuthenticated, HasCompanyContext]`. The finer-grained gating is enforced on the
**frontend** routes (see the companion doc).

### API surface (`warehouse/urls.py`, mounted under the warehouse API root)

| Method & path | View | Purpose |
|---|---|---|
| `GET bst/sap-transfers/` | `BSTSAPTransferListView` | Search SAP stock transfers **or** invoices (`?document_type=INVOICE`) |
| `GET bst/sap-transfers/<doc_entry>/` | `BSTSAPTransferDetailView` | One SAP document with lines |
| `GET/POST bst/` | `BSTTransferListCreateView` | List outgoing (date-filtered) / create |
| `GET/PUT bst/<id>/` | `BSTTransferDetailView` | Detail / edit while `SCANNING` |
| `GET/POST bst/<id>/box-scans/` | `BSTBoxScanListCreateView` | List scans / scan one box or pallet |
| `POST bst/<id>/box-scans/batch/` | `BSTBoxScanBatchView` | Scan many (per-code result) |
| `DELETE bst/<id>/box-scans/<scan_id>/` | `BSTBoxScanDetailView` | Remove a scan |
| `POST bst/<id>/approve/` | `BSTApproveView` | Warehouse approval → gate or in-transit |
| `POST bst/<id>/cancel/` | `BSTCancelView` | Cancel |
| `GET bst/incoming/` · `incoming/<id>/` | `BSTIncoming*View` | Destination inbox / detail |
| `POST bst/<id>/receive-scans/` | `BSTReceiveScanView` | Accept/reject an arriving box/pallet |
| `POST bst/<id>/receive/complete/` | `BSTReceiveCompleteView` | Finalize + settle |
| `GET bst/gate/expected-outwards/` | `BSTGateOutwardsListView` | **perm** `can_gate_bst` — gate-out board |
| `POST bst/<id>/gate/mark-out/` | `BSTGateMarkOutView` | **perm** — mark vehicle out |
| `GET bst/gate/expected-inwards/` | `BSTGateInwardsListView` | **perm** — *dormant* |
| `POST bst/<id>/gate/mark-in/` | `BSTGateMarkInView` | **perm** — *dormant (unreachable)* |

List endpoints honor `?from_date=&to_date=` (`created_at`, except the gate-out board which
filters on `gated_out_at`). Errors: `BSTError` → 400, SAP errors → 502, not-found → 404.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app`)**
- `warehouse/models_bst.py` — `BSTTransfer`, `BSTTransferDoc`, `BSTTransferItem`, `BSTBoxScan`,
  status/enum choices.
- `warehouse/services/bst_service.py` — `BSTService` (all logic + status-set constants).
- `warehouse/serializers_bst.py` — read/write serializers incl. `BSTSapDocumentSerializer`.
- `warehouse/views_bst.py` — the API views + error helpers.
- `warehouse/urls.py` — the `bst/…` routes.
- `warehouse/management/commands/setup_bst_group.py` — *BST Operator* / *BST Gate* groups.
- `warehouse/migrations/0002…0008` — BST tables (`0007` docs, `0008` `source_type` +
  cross-company fields).
- `barcode/services/box_ownership.py` — cross-company reassignment + item-code remap.
- `gate_core/services/sales_dispatch_documents.py` — the invoice reader reused for INVOICE.
- `gate_core/models/bst_gate_out.py`, `bst_gate_in.py`, `bst_gate_return.py` +
  `gate_core/views.py` (`BSTGateOut*`, `BSTGateIn*`, `BSTGateReturn*`) — **legacy gate-desk BST**.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow`)** — see the companion doc.
- `src/modules/warehouse/pages/bst/*`, `src/modules/warehouse/api/bst.*`,
  `src/modules/warehouse/types/bst.types.ts`, and the Gate "BST Out" pages in
  `src/modules/gate/pages/bstGate/*`.

---

## Related docs
- **Frontend companion:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/bst.md`
- **Older flow note (partly stale — pre cross-company/multi-doc):**
  `C:/Users/gurpa/dev/FactoryFlow/src/docs/bst_flow.md`
- Cross-company boundary rule: user memory `cross-company-flow-boundary.md`.
