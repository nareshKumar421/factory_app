# Gate Management — Inbound Vehicle & Material Entry (Backend)

> Server-side reference for the inbound gate. Covers the Django apps that record a
> truck arriving at the factory, the security check, the weighbridge, the raw-material
> PO receipt, and the hand-off to QC and SAP GRPO. **Trust this doc + the code over
> older per-app READMEs — several of those are stale (see "Known stale docs" below).**
>
> Frontend counterpart: [`FactoryFlow/docs/modules/gate.md`](../../../FactoryFlow/docs/modules/gate.md)

## Overview — what it does & who uses it

The inbound gate is the **single front door** for everything a supplier's truck brings
into a plant: purchased raw/packing material and assets, daily-need/canteen goods,
maintenance spares, construction material, and fixed assets. A **gate operator** (security
desk) records the vehicle, driver and a safety check; a **stores/receiving operator**
receives the PO lines against SAP; **QC** inspects the material via an arrival slip; and
finally the entry is **completed and locked**, which signals the **GRPO team** to post the
Goods Receipt PO into SAP.

The module maps to five Django apps plus `vehicle_management` (which owns the master data
and the actual create endpoint):

| App | Responsibility |
|-----|----------------|
| `vehicle_management` | `Vehicle`, `Transporter`, `VehicleType` masters **and the `VehicleEntry` create/list endpoints** (the gate "root" record) |
| `driver_management` | `Driver` master + the `VehicleEntry` model definition |
| `security_checks` | `SecurityCheck` (one per entry): vehicle/tyre/fire/seal/alcohol checks |
| `weighment` | `Weighment` (one per entry): gross / tare / net weighbridge capture |
| `raw_material_gatein` | `POReceipt` / `POItemReceipt`: SAP-validated PO line receipt + gate completion |
| `gate_core` | Shared base models, status enum + transition guard, lock manager, gate attachments, the read-only "full view" endpoints, **and the empty-vehicle gate-in + `VehicleArrival` lifecycle** (the inbound-vehicle side of a dispatch truck — covered below). The outbound *tail* (docking, box scans, gatepass, mark-dispatched) is a separate dispatch module and is cross-linked, not detailed here. |

Two families of inbound event share this spine:

- **Material entry** — a supplier truck brings goods: raw/packing material and assets, daily-need,
  maintenance, construction, fixed assets. Daily-need, maintenance, construction and fixed-asset
  entries reuse the exact same `VehicleEntry` + security + weighment + attachment spine; only their
  line-detail app differs (`daily_needs_gatein`, `maintenance_gatein`, `construction_gatein`,
  `fixed_asset_gatein`). This doc uses **raw material** as the canonical, most complex material path.
- **Inbound (empty) vehicle** — an *empty* truck arrives to be loaded for an outbound dispatch. The
  gate records an `EmptyVehicleGateIn` and threads it onto a `VehicleArrival` (one physical trip).
  The loading itself is the dispatch module; the **gate-in and the arrival lifecycle live here** and
  carry one of this module's hardest-won invariants (auto-close on full dispatch — see flow 5).

## Key concepts & entities

- **`VehicleEntry`** (`driver_management/models/vehicle_entry.py`) — the gate root. One row per
  truck visit. Extends `GateEntryBase`; carries `company` (FK, PROTECT), `vehicle`, `driver`,
  `entry_type`, `entry_no` (unique), `status`, `is_locked`, `entry_time`, `remarks`.
  `entry_type` choices: `RAW_MATERIAL`, `DAILY_NEED`, `MAINTENANCE`, `CONSTRUCTION`,
  `FIXED_ASSET`, `EMPTY_VEHICLE`, `BST_IN`, `BST_RETURN`, `JOB_WORK`, `SALES_DISPATCH`.
- **`GateEntryBase` / `BaseModel`** (`gate_core/models/base.py`, `gate_entry.py`) — audit fields
  (`created_by`/`updated_by`/timestamps/`is_active`) + `entry_no`, `status`, `is_locked`.
  **`GateEntryBase.save()` raises `ValueError` if the row is already locked** — the hard write guard.
