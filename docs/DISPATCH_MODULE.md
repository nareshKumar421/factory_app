# Dispatch Module — Architecture & Flow

The dispatch module moves a **SAP sales invoice ("bill")** from the planner's
desk onto a physical truck and out the gate. It spans three Django apps —
`dispatch_plans` (booking + status), `gate_core` (gate-in, arrival, docking,
gate-out), and `driver_management`/`vehicle_management` (the physical entities) —
plus the React frontend in `factoryflow`.

This is the canonical "how it works" doc. `GATE_DISPATCH_FLOWS.md` is the shorter
lifecycle summary; `DISPATCH_BILL_MATCHING_PLAN.md` / `dispatch_full_rebuild.md`
are the historical implementation plans.

---

## 1. The one-sentence model

> A **bill** (`DispatchPlan`) is booked to a **vehicle**; the truck is gated in
> (`EmptyVehicleGateIn`) which snapshots the bill as a **cover**; the truck docks
> (`SalesDispatchGateOut`), boxes are scanned, a gate pass prints, and on dispatch
> the cover is consumed, the gate-in retires, and the physical trip
> (`VehicleArrival`) departs.

Everything else — cross-company trucks, late bills, cancels, partial dispatch —
is a variation on that spine.

---

## 2. Data model

| Model | File | Purpose | Key relations |
|---|---|---|---|
| `DispatchPlan` | `dispatch_plans/models.py` | A bill's booking. Unique on `(company, sap_invoice_doc_entry)`. | `vehicle`, `transporter`, `driver`, `linked_vehicle_entry` → `VehicleEntry` |
| `EmptyVehicleGateIn` | `gate_core/models/empty_vehicle_gate_in.py` | Gate-in record for an empty truck arriving to load. | `vehicle_entry` (1-1), `vehicle`, `driver`, `arrival` → `VehicleArrival`; reverse `covers` |
| `EmptyVehicleGateInCover` | same file | The bill-accurate snapshot: one row per bill a DISPATCH gate-in carries. Unique `(empty_vehicle_gate_in, sap_doc_entry)`. | `dispatch_plan`, `sap_doc_entry`, `consumed_at` |
| `VehicleArrival` | `gate_core/models/vehicle_arrival.py` | **One physical truck trip** (NOT company-scoped). Threads per-company gate-ins + dockings. | reverse `gate_ins`, `gate_outs`; `status` INSIDE→LOADING→DEPARTED(+CANCELLED) |
| `SalesDispatchGateOut` | `gate_core/models/sales_dispatch.py` | A docking / gate-out record (the "load"). Unique-active on `(company, document_type, sap_doc_entry)`. | `vehicle_entry`, `dispatch_plan` (primary bill), `arrival`; reverse `documents`, `box_scans` |
| `SalesDispatchGateOutDocument` | same file | One bill carried by a (multi-bill) docking. Unique `(sales_dispatch, document_type, sap_doc_entry)`. | `sales_dispatch`, `dispatch_plan` |
| `SalesDispatchGateOutItem` | same file | SAP line item on the load. Unique `(sales_dispatch, line_num)`. | `sales_dispatch`, `document` |
| `SalesDispatchBoxScan` | same file | A scanned box. Unique `(sales_dispatch, box_barcode)`. | `sales_dispatch` (CASCADE), `document` (SET_NULL), `box`, `scan_log` |
| `EmptyVehicleGateOut` | `gate_core/models/empty_vehicle_gate_out.py` | Physical exit of a truck that leaves empty (repair/reschedule). | `vehicle_entry`, `status` COMPLETED/CANCELLED |

**Two "primary" links you must keep in sync.** A multi-bill docking references its
primary bill twice: `SalesDispatchGateOut.dispatch_plan` (header FK) *and* a
`SalesDispatchGateOutDocument` row. Secondary bills exist only as documents. Status
derivation reads **both** paths (§5), so both must be detached when a bill leaves.

---

## 3. The pipeline (single current stage)

```
BOOKED → EMPTY_IN → READY_TO_DOCK → DOCKED/scanning → PHOTO_ATTACHED
       → READY_FOR_GATEPASS → GATEPASS_PRINTED → PRINT_COMMITTED → DISPATCHED
                                                          (+ REJECTED / CANCELLED)
```

