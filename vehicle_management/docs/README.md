# Vehicle & Transporter Management — Backend

Django apps: **`vehicle_management`** and **`driver_management`**
API bases: `/api/v1/vehicle-management/…` and `/api/v1/driver-management/…`

> Frontend companion doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/vehicle-management.md`

---

## Overview — what it does & who uses it

This is the **master-data + gate-root** layer for everything that moves on wheels through
the factory. It owns three global masters (Vehicle, Transporter, Driver), the vehicle-type
lookup, and the vehicle **gate-entry** record that every gate/dispatch flow attaches to.

Two responsibilities:

1. **Masters** — the canonical list of vehicles, the transporters that own them, vehicle
   types, and drivers (with license/ID-proof/photo). These are looked up by *every* inbound
   and outbound gate flow (raw-material gate-in, empty-vehicle, dispatch/docking, job-work, BST).
2. **`VehicleEntry` (the gate root)** — a single "a truck came to the gate for reason X"
   record. Downstream apps (`raw_material_gatein`, `gate_core`, `quality_control`,
   `dispatch_plans`, `weighment`) hang their own records off it via FK/OneToOne.

Who uses it: **security/gate operators** register entries; **planning/dispatch** links vehicles
to dispatch bills and corrects loads on trucks already inside; **admins** maintain masters
(mostly via Django admin); **QC** consumes the raw-material entries. Most reads are indirect —
this app is a dependency of the gate more than a destination screen.

The masters "feed the gate + dispatch; dispatch-vehicle linking is the integration point." That
linking, and the correction console that manages it, live in **`dispatch_plans`** and
**`gate_core`** (documented under *Integrations* below) — this app supplies the vehicle/driver
identities they reference and the `VehicleEntry` they anchor to.

---

## Key concepts & entities

| Entity | App / file | Notes |
|---|---|---|
| **`Transporter`** | `vehicle_management/models/transporter.py` | Logistics company. `name` **unique**, plus `contact_person`, `mobile_no`, `gstin`. **Not company-scoped** (global). |
| **`Vehicle`** | `vehicle_management/models/vehicle.py` | `vehicle_number` **unique**; FK `vehicle_type` (SET_NULL), FK `transporter` (SET_NULL), `capacity_ton`. Global. |
| **`VehicleType`** | `vehicle_management/models/vehicle.py` | Just `name` (unique). Read-only over the API — created via Django admin. |
| **`Driver`** | `driver_management/models/driver.py` | `name`, `mobile_no`, `license_no` (**unique**), `id_proof_type/number`, optional `photo` (ImageField → `drivers/photos/`). Global. |
| **`VehicleEntry`** | `driver_management/models/vehicle_entry.py` | The **gate root**. Extends `GateEntryBase`. FK `company` (PROTECT), `vehicle` (PROTECT), `driver` (PROTECT). `entry_no` unique, `status`, `is_locked`, `entry_type`, `entry_time`, `remarks`. **Company-scoped.** |

Base classes (`gate_core/models/`):

- **`BaseModel`** (`base.py`) — `created_at`, `updated_at`, `created_by`, `updated_by`,
  `is_active`. Every master inherits it. There is **no `company` field on the masters** — they
  are shared across all companies in this database.
- **`GateEntryBase`** (`gate_entry.py`) — adds `entry_no` (unique), `status`
  (`GateEntryStatus`, default `DRAFT`), `is_locked`. Its `save()` **hard-refuses any edit once
  `is_locked=True`** (`raise ValueError("Gate entry is locked and cannot be modified")`).

**`entry_type`** (`VehicleEntry.ENTRY_TYPE_CHOICES`) — the reason a truck is at the gate:
`RAW_MATERIAL`, `DAILY_NEED`, `MAINTENANCE`, `CONSTRUCTION`, `FIXED_ASSET`, `EMPTY_VEHICLE`,
`BST_IN`, `BST_RETURN`, `JOB_WORK`, `SALES_DISPATCH`. Dispatch trucks are booked in as
`EMPTY_VEHICLE` (they arrive empty to load an outbound sales dispatch).

**`GateEntryStatus`** (`gate_core/enums.py`) — the full lifecycle spans gate → QC → done:
`DRAFT`, `SECURITY_CHECK_DONE`, `ARRIVAL_SLIP_SUBMITTED/REJECTED`, `IN_PROGRESS`, `QC_PENDING`,
`QC_IN_REVIEW`, `QC_AWAITING_QAM`, `QC_REJECTED`, `QC_HOLD`, `QC_COMPLETED`, `COMPLETED`,
`CANCELLED`. (The frontend Vehicle-Entries filter only exposes a subset: Draft / In Progress /
Completed / Cancelled.)

### Integration-point entities (owned by `gate_core` / `dispatch_plans`, not this app)

These are the records the dispatch-vehicle linking flow threads through. They reference this
app's masters and `VehicleEntry`, so a maintainer of this module must understand them:

| Entity | File | Role |
|---|---|---|
| **`EmptyVehicleGateIn`** | `gate_core/models/empty_vehicle_gate_in.py` | The dispatch truck's gate-in. OneToOne → `VehicleEntry`; FK `vehicle`/`driver`; `reason` (`DISPATCH`/`REPAIR_MOVEMENT`/`JOB_WORK`/`OTHER` — **BST removed**); `retired_at` + `retired_reason` (`DISPATCHED`/`EMPTY_OUT`/`CANCELLED`); nullable FK `arrival`. |
| **`EmptyVehicleGateInCover`** | same file | One dispatch **bill** a gate-in is gated in to carry. Keyed on the stable `sap_doc_entry`; nullable FK `dispatch_plan`; `consumed_at` set when the bill's docking dispatches. Unique `(gate_in, sap_doc_entry)`. **The source of truth for "what this truck carries."** |
| **`VehicleArrival`** | `gate_core/models/vehicle_arrival.py` | One physical truck **trip**. NOT company-scoped: threads per-company gate-ins + dockings for one truck. `status` INSIDE/LOADING/DEPARTED/CANCELLED; one `tare_weight`; one combined `ARV/…` gatepass; one exit. |
| **`DispatchPlan`** | `dispatch_plans/models.py` | A SAP invoice booked for dispatch. FK `vehicle` → `Vehicle`; FK `linked_vehicle_entry` → `VehicleEntry`; `booking_status` PENDING/BOOKED/DISPATCHED/CANCELLED. Unique `(company, sap_invoice_doc_entry)`. |

---

## End-to-end flows (as the server sees them)

### 1. Maintain a master (Vehicle / Transporter / Driver)
1. `GET` list (`…/vehicles/`, `…/transporters/`, `driver-management/drivers/`) — returns all
   rows (no company filter), ordered by name/number.
2. `POST` creates; `serializer.save(created_by=request.user)`. Vehicle/Transporter are posted
   as **`application/x-www-form-urlencoded`** by the frontend; Driver can carry a photo
   (multipart).
3. `PUT …/<id>/` updates. `Vehicle`/`Driver` use `partial=True`; **`Transporter` PUT is a full
   update** (all fields required) — an inconsistency worth knowing.
4. Lightweight `…/names/` endpoints exist for dropdowns (id + name only).

Permission at the API layer is **`IsAuthenticated` only** for all masters — see *Critical rules*.

### 2. Register a vehicle gate entry (generic gate root)
1. `POST /vehicle-management/vehicle-entries/` with `HasCompanyContext`. The view injects
   `company = request.company.company` (never trusted from the body) and saves with `created_by`.
2. Response returns `{id, entry_no, status}`.
3. In practice most `VehicleEntry` rows are **not** created here — the specialised gate flows
   (`raw_material_gatein`, `gate_core` empty-vehicle/dispatch, job-work, BST) create their own
   `VehicleEntry` with a generated `entry_no` and then attach their detail record.

### 3. Read gate entries (dashboards / QC rollup)
1. `GET /vehicle-management/vehicle-entries/?entry_type=&from_date=&to_date=` — **all three
   query params are required**; dates must be `YYYY-MM-DD` (400 otherwise). `to_date` is made
   inclusive by adding one day.
2. Scoped to `company=request.company.company`, ordered `-entry_time`, prefetching
   `po_receipts__items__arrival_slip__inspection`.
3. `VehicleEntrySerializer.to_representation` expands nested `vehicle`/`driver`/`company`,
   lists `suppliers` from PO receipts, and — **for `RAW_MATERIAL` entries only** — computes a
   rolled-up **`qc_final_status`** (Approved / Rejected / On-Hold / partial counts) from each
   item's arrival-slip inspection (`_get_qc_final_status`). Returns `None` for non-raw-material
   entries and when there are no inspected items.
4. Sibling read endpoints: `…/vehicle-entries/count/` (status histogram) and
   `…/vehicle-entries/list-by-status/` (adds a required `status` param).

### 4. Dispatch-vehicle linking (the integration point — lives in `dispatch_plans`)
Triggered from the frontend "Dispatch Vehicle Linking" board; the write path is
`DispatchPlanWriteService.update_plan` in `dispatch_plans/services.py`, exposed at
`PUT /api/v1/dispatch-plans/bills/<sap_invoice_doc_entry>/plan/`:
1. Planner picks a **Vehicle** (from this app's master) for a dispatch **bill**; the payload
   carries `vehicle_id`, transporter/driver snapshot fields, and `booking_status='BOOKED'`.
2. `_update_single_plan` runs guards **before** persisting, in order:
   `_assert_link_not_locked` → `_assert_bill_not_added_to_inside_vehicle` → `_validate_links`
   → `_apply_master_data`, then `get_or_create` + field assignment.
3. **Batch link** (`update_linked_plans`): when `linked_invoice_doc_entries` names >1 invoice,
   one vehicle is linked to all of them in a single call. All must belong to the **same SAP
   branch** (else `ValueError`); freight is allocated across them (`_allocate_batch_freight`).
4. **Unlink**: the frontend re-sends the plan with `vehicle_id/transporter_id/driver_id/
   linked_vehicle_entry_id = null` and `booking_status='PENDING'`; the backend wipes the
   snapshot fields once the ids are explicitly nulled.
5. At **empty-vehicle gate-in completion**, `record_dispatch_covers` snapshots the vehicle's
   BOOKED + `linked_vehicle_entry IS NULL` bills as **covers** and points each plan's
   `linked_vehicle_entry` at the gate-in's `VehicleEntry`.

### 5. Truck goes inside → carries bills → leaves (`gate_core`)
1. **Empty-vehicle gate-in** creates a `VehicleEntry(entry_type=EMPTY_VEHICLE, status=COMPLETED)`
   + `EmptyVehicleGateIn(reason=DISPATCH)` + `EmptyVehicleGateInCover` rows for each booked bill,
   all threaded under one **`VehicleArrival`** (the cross-company physical trip).
2. `replicate_dispatch_gate_in_across_companies` mirrors the gate-in onto every *other* company
   that booked bills for the same physical truck (one truck, one trip, many companies).
3. Docking dispatch consumes covers (`consume_covers_for_dispatched_plans` → each dispatched
   bill's cover gets `consumed_at`); when **every** cover of a gate-in is consumed it **retires**
   (`_retire_if_fully_consumed`), and when **every** company chain on the arrival is retired the
   arrival **auto-departs** (`_depart_arrival_if_complete`). Un-dispatch reverses both.

### 6. Inside-Vehicle correction console (`gate_core`)
When a truck is already inside, its load is managed only here, never from the linking board:
- **Add Bill** (`InsideVehicleAddBillView`) / **Add other bill** (`InsideVehicleAddBillToTruckView`)
  → `attach_bill_to_inside_vehicle` / `_attach_bill_via_arrival`: adds a cover to the live
  gate-in (or creates a sibling per-company gate-in under the same arrival), **unless** the load
  is already photo-locked at docking.
- **Remove** (`InsideVehicleRemoveBillView`), **Unlink All** (`InsideVehicleUnlinkAllView`),
  **Move** (`InsideVehicleMoveBillView`) → `detach_bill_from_gate_in`: deletes the cover +
  clears the plan link (kept BOOKED). Refuses a bill whose load is committed (`bill_commit_reason`).
  ⚠️ **Does not retire/depart** — see edge cases #6 and the failure table.

---

## Critical business rules & invariants

- **Masters are global, not company-scoped.** `Vehicle.vehicle_number`, `Transporter.name`,
  `Driver.license_no` are **globally unique** — two companies share one vehicle row and cannot
  each own the same plate/license. Reads intentionally have no company filter.
- **`VehicleEntry` is company-scoped** and always created with `company = request.company.company`
  (set server-side, never trusted from the client body).
- **Locked gate entries are immutable.** `GateEntryBase.save()` raises if `is_locked` was already
  set. Downstream flows lock an entry once its documents are committed.
- **PROTECT everywhere it matters.** `VehicleEntry.company/vehicle/driver`,
  `EmptyVehicleGateIn.vehicle_entry/vehicle/driver`, `VehicleArrival.vehicle/driver` are all
  `on_delete=PROTECT` — a master that has ever been used **cannot be deleted** (deactivate via
  `is_active` instead).
- **A vehicle already inside is frozen on the linking board.** Booking a *new* bill onto an
  inside truck is rejected by `_assert_bill_not_added_to_inside_vehicle`; the bill must be added
  via the Inside-Vehicle console, or the truck emptied-out first. Re-pointing an
  already-linked-and-completed bill is rejected by `_assert_link_not_locked`.
- **Covers are the source of truth for "what this truck carries."** Matching/docking eligibility
  read `EmptyVehicleGateInCover`, keyed on the stable `sap_doc_entry`, so a reused truck's new
  bills can never ride on an old/foreign gate-in.
- **Retirement + auto-depart are automatic and reversible on dispatch.** A gate-in retires only
  when *all* its covers are consumed; an arrival departs only when *all* its company chains are
  retired; un-dispatch un-retires and re-opens. **Console detach does NOT trigger this** (gap).
- **`_retire_if_fully_consumed` needs ≥1 cover.** A gate-in with **zero** active covers is
  vacuously "not fully consumed" and is left un-retired — so emptying a gate-in via the console
  does not free the truck.
- **`qc_final_status` is RAW_MATERIAL-only.**
- **SAP is not called at link time.** Linking a vehicle only writes `DispatchPlan`; SAP posting
  happens later (docking goods-issue / GRPO). A SAP outage does **not** block vehicle linking.
- **One truck, one trip, one exit.** A multi-company truck is one `VehicleArrival`; its real
  in/out state is the arrival status, not any single docking's status.

---

## API surface

`vehicle_management` (`/api/v1/vehicle-management/…`) and `driver_management`
(`/api/v1/driver-management/…`) — all master endpoints are **`IsAuthenticated` only**; the
`vehicle-entries/*` group additionally requires **`HasCompanyContext`**.

| Method | Path | View | Notes |
|---|---|---|---|
| GET/POST | `transporters/` | `TransporterListCreateAPI` | list all / create |
| GET | `transporters/names/` | `TransporterNameListAPI` | id + name |
| GET/PUT | `transporters/<id>/` | `TransporterDetailAPI` | **PUT is full update** |
| GET/POST | `vehicles/` | `VehicleListCreateAPI` | list all / create |
| GET | `vehicles/names/` | `VehicleNameListAPI` | id + number |
| GET/PUT | `vehicles/<id>/` | `VehicleDetailAPI` | PUT partial |
| GET | `vehicle-types/` | `VehicleTypeListAPI` | read-only (admin creates) |
| GET/POST | `vehicle-entries/` | `VehicleEntryListCreateAPI` | GET needs `entry_type,from_date,to_date` |
| GET | `vehicle-entries/<id>/` | `VehicleEntryDetailAPI` | company-scoped |
| GET | `vehicle-entries/count/` | `VehicleEntryCountAPI` | status histogram |
| GET | `vehicle-entries/list-by-status/` | `VehicleEntryListByStatus` | adds required `status` |
| GET/PUT | `driver-management/drivers/`, `drivers/<id>/`, `drivers/names/` | driver APIs | list/create/detail/names |

Integration endpoints owned by other apps but central to this module:

| Method | Path | View | Perm |
|---|---|---|---|
| PUT | `/api/v1/dispatch-plans/bills/<doc_entry>/plan/` | `dispatch_plans` update-plan | `can_link_dispatch_vehicle` |
| GET | `/api/v1/dispatch-plans/bills/` (+ `by-number/<n>/`) | `DispatchBillListAPI` | plan reads (SAP-backed) |
| GET | `/api/v1/gate-core/inside-dispatch-vehicles/` | `InsideDispatchVehiclesView` | `can_view_inside_vehicle_manager` |
| POST | `…/inside-dispatch-vehicles/add-bill/` | `InsideVehicleAddBillView` | `can_add_bill_inside_vehicle` |
| POST | `…/inside-dispatch-vehicles/add-bill-to-truck/` | `InsideVehicleAddBillToTruckView` | `can_add_bill_inside_vehicle` |
| POST | `…/inside-dispatch-vehicles/remove-bill/` | `InsideVehicleRemoveBillView` | `can_remove_bill_inside_vehicle` |
| POST | `…/inside-dispatch-vehicles/move-bill/` | `InsideVehicleMoveBillView` | `can_move_bill_inside_vehicle` |
| POST | `…/inside-dispatch-vehicles/unlink-all/` | `InsideVehicleUnlinkAllView` | `can_unlink_bills_inside_vehicle` |

`InsideDispatchVehiclesView` is **cross-company** (`user_company_ids(request)`) and returns, per
live dispatch gate-in: `arrival`/`arrival_no`, `company_code`/`company_name`, `vehicle_*`,
`driver_*`, and a `bills[]` array where each bill carries `removable`, `not_removable_reason`
(from `bill_commit_reason`), and `duplicate_on[]` (other gate-ins covering the same
`sap_doc_entry`).

---

## Integrations & cross-module boundaries

| Boundary | Direction | Mechanism |
|---|---|---|
| **`gate_core`** | downstream | `EmptyVehicleGateIn.vehicle_entry` = OneToOne → `VehicleEntry`; `VehicleArrival.vehicle/driver` → this app's masters; empty-vehicle/dispatch/BST/job-work all create a `VehicleEntry`. |
| **`dispatch_plans`** | downstream / integration point | `DispatchPlan.vehicle` → `Vehicle`; `DispatchPlan.linked_vehicle_entry` → `VehicleEntry`; `EmptyVehicleGateInCover.dispatch_plan` → `DispatchPlan`. The `can_link_dispatch_vehicle` + `can_*_inside_vehicle` permissions live in `dispatch_plans`. |
| **`raw_material_gatein`** | downstream | `POReceipt.vehicle_entry` → `VehicleEntry`; drives the `qc_final_status` rollup. |
| **`quality_control`** | downstream | inspections read via `po_receipts__items__arrival_slip__inspection`. |
| **`weighment`** | sibling | `Weighment.vehicle_entry` → `VehicleEntry`; tare copied onto the arrival. |
| **`company`** | upstream | `HasCompanyContext` supplies `request.company.company`; `user_company_ids()` powers cross-company reads. |
| **SAP B1** | via `dispatch_plans`/`gate_core` | vehicle/transporter/bilty hints come **from** SAP into the plan; this app posts nothing to SAP directly. |

**Cross-company boundary (important):** reads that must see every company (the Inside-Vehicle
console) use `user_company_ids(request)`; per-company reads (`VehicleEntry` lists) use
`request.company.company`. Writes always resolve the company **from the record**, never from the
active tab — the root cause of "blank in sibling company" bugs elsewhere.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **SAP names a vehicle not in Vehicle Master.**
   → Linking sheet cannot resolve a `vehicle_id`; save is refused server-side unless a master
   vehicle is chosen/created. → Operator sees an amber "SAP shows vehicle X, not linked to Master"
   banner and a red form error on save. → Risk: plate typos create near-duplicate masters.

2. **Book a fresh bill onto a truck already inside.**
   → `_assert_bill_not_added_to_inside_vehicle` raises before save. → Toast: *"HRxx… is already
   inside (gate-in EVGI-…). Add bills to it from the 'Add Bills to Inside Vehicle' page, or do an
   empty-vehicle-out first."* → Correct by design; risk is user confusion if they don't know the
   console exists.

3. **Re-point an already-linked bill after empty-in completed.**
   → `_assert_link_not_locked` raises "Vehicle linking is locked…" when any of the guarded
   fields differ from the stored plan. → Link/unlink refused. → Must empty-vehicle-out to re-plan.

4. **Same bill covered by two gate-ins (duplicate cover).**
   → `InsideDispatchVehiclesView` annotates the bill with `duplicate_on = [other entry_no…]`. →
   Operator sees a red "Duplicate cover (n)" badge and removes the wrong one. → This console
   exists specifically to fix the historical triple-cover bug.

5. **Bill already loading/dispatched, someone tries to remove/move it.**
   → `bill_commit_reason` returns non-null once a box is scanned or the docking is
   photo-locked/dispatched; `detach_bill_from_gate_in` refuses (`removable=false`). → Bill shows
   "Locked — …" and the Remove/Move buttons are disabled. → Prevents pulling a bill off a truck
   that physically left.

6. **Remove / Unlink-All / Move the LAST bill off a gate-in (empties it).** ⚠️ **live gap on `main`.**
   → `detach_bill_from_gate_in` deletes the cover + unlinks the plan but does **not** call
   `_retire_if_fully_consumed` / `_depart_arrival_if_complete`; and `_retire_if_fully_consumed`
   returns early on a 0-cover gate-in. So the gate-in never retires and the arrival stays
   `LOADING`. → The truck lingers in the Inside Vehicle Manager (company panel now shows 0 bills)
   and the linking board still says "already inside — can't start"; only an explicit **Mark Out**
   (empty-vehicle-out) frees it. → Operators expect Unlink-All to release the truck; it doesn't.
   The auto-retire-on-detach fix ("C1") exists on branch `fix/dispatch-flow-audit-remediation`
   but is **not merged to `main`**.

7. **Bill booked *after* the truck is inside (late bill).**
   → Via the console's **Add Bill / Add other bill**, `attach_bill_to_inside_vehicle` /
   `_attach_bill_via_arrival` adds a cover (and, for a new company, a sibling gate-in under the
   same arrival) **if** the load is not yet photo-locked. → Bill appears on the inside truck
   without a second physical gate-in. → If the load is already photo-locked at docking, attach is
   refused (returns False → 400).

8. **Partial truck load / multi-company truck.**
   → One `VehicleArrival` threads several per-company gate-ins; auto-depart fires **only** when
   *every* company chain is retired. → Truck correctly stays "inside" until the last company
   dispatches. → Risk: a company whose docking never dispatches (unloaded phantom BOOKED bill,
   stalled PRINT_COMMITTED) leaves the arrival lingering `LOADING`.

9. **Stale arrival reuse.**
   → `_attach_bill_via_arrival` only adopts a *live* arrival (`gate_ins.retired_at__isnull=True`);
   a fully-retired arrival is skipped. → A returning truck gets a fresh arrival instead of
   resurrecting an old one. → This is the fix for "truck stuck inside forever" (commit `edbf023`).

10. **Missing weighbridge tare.**
    → `VehicleArrival.tare_weight` / `Weighment` are nullable; the gate-in completes without a
    tare. → Gate-in still succeeds. → Net weight downstream is unavailable until the truck is
    weighed.

11. **Gate entry locked, an edit is attempted.**
    → `GateEntryBase.save()` raises `ValueError`. → API 500/validation error; admin bulk actions
    that filter `is_locked=False` silently skip locked rows. → Expected guard.

12. **Un-dispatch after auto-depart.**
    → Un-consuming covers un-retires DISPATCHED-retired gate-ins and re-opens the arrival
    (`DEPARTED → LOADING`). → Truck reappears inside. → Correct, but two rollbacks in a row can
    surprise operators.

---

## Failure modes / what can break

| Failure | Server behaviour | Operator/manager notices |
|---|---|---|
| Missing/invalid `entry_type`/`from_date`/`to_date` on entry reads | `400` with a `detail` message | "Vehicle Entries" list fails to load / empty |
| No active company context | `HasCompanyContext` denies (`403`) | Entry / inside-vehicle endpoints unusable until a company is selected |
| Link a vehicle that's inside | `ValueError` → `400` with guard text | Toast telling them to use the Inside-Vehicle page |
| Delete a used master | DB `PROTECT` → `IntegrityError` | Admin delete blocked; must deactivate (`is_active=False`) |
| Duplicate `vehicle_number`/`license_no`/transporter `name` | `unique` constraint → `400` | "already exists" on create |
| Editing a locked `VehicleEntry` | `ValueError` from `save()` | Change refused |
| Batch-link invoices from different SAP branches | `ValueError` → `400` | "Selected invoices must belong to the same SAP branch." |
| Unlink-All / Remove empties a gate-in | cover deleted, **gate-in NOT retired** | Truck stays "inside"; needs a manual Mark Out (empty-out) |
| Arrival never departs (one company stuck) | Arrival stays `LOADING` | Truck shows perpetually "inside" in the console |
| `qc_final_status` heavy read | N+1 avoided via `prefetch_related`, but wide date windows are slow | Slow "Vehicle Entries" dashboard (see perf backlog) |

---

## Improvement opportunities & known gaps

- **Console detach does not retire/depart (the biggest gap).** Landing the branch "C1" fix on
  `main` would make Remove/Unlink-All/Move auto-retire an emptied gate-in and auto-depart a
  complete arrival, so the console can actually free a truck without a separate Mark Out. Until
  then, treat Mark Out (empty-vehicle-out) as the only way to retire a gate-in from the console.
- **Master write endpoints only check `IsAuthenticated`.** `vehicle_management.change_vehicle`,
  `change_transporter`, `driver_management.change_driver` are enforced **only in the frontend**
  (button/nav gating). Any authenticated user can `POST`/`PUT` a vehicle/transporter/driver via
  the API. Consider `DjangoModelPermissions`.
- **No API to create a `VehicleType`** (admin-only) though `Vehicle` references it.
- **`Transporter` PUT is non-partial** while `Vehicle`/`Driver` are partial — inconsistent.
- **`VehicleEntrySerializer.entry_no` is writable** on the generic gate-root POST (not read-only),
  so a caller can pass an arbitrary `entry_no`; specialised flows generate it instead.
- **No soft-delete/merge tooling** for the near-duplicate masters that SAP plate typos create.
- **Entry-list date windows are unbounded**; wide ranges + the QC rollup can be slow.
- **Phantom BOOKED bills** (booked, never docked/dispatched/cancelled) re-snapshot as covers at
  every gate-in and keep partial trips from closing — an operational (planner) fix, not a code one.

---

## Permissions & roles

Backend-enforced:

| Endpoint group | Permission classes |
|---|---|
| Masters (Vehicle/Transporter/Driver/VehicleType — list/create/detail/names) | `IsAuthenticated` **only** |
| `VehicleEntry` list/create/detail/count/by-status | `IsAuthenticated`, `HasCompanyContext` |
| Inside-vehicle console (`gate_core`) | `IsAuthenticated`, `HasCompanyContext`, + per-action `Can*InsideVehicle` (view/add/remove/move/unlink) |

Permission **codenames** the frontend gates on (owned by other apps' migrations):
`vehicle_management.view_vehicle`, `.change_vehicle`, `.change_transporter`,
`driver_management.change_driver`, and the dispatch-owned `dispatch_plans.can_link_dispatch_vehicle`
+ `dispatch_plans.can_{view,add_bill,remove_bill,move_bill,unlink_bills,mark_out}_inside_vehicle`.
Because backend master writes don't check these, treat the master codenames as **UI gating**, not
security boundaries. The inside-vehicle codenames **are** enforced backend-side.

---

## Developer file map

**Backend — `vehicle_management/`**
- `models/vehicle.py` — `Vehicle`, `VehicleType`
- `models/transporter.py` — `Transporter`
- `serializers.py` — `Vehicle/Transporter/VehicleType` serializers + `VehicleEntrySerializer` (incl. `_get_qc_final_status`)
- `views.py` — all master + `VehicleEntry` APIViews
- `urls.py` — route table (mounted at `/api/v1/vehicle-management/`)
- `admin.py` — rich Django admin (badges, inlines, bulk activate/deactivate)
- `tests.py` — `VehicleEntryDashboardQCTests` (QC rollup)
- `migrations/0003_delete_vehicleentry.py` — note: `VehicleEntry` used to live here, now in `driver_management`

**Backend — `driver_management/`**
- `models/driver.py` — `Driver`
- `models/vehicle_entry.py` — `VehicleEntry` (the gate root)
- `serializers.py`, `views.py`, `urls.py`, `admin.py`

**Backend — boundary / integration code**
- `gate_core/models/base.py`, `models/gate_entry.py` — `BaseModel`, `GateEntryBase`
- `gate_core/models/vehicle_arrival.py` — `VehicleArrival`, `ArrivalGatepassSequence`
- `gate_core/models/empty_vehicle_gate_in.py` — `EmptyVehicleGateIn`, `…Item`, `…Cover`
- `gate_core/services/empty_vehicle_dispatch.py` — covers, retirement, auto-depart, attach/detach, `bill_commit_reason`, `replicate_dispatch_gate_in_across_companies`
- `gate_core/views.py` — `InsideDispatchVehiclesView` + `InsideVehicleAddBill/AddBillToTruck/Remove/Move/UnlinkAll` views (L3009+)
- `gate_core/urls.py` — `inside-dispatch-vehicles/*` routes
- `gate_core/enums.py` — `GateEntryStatus`
- `dispatch_plans/services.py` — linking write path (`update_plan`, `update_linked_plans`) + guards (`_assert_link_not_locked`, `_assert_bill_not_added_to_inside_vehicle`, `_link_completed_empty_in`)

---

## Related docs

- **Frontend companion:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/vehicle-management.md`
- `C:/Users/gurpa/dev/FactoryFlow/docs/gate.md` — gate flows that create `VehicleEntry`
- `C:/Users/gurpa/dev/FactoryFlow/docs/dispatch.md` + `docs/sales-dispatch-docking.md` — dispatch / docking board
- `C:/Users/gurpa/dev/FactoryFlow/docs/sap-plan-dashboard.md` — dispatch plans / SAP hints
- Auto-memory notes (context, not in-repo): *VehicleArrival lifecycle*, *Late-booked bills → inside
  truck*, *Stuck-inside truck root causes*, *Cross-company flow boundary*.