- **`SecurityCheck`** (`security_checks/models/security_check.py`) — OneToOne to `VehicleEntry`.
  Booleans for vehicle/tyre/fire condition, `seal_no_before/after`, alcohol test, `inspected_by_name`,
  and `is_submitted`. **`save()` raises `ValueError` once `is_submitted` is true** (submit = permanent lock).
- **`Weighment`** (`weighment/models/weighment.py`) — OneToOne to `VehicleEntry`. `gross_weight`,
  `tare_weight`, computed `net_weight` (`= gross − tare`, else `0`), `challan_weight` (challan-declared,
  for comparison), `weighbridge_slip_no`, first/second weighment times.
- **`POReceipt`** (`raw_material_gatein/models/po_receipt.py`) — a SAP PO received against one entry.
  `po_number`, `supplier_code/name`, and the SAP link snapshot needed for GRPO: `sap_doc_entry`
  (OPOR DocEntry → GRPO BaseEntry), `branch_id` (BPLId), `vendor_ref`, `po_date`. Unique per
  `(vehicle_entry, po_number)`.
- **`POItemReceipt`** (`raw_material_gatein/models/po_item_receipt.py`) — a received PO line.
  `sap_line_num` (POR1 LineNum → GRPO BaseLine), `unit_price`, `tax_code`, `warehouse_code`,
  `gl_account`, `variety`, `ordered_qty`, `received_qty`, `accepted_qty`, `rejected_qty`,
  computed `short_qty` (`= ordered − received`), `uom`.
- **`POReplacementLog`** — audit trail for swapping a wrong PO on a QC-sent-back arrival slip.
- **Arrival slip / inspection** — live in `quality_control` (`MaterialArrivalSlip`, `RawMaterialInspection`),
  attached per `POItemReceipt`. The gate creates the slip; QC works it. See that app's docs.
- **`GateEntryStatus`** (`gate_core/enums.py`) — the lifecycle enum (below).

### Status lifecycle

`GateEntryStatus`: `DRAFT`, `SECURITY_CHECK_DONE`, `ARRIVAL_SLIP_SUBMITTED`,
`ARRIVAL_SLIP_REJECTED`, `IN_PROGRESS`, `QC_PENDING`, `QC_IN_REVIEW`, `QC_AWAITING_QAM`,
`QC_REJECTED`, `QC_HOLD`, `QC_COMPLETED`, `COMPLETED`, `CANCELLED`.

Phases (`get_entry_phase`): **GATE** (draft→in-progress), **QC** (pending→hold), **DONE**
(`QC_COMPLETED`/`COMPLETED` = `GRPO_READY_STATUSES`), **CANCELLED**. Transitions are guarded by
`ALLOWED_TRANSITIONS` in `gate_core/services/status_guard.py` (`validate_status_transition`).

> **Reality note:** the enum defines `SECURITY_CHECK_DONE`, but the current raw-material code
> path does **not** use it — saving a security check flips `DRAFT → IN_PROGRESS`, and receiving a
> PO moves the entry straight to `QC_PENDING`. `ARRIVAL_SLIP_SUBMITTED/REJECTED` and the `QC_*`
> states are driven by the `quality_control` app, not by these apps.

### Inbound-vehicle entities (empty-vehicle-in & arrival)

- **`EmptyVehicleGateIn`** (`gate_core/models/empty_vehicle_gate_in.py`) — the gate-in of an *empty*
  truck arriving for an outbound move. Company-scoped (FK, PROTECT), one-to-one to a `VehicleEntry`
  (`entry_type=EMPTY_VEHICLE`), `reason` ∈ {`DISPATCH`, `REPAIR_MOVEMENT`, `JOB_WORK`, `OTHER`},
  `entry_no` `EVGI-YYYYMMDD-NNNN`. Key lifecycle field **`retired_at`** (+ `retired_reason` ∈
  {`DISPATCHED`, `EMPTY_OUT`, `CANCELLED`}): a gate-in is *retired* once every bill it carries has
  dispatched or the truck left empty. A retired gate-in no longer counts as "inside" and stops making
  its bills dockable.