The stage is **derived**, never stored, by `compute_pipeline_stage(plan)`
(`dispatch_plans/services.py`) from: the plan's `booking_status`, its
`linked_vehicle_entry` (gate-in), and its **representative docking gate-out**. The
`"<status> at <module>"` label ("docked at dock", "dispatched at sales dispatch
out") comes from `pipeline_module_status` / `compute_pipeline_status`.

---

## 4. Stage-by-stage flow

### 4.1 Vehicle Linking (planner) — `BOOKED`
`PATCH /api/v1/dispatch-plans/bills/{docEntry}/plan/` → `DispatchLinkingService`
(`dispatch_plans/services.py`). Sets `booking_status=BOOKED`, `vehicle`,
transporter/driver. Batch link via `linked_invoice_doc_entries`
(`update_linked_plans`, now **atomic** — all-or-nothing). Frontend
`DispatchLinkingSheet.tsx`.

- **Link lock** (`_assert_link_not_locked`): once the gate-in's `VehicleEntry` is
  COMPLETED the transport identity is frozen. The UI only promotes a *PENDING*
  bill to BOOKED on link, so editing an advanced bill can't demote it.
- **Late booking** (`_link_completed_empty_in`): a bill booked *after* gate-in
  attaches to an existing cover, else auto-attaches to the inside truck
  (`attach_bill_to_inside_vehicle`), else falls to the correction path.

### 4.2 Empty Vehicle In (gate) — `EMPTY_IN → READY_TO_DOCK`
`EmptyVehicleGateInListCreateView` (`gate_core/views.py`). On **complete**, for a
DISPATCH gate-in: `record_dispatch_covers` snapshots one `EmptyVehicleGateInCover`
per booked, unlinked bill and sets each plan's `linked_vehicle_entry`; then
`replicate_dispatch_gate_in_across_companies` wraps the truck in a `VehicleArrival`
and creates sibling gate-ins for other companies with booked bills (§6).

- **Inside-guard** (`gate_core/views.py` ~L441): a vehicle can be inside only once.
  Blocks a second entry while a live gate-in exists (`retired_at IS NULL`, VE not
  CANCELLED, no completed empty-vehicle/BST gate-out).
- **Idempotent complete**: re-completing an already-COMPLETED gate-in no-ops (so a
  double-submit can't create a duplicate arrival).
- **Auto-attach** (`attach_bill_to_inside_vehicle` / `_attach_bill_via_arrival`): a
  late bill on an already-inside truck joins the current load, bounded by the
  photo-lock cutoff (`_LOAD_LOCKED_DOCKING_STATUSES`).

### 4.3 Docking — `DOCKED → … → PRINT_COMMITTED`
A bill is **dockable** iff `BOOKED`, with an unconsumed cover on a live
(non-retired, COMPLETED-VE) gate-in, and not already on an active docking
(`_pending_dispatch_plan_base`, `gate_core/views_sales_dispatch.py`).
`SalesDispatchGateOutListCreateView.post` creates a docking from the selected
bills → boxes scanned (`SalesDispatchBoxScan`) → truck photo attached (locks the
load) → gate pass printed → **commit** (row-locked, so no double-commit).

- **De-fragmentation** (`_append_to_open_docking`): if the truck+company already
  has an open (`DOCKED`) docking, new bills **append** to it (reusing the
  pre-fetched SAP docs — no second round-trip) instead of spawning a second
  docking. Returns `appended: true` (HTTP 200); dropped bills surface as warnings.
- **Late auto-merge** (`merge_bill_into_open_docking` / `add_plan_to_open_docking`)
  + manual "Add to DOCK" fallback (`SalesDispatchAddDocumentView`).
- **Header** aggregates are recomputed from document rows
  (`recompute_header_from_document_rows`) on every append/remove.

### 4.4 Sales Dispatch Out (gate-out) — `DISPATCHED`
`SalesDispatchMarkDispatchedView` (row-locked; idempotent when already DISPATCHED).
For a cross-company arrival it dispatches the **whole truck** at once
(`dispatch_arrival`); otherwise `mark_docking_dispatched`. On dispatch:
`consume_covers_for_dispatched_plans` marks covers consumed → `_retire_if_fully_consumed`
retires the gate-in → `_depart_arrival_if_complete` departs the trip once every
company chain has retired.

---

## 5. Pipeline "Vehicle Status" derivation (read carefully)

`_pick_representative_gate_out(plan)` picks the gate-out that represents a bill's
current state, from **two** paths:
1. direct dockings — `plan.sales_dispatch_gate_outs` (header `dispatch_plan` FK);
2. rode-along dockings — `plan.sales_dispatch_gate_out_documents` (a secondary
   bill on a multi-bill load).

**Only `is_active` rows count.** A bill removed from a load (its document
soft-deleted) or a docking that was cancelled/detached must NOT keep deriving that
docking's stage. Both the in-Python loop and `pipeline_gate_out_prefetch`'s
querysets filter `is_active=True` (fixed 2026-07 — see §11). The newest active
gate-out in an active status wins; else the newest is used and CANCELLED/REJECTED
maps to the REJECTED stage.

Consequence: to make a bill "leave" a docking cleanly you must **null the
`dispatch_plan` FK** on both the document *and* the header (and/or soft-delete the
document), not just flip the docking status — otherwise the derivation rehydrates
the dead docking. The unwind paths (§7) all do this now.

Prefetches (`pipeline_gate_out_prefetch`, `empty_in_pipeline_prefetch`) keep the
stage O(1) per bill; the pipeline board uses the shared prefetch too (no N+1).

---

## 6. Cross-company arrivals

One physical truck can carry Jivo Oil + Beverages + Mart bills. Because each
company issues its **own SAP gate pass**, they can't share one docking. So the
model is: **one `VehicleArrival`** (global, not company-scoped) grouping **one
`EmptyVehicleGateIn` + one `SalesDispatchGateOut` per company**, with per-company
covers. `replicate_dispatch_gate_in_across_companies` / `_create_company_gate_in`
build the sibling chains; `create_vehicle_arrival` (`views_arrival.py`) is the
direct entry point. Legacy single-company records have `arrival = null`.

The arrival is the "one physical truck" unit: it departs only when **all** company
chains have dispatched (`_depart_arrival_if_complete`), and a stale/departed
arrival must never be reused for a fresh gate-in (reuse requires ≥1 live gate-in).

The frontend groups the per-company records into **one expandable vehicle row** on
the gate + docking boards (`salesDispatchVehicleGrouping.ts`,
`buildEmptyVehicleGroups`).

---

## 7. Unwind / correction paths (each settles differently)

| Path | Trigger | What it does to the bills |
|---|---|---|
| **Cancel / Reject** | `SalesDispatchCancelView` / `SalesDispatchRejectView` | `settle_cancelled_docking` (`gate_core/services/sales_dispatch_release.py`): null header + document `dispatch_plan` FKs (bills revert to their gate-in stage, **re-dockable on the same truck**) + `unconsume_covers_for_plans` (a no-op pre-dispatch). |
| **Remove a bill (C1)** | `SalesDispatchRemoveDocumentView` | Bill goes to a **future trip**: deactivate document + items, void the cover, reset plan to `BOOKED` + `linked_vehicle_entry=None`, un-attribute its box scans, recompute the header. |
| **Empty-vehicle-out** | `EmptyVehicleGateOutListCreateView` / `release_dispatch_plans_for_empty_out` | Truck leaves empty: retire the gate-in (`EMPTY_OUT`), cancel its un-scanned dockings, release BOOKED bills to `linked_vehicle_entry=None`, and **depart the arrival**. |

The distinction matters: **cancel/reject** keep the bills gated in (redo on the
same truck); **C1** and **empty-out** unlink them (future trip / fresh gate-in).

---

## 8. Key invariants & guards

- **One live gate-in per (vehicle, company)**; **one open arrival per vehicle**.
- **A retired gate-in makes none of its bills dockable** — a returning truck needs
  a fresh gate-in with fresh covers.
- **Photo-lock cutoff**: once a docking is photo-attached the load is fixed; late
  bills can't auto-join (`_LOAD_LOCKED_DOCKING_STATUSES`).
- **Same-branch only**: bills from different SAP branches are separate physical
  flows (one gate pass = one branch/GSTIN) — never merged.
- **Vehicle identity** is `vehicle_id`, never the raw plate string (`DL01LAM0715`
  vs `DL1LAM0715` are different `Vehicle` rows).

---

## 9. Concurrency

- **Row locks**: dispatch, commit-print, and gatepass-print load the docking
  `select_for_update` inside `transaction.atomic` before the status guard, so
  concurrent calls serialize (no double-dispatch / double-commit).
- **Atomic batch link**: `update_linked_plans` wraps its per-bill loop so a
  partial batch can't commit.
- **Gate-pass numbers** use a `select_for_update` sequence
  (`ArrivalGatepassSequence.next_gatepass_no`).
- **Entry numbers** (`generate_entry_no` on gate-in / docking / arrival) still use
  `max(...)+1`; a `select_for_update` sequence is the deferred fix (§12).

---

## 10. Frontend map (`factoryflow`)

| Screen | Component | Notes |
|---|---|---|
| Vehicle Linking | `vehicle-management/components/DispatchLinkingSheet.tsx`, `DispatchLinkingTable.tsx` | link/edit; per-bill lock via `is_vehicle_link_locked` |
| Empty Vehicle In | `gate/pages/emptyVehicleInPages/EmptyVehicleInPage.tsx`, `EmptyVehicleInNewPage.tsx` | Expected-Dispatch list (`buildExpectedDispatchVehicles`), per-vehicle grouping |
| Docking / Sales Dispatch Out | `gate/pages/customerSalesFlow/SalesDispatchDashboardPage.tsx` | one component, `isGateOutMode` toggles; per-vehicle grouping (`salesDispatchVehicleGrouping.ts`) |
| Pipeline board | `dashboards/dispatch-pipeline/*` | |

**react-query cache keys** (invalidate these together after a dispatch mutation):
`['salesDispatch']`, `['arrivals']`, `['emptyVehicleIn']`, `['dispatch-plans']`,
`['dispatch-pipeline']`, `['vehicleEntries']`. The invalidation helpers live in
each module's `*.queries.ts`.

---

## 11. Recent changes — 2026-07 audit remediation

A full audit of this flow (both repos) drove the following fixes, shipped on
branch `fix/dispatch-flow-audit-remediation`. They are the permanent fixes for the
recurring "status won't update / truck stuck inside / bill stuck DISPATCHED"
problems that previously needed manual DB surgery.

**P0 — stale status (root cause)**
- `_pick_representative_gate_out` + `pipeline_gate_out_prefetch` now filter
  `is_active`, so a removed/cancelled docking no longer keeps deriving its stage.
  `DispatchPipelineView` uses the shared prefetch (fixes an N+1).
- Frontend: repaired a **dead cache key** (`salesDispatchGateOuts` → `salesDispatch`)
  so linking a bill actually refreshes the board; added the missing
  `arrivals`/`emptyVehicleIn`/`salesDispatch`/`dispatch-pipeline` invalidations.

**P1 — consistent unwind + shipped-bug fixes**
- Cancel/reject now detach bills (revert to gate-in stage) + `unconsume_covers`
  (new `settle_cancelled_docking`). Previously they settled nothing.
- C1 remove now nulls `document.dispatch_plan`, recomputes the header, and
  un-attributes box scans.
- `release_dispatch_plans_for_empty_out` now **departs the arrival** (single-company
  empty-out previously left it LOADING forever).
- Docking-create reuse/append: passes pre-fetched SAP docs (no double round-trip /
  no SAP-in-transaction 500), surfaces dropped bills as warnings, adds `appended`.
- Frontend: link-edit no longer demotes `booking_status`; board badges count
  vehicle groups (match the grouped rows).

**P2 — concurrency & robustness**
- Row locks on mark-dispatched + commit-print; idempotent when already advanced.
- Atomic batch link; idempotent complete-gate-in.
- `formatNumber` guards null/string SAP decimals (no table crash); cross-company
  `all_companies` on the New-entry guard; keyboard-accessible group header rows.

**Production data cleanups (one-time)**
- Released a stuck cross-company trip (DL01LAN4204).
- Zombie sweep: retired 11 gate-ins (cancelled-VE, not retired) and departed 14
  spent arrivals (0 live gate-ins) — data is now consistent (see §12).

---

## 12. Deferred / known items

- **Partial-unique constraint migrations** — `(vehicle, company) WHERE live` on
  gate-ins and `(vehicle) WHERE open` on arrivals. The 2026-07 zombie sweep made
  the data satisfy them (all violation checks = 0); bundle the same sweep as a
  data-migration step so any newly-accumulated violations are cleaned at deploy.
- **Entry-number sequence locking** — move `generate_entry_no` to a
  `select_for_update` sequence (mirror `ArrivalGatepassSequence`) to close the
  duplicate-key race under concurrent creates.
- **Abandoned-docking sweep** — a `DOCKED`/0-scan docking never retires its gate-in
  so the arrival stays LOADING; needs a scheduled empty-out/flag past an SLA.
- **Global error toast** (`factoryflow` `core/api/client.ts`) fires on every
  non-401/404 → double toasts where the caller also toasts; make it opt-in.
- **Dead code**: `unconsume_covers_for_plans`'s reopen path is only reachable via
  cancel/reject (now wired); verify no other un-dispatch path needs it.
