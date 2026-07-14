# Barcode & Labels — Backend (`barcode` Django app)

> Audience: developers and technical managers. This documents the **code as it is
> today**, not the original design brief. Where older docs describe SAP stock
> postings or bin foreign keys, the code has since diverged — trust this file.
>
> Frontend companion: [`FactoryFlow/docs/modules/barcode.md`](../../../FactoryFlow/docs/modules/barcode.md)

---

## Overview — what it does & who uses it

The `barcode` app is the factory's **label + physical-unit tracking layer**. It
mints barcodes for cartons (boxes) and pallets, tracks every physical move, and
drives the **scanner-first dispatch** workflow that validates what actually gets
loaded onto a truck against an SAP bill.

All box/pallet/scan/print/dispatch state lives in **PostgreSQL**, scoped per
company. SAP is used for **reads** (bill lookup, item master, production-release
rows) but the app does **not** post stock movements back to SAP today — see
[Integrations](#integrations--cross-module-boundaries).

Primary users:
- **Barcode operators / production floor** — generate and print box+pallet labels.
- **Warehouse / dispatch operators** — scan pallets and boxes against a bill to dispatch.
- **Barcode team** (holders of `barcode.view_pallet`) — resolve pallet-verify tickets, reconcile drift.
- **Transfer operators** — intercompany box/pallet ownership moves.
- **Managers** — dispatch, pallet, box, and rejected-scan reports.

---

## Key concepts & entities

Defined in `models.py`.

| Entity | Meaning |
|--------|---------|
| **Box** | One physical carton. Unique global `box_barcode` like `BOX-20260417-L4-0001`. Has item/batch/qty/uom/weights, a `current_warehouse` + app-managed `current_bin` (string, **not** a FK), optional `pallet` FK, and a `status`. |
| **Pallet** | A collection of boxes. Unique global `pallet_id` like `PLT-20260417-L4-001`. Carries denormalized counters (`box_count`, `total_boxes`, `available_boxes`, `dispatched_boxes`, `total_qty`) recomputed from its boxes, plus an editable `max_box_count` capacity. |
| **BarcodeSequence** | Per `(company, type, date, line)` counter that reserves contiguous barcode numbers under `select_for_update`, so concurrent label runs never collide. |
| **BarcodeMaster** | Optional normalized `barcode → box/pallet/item` mapping used by dispatch resolution (e.g. printed SSCC/EAN that isn't the raw `BOX-…` string). Unique per `(company, barcode)`. |
| **LooseStock** | Product dismantled out of a box (full or partial). Can later be **repacked** into a new box. Keeps `original_qty` (dismantle-time qty) alongside the consumable `qty`. |
| **PalletMovement / BoxMovement** | Append-only movement history (CREATE, MOVE, TRANSFER, PALLETIZE, DEPALLETIZE, DISPATCH, REMOVE_FOR_DISPATCH, DISMANTLE, CLEAR, SPLIT, VOID). |
| **PalletBoxHistory** | Higher-level pallet/box lifecycle events tied to a dispatch session (e.g. `BOX_DISPATCHED_SEPARATELY`, `BOX_PARTIAL_DISPATCH`). |
| **ScanLog** | Generic scan audit for the lookup/scan endpoints (RECEIVE/PICK/COUNT/…/LOOKUP). |
| **LabelPrintLog** | Every print/reprint, with reprint reason + printer name. |
| **DispatchSession** (+ `DispatchSessionLine`, `DispatchScanLog`, `DispatchScannedUnit`, `DispatchSapSyncLog`) | The whole barcode-dispatch workflow for one SAP bill. |
| **DispatchSettings** | One row per company: partial-dispatch, partial-pallet, box-from-pallet, sequential-scan, manual-close, admin-override toggles (+ an inert `require_sap_sync_on_completion`). |
| **IntercompanyTransfer** (+ `IntercompanyTransferLine`) | Moves box/pallet **ownership** from one company to another. |
| **BarcodeAuditLog** | Global (non-company-scoped) traceability trail: MANUFACTURED, SCANNED, TRANSFER_*, DISPATCH_COMPLETED. |
| **PalletVerifyRequest** | A ticket asking the barcode team to reconcile a suspect pallet. |

### Barcode formats (`services/scan_service.py::_parse_barcode`)
- Canonical: `BOX-YYYYMMDD-LINE-NNNN` (4-digit box seq) and `PLT-YYYYMMDD-LINE-NNN` (3-digit pallet seq).
- `barcode_data` JSON now stores only `{"barcode": "<id>"}` — the heavy legacy JSON-QR payload is a **read-only compatibility fallback**.
- Also accepted: `BOX_ID:`/`PALLET_ID:` prefixes, and 1D printer values `BBOX…`/`PPLT…` (canonical string with `-`/space stripped and a `B`/`P` prefix).

### Status lifecycles
- **Box**: `ACTIVE → PARTIAL → DISPATCHED` / `DISMANTLED` / `VOID`.
- **Pallet**: `ACTIVE → PARTIAL → DISPATCHED` / `EMPTY` / `CLEARED` / `VOID` (`SPLIT`, `INACTIVE` defined but rarely used).
- **DispatchSession**: `DRAFT → ACTIVE → PARTIAL → READY_TO_DISPATCH → COMPLETED`; plus `CLOSED`, `CANCELLED`, `SAP_SYNC_FAILED`.

---

## End-to-end flows

### 1. Generate & print labels (Pallet QR workflow)
1. Frontend fetches a **finished-good item** from SAP HANA `OITM` (`OitmItemListAPI`) and, optionally, a `PRODUCTION_RELEASE_OIL` row (`ProductionReleaseOilListAPI`).
2. An **empty pallet** is created first via `PalletCreateAPI` → `BarcodeService.create_pallet`. Creation is deliberately empty: passing `box_ids` is rejected ("Create the pallet first, then attach boxes…").
3. `BoxGenerateAPI` → `generate_boxes` bulk-creates N boxes for the item/batch, reserving a contiguous sequence range, writing CREATE movements and `MANUFACTURED` audit rows.
4. The boxes are attached to the pallet via `PalletAddBoxesAPI` → `add_boxes_to_pallet`, which stamps the pallet's item/batch/uom from the first box and enforces one item/batch/uom per pallet.
5. `BulkPrintAPI` returns label data (pallet ×2 + each box) and logs each print. Rendering/printing happens in the browser.

### 2. Barcode dispatch (the core SAP-validated flow) — `services/dispatch_service.py`
1. **Lookup** — `DispatchBillLookupAPI` → `lookup_bill` reads the bill from SAP via `dispatch_plans.DispatchPlansService.get_bill_by_number` (through `SapDispatchAdapter`) and normalizes lines (material, qty, `total_boxes` derived from a `N PCS/BTL` pack size parsed out of the item name when SAP doesn't give it explicitly).
2. **Create/resume session** — `DispatchSessionListCreateAPI`/`…FromBill` → `create_session`. `select_for_update` + a partial unique constraint (`unique_active_barcode_dispatch_bill`) make this **idempotent**: an open session for the bill is resumed, an already-dispatched bill is rejected (`BILL_ALREADY_DISPATCHED`). Each SAP line becomes a `DispatchSessionLine` with `bill_qty`/`bill_boxes`.
3. **Scan** — `DispatchSessionScanAPI` → `submit_scan`. Per scan: dedupe by `request_id`; resolve the barcode to box / pallet / item (via `ScanService`, then `BarcodeMaster`, then a bare material-code match against the bill lines); route to `_scan_box`, `_scan_pallet`, or `_scan_item`.
   - **Line selection** (`_select_line_for_material`): if `require_sequential_item_scanning` is on, the scan must match the current active line (else `LINE_SEQUENCE_VIOLATION` / `WRONG_MATERIAL`); if off, the operator may pass `line_id`, or the first pending line for that material is chosen.
   - Batch mismatch → `WRONG_BATCH`; warehouse mismatch → a non-blocking `warehouse_warning` attached to the scan message.
   - Accepted scans create a `DispatchScanLog` + `DispatchScannedUnit` (staged, not yet dispatched) and increment `line.scanned_qty`. A **box** dispatches `min(box qty, line pending)`; a **pallet** allocates across its dispatchable boxes. When a box is fully consumed *and* `allow_partial_pallet_dispatch` is **off**, it is detached from its pallet already at scan time (`_remove_box_from_pallet_for_dispatch`).
4. **Adjust** — `DispatchScannedBoxQtyAPI` (edit staged qty, re-checked against box qty and remaining bill qty) and `…RemoveAPI` (remove a staged box) before completion.
5. **Complete** — `DispatchSessionDispatchAPI`/`…CompleteAPI` → `mark_dispatched → complete_session`. Requires all lines complete **unless** `allow_partial_dispatch`. This is where staged units are **applied** (`_apply_scanned_box_dispatch`): boxes flip to `DISPATCHED` (qty→0) or `PARTIAL` (qty reduced), are removed from their pallet when dispatched separately, DISPATCH movements + `PalletBoxHistory` are written, and pallets recalc to `DISPATCHED`/`PARTIAL`/`EMPTY` via `_recalculate_pallet_state`.
6. **SAP write-back is disabled.** `complete_session` sets `sap_update_status = NOT_CONFIGURED` with error text "SAP sync is disabled for barcode dispatch." The session lands in `COMPLETED` locally regardless.
7. **Close / Cancel** — `close_session` (needs `allow_manual_close`) and `cancel_session` end a session that has **not** reached a locally-dispatched status; `COMPLETED`/`SAP_SYNC_FAILED` sessions cannot be closed/cancelled.

### 3. Pallet operations — `services/barcode_service.py`
- **Move** (`move_pallet`) — changes warehouse/bin (must actually change one of them), moves all active boxes too, and mirrors the pallet into the WMS `inventory` collection (`_sync_wms_pallet_inventory`) when it lands in an own-warehouse bin.
- **Add / Remove boxes**, **Clear** (strip all boxes, status → `CLEARED`, item context wiped), **Split** (move selected boxes into an empty target pallet — cannot split *all* boxes), **Void**.
- **Reconcile** (`reconcile_pallet`) — compare a pallet's expected boxes against physically scanned barcodes → `matched` / `missing` / `foreign` buckets. Read-only by default; `apply=True` heals drift (pull same-item foreign boxes on, drop missing boxes off; unsafe rows are skipped and reported). Flags a **recover** case: when the operator confirms exactly as many unlabeled boxes as there are missing records and they share one item/batch/uom, the missing labels can be reprinted safely.

### 4. Dismantle / repack / loose stock
- `dismantle_pallet` — depalletize all/selected boxes (fully cleared pallet → `CLEARED`).
- `dismantle_box` — split a box into `LooseStock` (full → box `DISMANTLED`; partial → box `PARTIAL`, qty reduced).
- `repack` — consume one-or-more same item+batch loose records into a **new** box (`RP` line). Exp/mfg dates copied from the source box when available.

### 5. Intercompany transfer — `services/intercompany_transfer_service.py`
1. `scan_barcode` validates a box/pallet belongs to the source company and is active (writes a `SCANNED` audit row). It also checks the user has `UserCompany` membership of **both** source and destination.
2. `create_transfer` reassigns the `company` FK on boxes (+pallets) and their `BarcodeMaster` rows to the destination (`box_ownership.reassign_*`). For **JIVO_OIL → JIVO_MART** it remaps each item code via the destination's `OITM.U_Oil_ItemCode` (`resolve_destination_item_code_map`); a missing/duplicate mapping fails the whole transfer atomically.
3. `reverse_transfer` restores ownership (and the original oil item codes on the mapped route) as long as no box has since been dispatched.

### 6. Pallet verify tickets — `services/verify_request_service.py`
A non-team operator raises a `PalletVerifyRequest` (snapshotting a read-only reconcile). The barcode team (`view_pallet`) marks it in-progress and resolves/cancels it. Best-effort notifications flow both ways.

---

## Critical business rules & invariants

- **Global barcode uniqueness.** `box_barcode` and `pallet_id` are unique across all companies. Generation reserves numbers through `BarcodeSequence.select_for_update` and re-checks existing barcodes (`_existing_next_value`) so a stale/recreated sequence row can't mint a duplicate. `IntegrityError` on collision → "Duplicate barcode detected. Please try again."
- **One item/batch/uom per pallet.** Enforced in `_prepare_pallet_for_receiving_boxes` and the empty/cleared-pallet reuse logic.
- **Dispatch line cannot be over-scanned.** DB `CheckConstraint dispatch_line_scanned_lte_bill` (`scanned_qty ≤ bill_qty`) backs the service-level `OVER_QUANTITY` checks; allocations always clamp with `min(available, pending)`.
- **One open dispatch per bill.** Partial unique constraint on `(company, bill_number)` over the open statuses; completing/failing keeps the bill locked out of new sessions.
- **One barcode / serial per session.** `unique_dispatch_barcode_per_session` and `unique_dispatch_serial_per_session` on `DispatchScannedUnit`.
- **Idempotent scans.** A repeated `request_id` returns the original `DispatchScanLog` instead of double-counting.
- **Box can't be dispatched twice.** `_scan_box` rejects `BOX_ALREADY_SCANNED` (staged in this session) and `BOX_ALREADY_DISPATCHED` (already gone, with a distinct message if it left via a pallet).
- **Completed sessions are terminal** for close/cancel; dispatched boxes/pallets are guarded against deletion (`delete_empty_pallet` refuses pallets with boxes, dispatch links, or scan history).
- **Company scoping (default).** Every service is constructed with `company_code` and filters `company=self.company`. The **exceptions** are the deliberately cross-company flows: `IntercompanyTransferService` resolves companies from the record and checks `UserCompany` membership, and `BarcodeAuditLog` / traceability is intentionally **global**.

---

## Integrations & cross-module boundaries

| Direction | What happens |
|-----------|--------------|
| **SAP HANA (read)** | Bill lookup (`dispatch_plans` via `SapDispatchAdapter`), `OITM` finished goods (`oitm_item_service`), `PRODUCTION_RELEASE_OIL` view (`production_release_service`), `U_Oil_ItemCode` mapping (`box_ownership`). |
| **SAP (write)** | **Not wired.** `services/sap_integration_service.py::create_stock_transfer` validates and logs intent, then returns `None` (TODO comment). `SapDispatchAdapter.update_dispatch_status` always returns `NOT_CONFIGURED`. Stock in SAP is **not** decremented on dispatch, move, or transfer. |
| **Production** (`production_execution`) | `ProductionBarcodeIntegration` generates labels/pallets for a `ProductionRun`; `Box.production_run` / `Pallet.production_run` link back. |
| **Warehouse Ops (`wms`)** | Pallet moves are mirrored into the WMS `inventory` collection (`_sync_wms_pallet_inventory`). The pallet **list** and **move** endpoints additionally accept WMS operator perms (`CanAccessBarcodePalletSync`) so the WMS pallet-move bridge can call them. |
| **dispatch_plans** | Source of SAP bill data for dispatch. |
| **notifications** | Intercompany completion/failure and verify-request events (best-effort). |
| **company** | `HasCompanyContext` provides `request.company.company`; `UserCompany` gates intercompany membership. |

**Boundary vs. gate / vehicle-arrival:** barcode dispatch is keyed to an **SAP bill number** and is independent of the gate/`VehicleArrival` lifecycle. It marks boxes/pallets `DISPATCHED` in Postgres but does not open/close arrivals or post an SAP delivery. A bill added after gate-in is a gate-module concern; the barcode session simply looks up whatever SAP returns for that bill.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Duplicate / re-scanned box during dispatch** → `_scan_box` finds an existing active unit and rejects `BOX_ALREADY_SCANNED`; the accepted count is unchanged → operator sees a warning toast and the already-scanned box row is highlighted → low risk (idempotency + `request_id` also protect against network double-submits).
2. **Box already dispatched on another bill** → `BOX_ALREADY_DISPATCHED`; message differs if it left via a pallet ("already dispatched through pallet dispatch") → operator told the box is gone → correct, but there is no cross-session "where did it go" hint beyond the message.
3. **Pallet where some boxes were already dispatched/removed** → with `allow_partial_pallet_dispatch` **off**: whole pallet rejected `PALLET_HAS_DISPATCHED_BOXES`. **On**: only remaining boxes dispatch, with warning "N boxes were already dispatched or removed…" → operator may not notice the count is short → risk of an under-loaded pallet slipping through if the warning is ignored.
4. **Partial truck load** → `allow_partial_dispatch` lets `complete_session` finish with lines still pending; the session goes `COMPLETED` with `total_scanned < total_expected` → looks "done" in the queue → **gap**: nothing forces a follow-up session for the shortfall; only the report surfaces pending qty.
5. **SAP down during bill lookup** → `lookup_bill` raises `SAP_UNAVAILABLE` (HTTP 503) → operator sees "Unable to fetch bill details from SAP" → they simply cannot start until SAP is back. Note: completion itself never calls SAP, so an SAP outage does **not** block dispatching an already-started session.
6. **"SAP rejecting/failing during posting"** → **does not occur** — barcode dispatch never posts to SAP. `sap_update_status` stays `NOT_CONFIGURED`. The `SAP_SYNC_FAILED` status, `retry_sap_sync` endpoint, and `DispatchSapSyncLog` exist but are effectively **dormant** because `update_dispatch_status` returns `NOT_CONFIGURED` (treated as success) → **gap/latent risk**: if SAP write-back is ever enabled, this dormant path becomes live and needs testing.
7. **Cross-company "blank" data after transfer** → transferring a box JIVO_OIL→JIVO_MART moves its `company` FK; it then disappears from JIVO_OIL box/pallet lists (all reads are single-company) → an operator in the source company sees the box vanish → **by design**; trace it with the global **Traceability** endpoint (`BarcodeAuditLog`), which is not company-filtered.
8. **Missing weighbridge / gross-net weight** → `g_weight`/`n_weight` are nullable; labels render blank weight fields → no error, label just omits weight → acceptable, but downstream weight totals on pallet labels will under-report.
9. **Concurrent label generation on the same line/date** → two requests both reserve from the same `BarcodeSequence` row under `select_for_update`; the second waits, so ranges never overlap → no duplicate barcodes → correct (this is the reason the sequence row is locked).
10. **Label fell off a box / illegible** → open **Verify Pallet**, scan the siblings, confirm the unlabeled count; if it equals the missing count and item/batch/uom match, the missing box's label is reprintable ("recover") → operator reprints without minting a new barcode → correct; if counts don't line up it correctly refuses to guess.
11. **Unknown/garbage scan** → `_resolve_barcode` returns nothing → `BARCODE_NOT_FOUND` reject logged → operator sees a rejection toast, scan is audited in the rejected-scan report.
12. **Legacy 1D barcode miss** → `_lookup_box`/`_lookup_pallet` only fall back to a full-table Python scan for genuine `BBOX…`/`PPLT…` values; a canonical `BOX-…` that misses the indexed lookups returns `None` immediately (a guard added after unmatched scans were walking tens of thousands of rows and hanging the dock queue ~20s).

---

## Failure modes / what can break

| Failure | Where | Symptom a manager/operator notices |
|---------|-------|-----------------------------------|
| SAP HANA unreachable | bill lookup, OITM, production-release | 503 "SAP unavailable"; can't start dispatch or fetch items for label print. |
| SAP `U_Oil_ItemCode` missing/duplicate | intercompany OIL→MART | Transfer fails with a clear "maintain/correct U_Oil_ItemCode in Jivo Mart OITM" message; **no** partial move (atomic). |
| Sequence row drift / manual DB edits | `_reserve_sequence` | Guarded — it maxes against existing barcodes, so worst case it skips numbers, not duplicates. |
| Dispatch marked complete without SAP | by design | Session shows COMPLETED but SAP inventory unchanged; reconciliation with SAP is a manual/out-of-band step. |
| Pallet mirror not appearing in WMS | `_sync_wms_pallet_inventory` | Only mirrors active pallets sitting in a mapped own-warehouse bin with boxes; otherwise the mirror is dropped — a moved pallet can be "missing" from the WMS map if the warehouse has no `sapWarehouseCode` mapping. |
| Notification service down | verify requests, transfers | Swallowed (best-effort) — the workflow still succeeds; only the alert is lost. |

---

## Improvement opportunities & known gaps

- **SAP write-back is stubbed.** Dispatch completion, pallet/box transfers, and moves never post to SAP. The whole `DispatchSapSyncLog`/`retry_sap_sync`/`SAP_SYNC_FAILED` machinery is dormant. Either wire it or clearly mark it "app-only" in the UI.
- **Partial dispatch has no shortfall follow-up.** A partially-scanned bill can be COMPLETED with no residual session/flag beyond the report.
- **`require_sap_sync_on_completion` setting is inert** — stored but not consulted, because SAP sync is disabled.
- **Bins are strings, not entities.** `current_bin` is free text; there is no bin master validation in this app (WMS owns real locations).
- **Traceability is text-search over `BarcodeAuditLog`** (`icontains`, top 100) — fine for lookups, not for analytics.

---

## Permissions & roles

Permission classes: `permissions.py`.

- **`HasAnyBarcodePermission`** — grants access if the user holds **any** `barcode.*` permission. Gates almost every endpoint (plus `IsAuthenticated` + `HasCompanyContext`).
- **`CanAccessBarcodePalletSync`** — barcode audience **or** a WMS operator (`wms.change_pallet` / `wms.add_movement`). Used only on **pallet list** and **pallet move**, so the WMS bridge can drive them.

Model-level custom permissions:
- **DispatchSession**: `can_view/create/scan/complete/close_barcode_dispatch`, `can_retry_barcode_dispatch_sap`, `can_manage_barcode_dispatch_settings`, `can_view_barcode_dispatch_reports`.
- **IntercompanyTransfer**: `can_view/create/scan/reverse_intercompany_transfer`, `can_manage_intercompany_transfer_settings`.

"**Barcode team**" is defined operationally as anyone with `barcode.view_pallet` (`views._is_barcode_team`); only the team can start/resolve pallet-verify tickets and apply reconcile stock moves. Note: the DRF endpoints themselves gate on the coarse `HasAnyBarcodePermission`, so the fine-grained dispatch/intercompany model perms are enforced primarily by **frontend nav gating** — see the frontend doc. Do not rely on the UI alone for authorization-sensitive actions.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/barcode/`)**
- `models.py` — all entities, choices, constraints, custom permissions.
- `views.py` — every APIView; pagination + CSV export helpers.
- `urls.py` — endpoint map (`/api/v1/barcode/…`).
- `serializers.py` — request/response shapes; dispatch session serializer computes progress/counts + `can_scan`/`can_dispatch`.
- `permissions.py` — `HasAnyBarcodePermission`, `CanAccessBarcodePalletSync`.
- `signals.py` / `notifications.py` — intercompany transfer status notifications.
- `services/barcode_service.py` — boxes, pallets, move/clear/split/add/remove, reconcile, dismantle, repack, WMS mirror.
- `services/dispatch_service.py` — `SapDispatchAdapter` + `BarcodeDispatchService` (lookup, session, scan routing, complete, reports).
- `services/scan_service.py` — barcode parsing + box/pallet lookup (with the 1D fallback guard).
- `services/intercompany_transfer_service.py` + `services/box_ownership.py` — cross-company ownership moves + item-code remap.
- `services/label_service.py` — label data + print logging.
- `services/verify_request_service.py` — pallet-verify ticket workflow.
- `services/oitm_item_service.py`, `services/production_release_service.py` — SAP HANA reads.
- `services/production_integration_service.py`, `services/sap_integration_service.py` — production bridge + (stubbed) SAP stock transfer.

**Key frontend files** — see the companion doc; entry points are
`FactoryFlow/src/modules/barcode/module.config.tsx` (routes/nav),
`api/barcode.api.ts` (endpoint calls), and `pages/BarcodeDispatchPage.tsx`.

---

## Related docs
- Frontend companion: [`FactoryFlow/docs/modules/barcode.md`](../../../FactoryFlow/docs/modules/barcode.md)
- Still-useful design/background (older, partly superseded): `FactoryFlow/docs/modules/barcode-dispatch-design.md`, `barcode-dispatch-sequence-options.md`, `barcode-implementation.md`.