- **`EmptyVehicleGateInCover`** — the specific dispatch bills (by SAP `sap_doc_entry`) a `DISPATCH`
  gate-in is gated in to carry, snapshotted at gate-in time by `record_dispatch_covers`. `consumed_at`
  is set when that bill's docking is dispatched; the gate-in retires once **all** covers are consumed.
- **`VehicleArrival`** (`gate_core/models/vehicle_arrival.py`) — **one physical truck trip**, NOT
  company-scoped. It threads the per-company `EmptyVehicleGateIn`s + dockings of a single truck (each
  points at it via a nullable `arrival` FK) so a truck carrying bills for several companies is *one*
  trip weighed once and leaving once. `status` ∈ {`INSIDE`, `LOADING`, `DEPARTED`, `CANCELLED`};
  `arrival_no` `ARV-YYYYMMDD-NNNN`. Legacy single-company gate-ins keep `arrival=null`.

## End-to-end flows

### 1. Raw-material inbound (happy path, as the server sees it)

1. **Create the entry.** `POST /api/v1/vehicle-management/vehicle-entries/` with
   `entry_no`, `vehicle`, `driver`, `entry_type=RAW_MATERIAL` (form-urlencoded). Company comes
   from the `Company-Code` header (`HasCompanyContext`). Row is created `status=DRAFT`.
   A `post_save` signal (`raw_material_gatein/signals.py`) fires
   `notify_gate_entry_created` → notifies the `raw_material_gatein` group.
   **`entry_no` is supplied by the caller, not generated server-side** (the frontend builds
   `GE-<year>-<last-4-of-epoch-ms>`), and the column is `unique` — see edge cases.
2. **Security check.** `POST /api/v1/security-checks/gate-entries/{id}/security/`
   (`SecurityCheckCreateUpdateAPI`) `get_or_create`s the `SecurityCheck` and, if the entry is
   `DRAFT`, flips it to `IN_PROGRESS`. A later `POST /security/{security_id}/submit/`
   sets `is_submitted=True` and **permanently locks the security check**.
3. **Receive PO lines.** `POST /api/v1/raw-material-gatein/gate-entries/{id}/po-receipts/`
   (`ReceivePOAPI`). The view calls `SAPClient.get_open_pos(supplier_code)`, matches the requested
   `po_number`, and for each requested line: validates the line exists & item code matches, runs
   `validate_received_quantity` (**≤ 110 % of ordered**, `> 0`), and snapshots the SAP header
   (`sap_doc_entry`, `branch_id`, `vendor_ref`, `po_date`) and line fields
   (`unit_price`, `tax_code`, `warehouse_code`, `gl_account`, `variety`). On success the entry is
   set to `QC_PENDING` and `notify_po_received` fires. All under `@transaction.atomic`.
4. **Arrival slip → QC.** For each `POItemReceipt` the gate raises a `MaterialArrivalSlip`
   (`quality_control`) with billing/commercial-invoice/e-way/bilty details + optional
   Certificate of Analysis/Quantity, and submits it to QA. QC then inspects
   (`RawMaterialInspection`) → `ACCEPTED` / `REJECTED` / `HOLD`.
5. **Weighment (optional).** `POST /api/v1/weighment/gate-entries/{id}/weighment/` upserts the
   `Weighment`. Net weight is auto-computed. First call typically records `gross_weight`; a later
   call records `tare_weight`.
6. **Complete & lock.** `POST /api/v1/raw-material-gatein/gate-entries/{id}/complete/`
   (`CompleteGateEntryAPI` → `complete_gate_entry`). Guards: entry editable, **≥ 1 PO item exists**,
   and **every item has a completed QC inspection (ACCEPTED or REJECTED)** via
   `quality_control.services.rules.can_complete_gate`. On success: `status=COMPLETED`,
   `is_locked=True`, and `notify_gate_entry_completed` notifies the **`grpo`** group
   ("ready for GRPO", deep-links `/grpo/material/preview/{id}`).
7. **GRPO into SAP (downstream, `grpo` app).** The GRPO team posts a Goods Receipt PO using the
   snapshot: `POReceipt.sap_doc_entry` (BaseEntry), `branch_id` (BPLId), each
   `POItemReceipt.sap_line_num` (BaseLine) and `accepted_qty`. SAP's own
   `SBO_SP_TransactionNotification` stored procedure enforces posting rules server-side (see
   Integrations) — some of which the gate itself never checks.

### 2. Correcting a PO before it reaches QC

`PUT /api/v1/raw-material-gatein/gate-entries/{id}/po-receipts/{poId}/` (`POReceiptDetailAPI`)
re-fetches the PO from SAP and rewrites its lines — allowed only while `_po_receipt_lock_reason`
returns `None` (entry not locked/completed/cancelled, no posted GRPO, no GRPO line activity,
**arrival slip not yet submitted to QC and no inspection started**).

### 3. Replacing the wrong PO after QC sent it back

`POST /.../po-receipts/{poId}/replace/` (`POReceiptReplaceAPI`). Explicit, **reason-mandatory**
correction: only when the arrival slip is in a **sent-back** state (`ARRIVAL_SLIP_REJECTED` /
`sent_back_at`), with no started inspection and no GRPO activity. It swaps the PO header,
**deletes the old items (cascading their draft slips/inspection)**, re-fetches the new PO from SAP,
and writes a `POReplacementLog` (old→new PO, `supplier_changed`, reason, prior QC remark).

### 4. Deleting an in-progress entry

`DELETE /api/v1/raw-material-gatein/gate-entries/{id}/` (`RawMaterialGateEntryDeleteAPI`).
Allowed only in the **GATE phase** (`GATE_PHASE_STATUSES`), not locked, no posted GRPO, no GRPO
line activity, no started inspection. Hard-deletes the entry and cascades PO receipts, arrival
slips, security check, weighment and attachments.

### 5. Empty-vehicle-in & the cross-company arrival lifecycle (inbound vehicle)

1. **Gate the empty truck in.** `POST /api/v1/gate-core/empty-vehicle-ins/`
   (`EmptyVehicleGateInListCreateView`, `gate_core/views.py`). A **guard** first blocks a second
   gate-in while the vehicle already has a live (non-retired, not empty-outed, not cancelled) one:
   *"{vehicle} is already inside under gate entry {EVGI-…} and has not left yet."* The owning company
   is resolved from the truck's **booked bills**, not the active `Company-Code` (`_resolve_dispatch_company`).
2. **Snapshot covers + go cross-company.** On completion, `record_dispatch_covers` links the vehicle's
   `BOOKED` + unlinked `DispatchPlan`s as `EmptyVehicleGateInCover`s, and
   `replicate_dispatch_gate_in_across_companies` wraps the gate-in in a `VehicleArrival` and mints
   sibling `COMPLETED` gate-ins for every *other* company that has bills on the same truck — reusing a
   live arrival only, never a stale one (services in `empty_vehicle_dispatch.py`).
3. **Load & dispatch (dispatch module).** Docking, box scans and the combined gatepass happen in the
   dispatch flow (`views_sales_dispatch.py`, `views_arrival.py`); each bill's cover is marked
   `consumed_at` when its docking is dispatched (`consume_covers_for_dispatched_plans`).
4. **Auto-close.** When the **last** cover of a gate-in is consumed it retires
   (`_retire_if_fully_consumed`); when **every** company chain on the arrival is retired the arrival
   auto-departs (`_depart_arrival_if_complete` → `status=DEPARTED`, exit stamped). (There is no
   un-dispatch flow: reject/cancel are blocked once a docking is `DISPATCHED`, so a consumed cover is
   never reversed.) **This auto-close is the invariant** — without it the arrival
   would linger `LOADING`, the next visit's gate-in would glue onto the stale trip, and the truck would
   look perpetually "inside" (see edge cases).
5. **Manual exit.** If the truck leaves empty, `POST /arrivals/{id}/empty-out/` /
   `empty-vehicle-outs/` retires the gate-in (`EMPTY_OUT`); `POST /arrivals/{id}/depart/`
   (`VehicleArrivalDepartView`) is the explicit, idempotent whole-trip exit.

## Critical business rules & invariants

- **Locked = frozen.** `GateEntryBase.save()` raises `ValueError` on any write to a locked entry;
  `gate_core/services/lock_manager.ensure_editable()` raises `PermissionError`. Completion sets
  `is_locked=True`.
- **Security check is one-way.** Once `is_submitted`, the model refuses further saves.
- **Completion requires QC, not weight.** `complete_gate_entry` requires ≥ 1 PO item and QC
  complete for **all** items; **weighment is explicitly optional** (see the docstring — this
  contradicts the stale `weighment/README.md`).
- **Over-receipt tolerance 10 %.** `validate_received_quantity` rejects `received_qty > 1.10 ×
  ordered_qty` and any non-positive quantity.
- **One PO per number per entry.** `unique_together (vehicle_entry, po_number)`; duplicate → 400.
- **`entry_no` is globally unique** — a collision on the client-generated value raises
  `IntegrityError`.
- **PO editability collapses forward.** Editable at gate → locked once the arrival slip is
  submitted / QC starts / GRPO posts. The serialized `is_editable` + `lock_reason` tell the UI why.
- **Status transitions are guarded** (`validate_status_transition`); illegal jumps raise `ValueError`.
- **Company scoping (inbound).** Raw-material reads/writes resolve the entry by the **active
  `Company-Code`** (`company=request.company.company`). Weighment and gate-attachment endpoints are
  the exception — they resolve across `user_company_ids(request)` so a sibling-company entry can be
  weighed/attached without switching companies. (The broad cross-company aggregation lives in the
  outbound dispatch/empty-vehicle flows, not here — see the memory note on the cross-company boundary.)
- **One truck inside once.** The empty-vehicle-in guard rejects a new gate-in for a vehicle that still
  has a live (non-retired, not empty-outed, not cancelled) gate-in across any of the user's companies.
- **Arrival auto-closes on full dispatch; a stale arrival is never reused** (confirmed in
  `services/empty_vehicle_dispatch.py`). `_depart_arrival_if_complete` departs the arrival only once
  **all** its company chains have retired (the "one deliberate exit" rule); the reuse/late-bill paths
  (`replicate_dispatch_gate_in_across_companies`, `attach_bill_to_inside_vehicle`) match only an
  arrival with **≥ 1 live** (`retired_at IS NULL`) gate-in. This is the core fix for "truck stuck
  inside" — but three gaps can still wedge a gate-in (see edge cases).

## Integrations & cross-module boundaries

- **SAP Service Layer (`sap_client`).** `get_open_pos(supplier_code)` supplies the PO header + open
  lines during receipt. Errors are normalized: `SAPConnectionError → 503`, `SAPDataError → 502`,
  "PO not found" → `400`. The receipt view never posts to SAP — it only reads.
- **SAP `SBO_SP_TransactionNotification`.** GRPO/A-P documents are validated by this SAP-side stored
  procedure at **post time**, not by our code. It is the source of `(2000xx)` rejections — e.g.
  **`200032`: gross weight mandatory for item group `105`**. Because gate completion does **not**
  require weighment, an entry can complete cleanly and only fail later at GRPO posting if the
  weighbridge weight is missing for such an item group. `grep` confirms `200032` appears nowhere in
  this repo — it is pure SAP config. To inspect/modify, read the SP in SAP, not Python.
- **`quality_control`.** Owns arrival slips and inspections attached per `POItemReceipt`; drives the
  `ARRIVAL_SLIP_*` and `QC_*` statuses and the `can_complete_gate` rule.
- **`grpo`.** Downstream consumer. Reads the PO snapshot fields to post the GRPO; also captures a
  tare weight at GRPO time that back-fills the same `Weighment` row (`grpo/services.py`).
- **`notifications`.** Group-targeted, `transaction.on_commit`-deferred pushes at entry-created,
  security-done, weighment-recorded, PO-received (→ `raw_material_gatein` group) and
  entry-completed (→ `grpo` group).
- **Outbound dispatch tail (same `gate_core` app, cross-linked module).** The gate-in + arrival
  lifecycle is documented here (flow 5); the *loading* tail — `SalesDispatchGateOut` (docking), box
  scans, the combined gatepass, mark-dispatched — is the **dispatch module** (`views_sales_dispatch.py`,
  `views_arrival.py`, `services/sales_dispatch_*`, and `gate_core/docs/sales_dispatch.md`). The two
  meet at the cover-consumption / arrival-departure hand-off in `empty_vehicle_dispatch.py`.

## Real-world edge cases

- **SAP down while receiving a PO** — trigger: `get_open_pos` raises `SAPConnectionError` →
  behaviour: `503`, no `POReceipt` created (atomic) → symptom: operator sees "SAP system is
  currently unavailable"; entry sits at `IN_PROGRESS` with no PO → risk: truck waits at gate until
  SAP returns; nothing queued.
- **`entry_no` collision** — trigger: two RM entries created within the same 10-second window get
  the same `GE-<year>-<last4ms>` → behaviour: second insert raises `IntegrityError` → symptom:
  generic "Failed to save gate entry"; retry regenerates a new suffix and usually succeeds → risk:
  real (if low-probability) failure at busy gates; the id is not server-authoritative.
- **Missing weighbridge weight for item group 105** — trigger: complete + GRPO an item-group-105
  material with no gross weight → behaviour: gate completes fine (weight optional), SAP SP rejects
  the GRPO with `200032` → symptom: GRPO team sees a SAP `(200032)` error, not a gate error → risk:
  failure surfaces one hand-off downstream; operator must reopen weighment path and repost.
- **Stale arrival / truck stuck "inside"** (the hardest-won gate-vehicle bug) — trigger: a dispatch
  gate-in never retires, so its `VehicleArrival` never auto-departs. Three confirmed code gaps do
  this: (a) an *emptied* gate-in with **0 active covers** — `_retire_if_fully_consumed` returns early
  (all-consumed is vacuously false) so it never retires (e.g. after a console "Remove/Unlink All");
  (b) a **partial trip** where one bill dispatches and a never-loaded `BOOKED` phantom bill keeps the
  gate-in from reaching all-consumed; (c) a docking left at **`PRINT_COMMITTED`** (gatepass printed,
  never weighed-out/dispatched) so its cover is never consumed → behaviour: the arrival stays
  `LOADING` and is treated as live → symptom: the next time that truck arrives, the empty-vehicle-in
  guard rejects it — *"{vehicle} is already inside under {EVGI-…}"* — and the Inside Vehicle Manager
  shows a truck that physically left days ago → risk: only a manual empty-vehicle-out / console unwind
  frees it; underneath it all are **phantom `BOOKED` bills** the planner must cancel or actually
  dispatch (code can't decide which). The gate C1 fix made console Remove/Unlink retire+depart; gaps
  (b) and (c) remain open. Root causes and the manual-unstick recipe are in the team memory notes.
- **Wrong PO already submitted to QC** — trigger: operator booked the wrong PO and submitted the
  slip → behaviour: edit is blocked (`lock_reason`), only **Replace** works and only after QC sends
  it back → symptom: "This PO cannot be edited after its arrival slip is submitted to QC" → risk:
  needs a QC round-trip to fix a gate typo.
- **Over-/under-receipt** — trigger: `received_qty > 110 %` of ordered → behaviour: `400` from
  `validate_received_quantity`; under-receipt is allowed and recorded as `short_qty` → symptom:
  operator sees the 110 % message; shorts pass silently → risk: silent shorts rely on QC/GRPO to catch.
- **Complete before QC finishes** — trigger: `complete` with any item lacking an ACCEPTED/REJECTED
  inspection → behaviour: `400` "QC is not completed for all items" → symptom: operator blocked at
  Review → risk: none (intended guard).
- **Duplicate PO on one entry** — trigger: same `po_number` added twice → behaviour: `400` "already
  added" (pre-check + `IntegrityError` fallback).
- **Cross-company "blank" RM entry** — trigger: viewing an RM entry created under a sibling company
  while a different `Company-Code` is active → behaviour: RM read filters by active company → symptom:
  `404` / not listed → risk: expected (inbound RM is single-company; only weighment/attachments span
  companies).

## Failure modes / what can break

| Failure | Server behaviour | Operator/manager-visible symptom |
|---------|------------------|----------------------------------|
| SAP Service Layer down | `503` on PO receive / SAP list endpoints | "SAP system is currently unavailable. Please try again later." |
| SAP data/read error | `502` | "Failed to retrieve PO data from SAP." |
| PO not open for supplier | `400` | "Open PO {n} was not found for this supplier." |
| GRPO rejected by SAP SP | error surfaced in `grpo` app | SAP `(2000xx)` message (e.g. gross-weight mandatory `200032`) |
| Edit after lock/submit | `ValueError`/`400` | "…locked and cannot be modified" / "…already posted to GRPO" |
| Complete without QC | `400` | "QC is not completed for all items." |
| `entry_no` clash | `IntegrityError` (500-class) | generic save failure at Step 1 |
| Empty-vehicle-in blocked (already inside) | `400` from the gate-in guard | "{vehicle} is already inside under gate entry {EVGI-…} and has not left yet." — often a stale arrival that never auto-departed |
| Stale arrival never departs | gate-in `retired_at` stays null; arrival stuck `LOADING` | truck shows "inside"/"at dock" days later; next visit's gate-in blocked; needs manual empty-out/console unwind |
| PostgreSQL shared box restart | connection errors across apps | plant-wide "slow/failing API" (see prod-server memory note) |

## Improvement opportunities & known gaps

- **Server-generate `entry_no`** (atomic sequence like `ArrivalGatepassSequence`) to remove the
  client-side collision window.
- **`Weighment.save()` vs the serializer/calculator disagree**: the model silently sets `net=0`
  when a weight is missing while `weighment/services/calculator.py` raises on `gross < tare`.
  The calculator appears unused by the API path — dead or drifting code.
- **Gate can't warn about the item-group-105 gross-weight rule**, so operators discover it only at
  GRPO. A gate-side pre-check mirroring the SAP SP would move the failure earlier.
- **`SECURITY_CHECK_DONE` status is defined but unused** in the RM path — either wire it in or drop it.
- **Two stuck-inside-truck gaps remain open** (arrival auto-close): a partial trip with a never-loaded
  `BOOKED` phantom bill, and an abandoned `PRINT_COMMITTED` docking, both leave a gate-in un-retired so
  the arrival never departs. Neither can be blindly auto-closed (staggered dispatch is legitimate); the
  durable fix is operational (planner cancels/dispatches dead bookings) plus an "abandoned docking" nudge.
- **Known stale docs:** `weighment/README.md` claims weighment is mandatory for RM completion (it
  is not); this `gate_core/docs/api.md` shows only the four read-only full-view endpoints and an
  older `GE-2026-001` `entry_no` format.

## Permissions & roles

All inbound write endpoints require `IsAuthenticated` + `HasCompanyContext` (valid `Company-Code`)
plus a Django permission:

| Action | Endpoint | Permission codename |
|--------|----------|---------------------|
| Create/list vehicle entry | `POST/GET /vehicle-management/vehicle-entries/` | `IsAuthenticated` + `HasCompanyContext` |
| Security check create/submit | `security-checks/...` | `IsAuthenticated` + `HasCompanyContext` |
| Weighment upsert/view | `weighment/...` | `IsAuthenticated` + `HasCompanyContext` |
| Receive / edit / replace PO | `raw-material-gatein/.../po-receipts...` | `raw_material_gatein.can_receive_po` |
| View PO receipts | `.../po-receipts/view/` | `raw_material_gatein.view_poreceipt` |
| Delete in-progress entry | `DELETE .../gate-entries/{id}/` | `raw_material_gatein.delete_poreceipt` |
| Complete entry | `.../complete/` | `raw_material_gatein.can_complete_raw_material_entry` |
| Read full view (RM) | `gate-core/raw-material-gate-entry/{id}/` | `gate_core.can_view_raw_material_full_entry` |
| Empty-vehicle-in list/create | `gate-core/empty-vehicle-ins/` | `IsAuthenticated` + `HasCompanyContext` |
| Arrival list/depart/empty-out/gatepass/dispatch | `gate-core/arrivals/...` | `IsAuthenticated` + `HasCompanyContext` (Inside-Vehicle console adds `dispatch_plans.can_*_inside_vehicle`) |

Standard CRUD uses Django defaults (`add_poreceipt`, `view_poreceipt`, `change_poreceipt`,
`delete_poreceipt`). Custom perms are declared in `POReceipt.Meta.permissions`
(`can_complete_raw_material_entry`, `can_receive_po`) and `gate_core` migrations
(`can_view_*_full_entry`). The frontend sidebar gates on these same codenames — see the memory note
that changing a Django **group's** perms can hide/show whole modules.

## Developer file map

**Backend (this repo):**
- `driver_management/models/vehicle_entry.py`, `models/driver.py`, `views.py`, `serializers.py`, `urls.py`
- `vehicle_management/views.py` (`VehicleEntryListCreateAPI`, `VehicleEntryDetailAPI`, `VehicleEntryListByStatus`, `VehicleEntryCountAPI`), `serializers.py`, `urls.py`
- `security_checks/models/security_check.py`, `views.py`, `serializers.py`, `urls.py`
- `weighment/models/weighment.py`, `views.py`, `serializers.py`, `services/calculator.py`, `urls.py`
- `raw_material_gatein/models/{po_receipt,po_item_receipt,po_replacement_log}.py`, `views.py`, `services/{validations,gate_completion}.py`, `signals.py`, `notifications.py`, `permissions.py`, `urls.py`
- `gate_core/models/{base,gate_entry,vehicle_arrival,gate_attachments}.py`, `enums.py`, `services/{status_guard,lock_manager}.py`, `permissions.py`, `views.py` (read-only `*GateEntryFullView`), `urls.py`
- Inbound-vehicle side: `gate_core/models/{empty_vehicle_gate_in,empty_vehicle_gate_out,vehicle_arrival}.py`, `services/empty_vehicle_dispatch.py` (covers/retire/auto-depart), `views.py` (`EmptyVehicleGateInListCreateView`, `InsideDispatchVehiclesView` + add/remove/move/unlink), `views_arrival.py`, `serializers_arrival.py`
- Downstream: `grpo/services.py`, `quality_control/` (arrival slip + inspection); dispatch tail in `views_sales_dispatch.py` + `gate_core/docs/sales_dispatch.md`

**Frontend (paired repo):** `FactoryFlow/src/modules/gate/` — see the frontend doc below.

## Related docs

- **Frontend counterpart:** [`FactoryFlow/docs/modules/gate.md`](../../../FactoryFlow/docs/modules/gate.md)
- `gate_core/docs/sales_dispatch.md` — the outbound loading tail (docking, box scans, gatepass, mark-dispatched) that consumes the arrival's covers
- `gate_core/docs/api.md` — the four read-only full-view endpoints (older, partial)
- `weighment/README.md` — weighment model/API (⚠ stale on the "mandatory for RM" claim)
- `quality_control/README.md`, `quality_control/flow.md` — arrival slip & inspection lifecycle
- `grpo/docs/error_codes.md` — GRPO/SAP error catalogue (the downstream failure surface)
- `vehicle_management/README.md` — vehicle/transporter masters
