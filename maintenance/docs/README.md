# Maintenance & Safety (CMMS / EHS) — Backend

> Grounded in the code as of this writing. Existing narrative docs (e.g.
> `maintenance/docs/USER_GUIDE.md`) are older and partly aspirational — trust
> this file and the source. Frontend companion doc:
> `C:/Users/gurpa/dev/FactoryFlow/docs/modules/maintenance.md`.

## Overview — what it does & who uses it

This module is the plant's combined **CMMS** (asset care, work orders, preventive
maintenance, spares, vendor visits) and **EHS** (fire store, fire-equipment shift
inspections, fire-gear issue register, **work permits / permit-to-work**, and
**safety fines**), plus **returnable / non-returnable gate passes** for material
that leaves the gate.

It spans **three Django apps**, all mounted under `/api/v1/`:

| App | URL prefix | Role |
|---|---|---|
| `maintenance` | `/api/v1/maintenance/` | The bulk of the module: assets, work orders, PM, spares, fire store/reports/issues, work permits, safety fines, vendor visits, dashboard/reports/scan, gate-link read models. |
| `maintenance_gatein` | `/api/v1/maintenance-gatein/` | The gate desk's "Maintenance & Repair Material" inward entry (a `VehicleEntry` sub-type) and the **receive-spare-into-store** action that feeds `maintenance` stock. |
| `returnable_items` | `/api/v1/returnable-items/` | Returnable / non-returnable gate pass (RGP/NRGP): send material out for repair/exchange and track it until every item is back. Owned by a department, but two of its stages happen at the gate. |

Primary users: maintenance technicians & head, store keepers, the **Fire
Department Head** (approves work permits, raises safety fines, reviews fire
reports), gate operators, and department clerks who raise returnable passes.

Everything is **company-scoped**: every viewset resolves the active company from
`request.company.company` (`company.permissions.HasCompanyContext`) and filters by
it. There is no cross-company read here — one active company at a time.

## Key concepts & entities

All models inherit `gate_core.models.BaseModel` (`is_active`, `created_by`,
`updated_by`, `created_at`, `updated_at`). Human-readable numbers are generated
per company per day by `next_*` classmethods (scan-for-last-row; the returnable
sequence is the exception — see below).

**Asset & masters** (`maintenance/models.py`)
- `Asset` — machine/utility register. FK to `AssetCategory`, `AssetLocation`,
  the global `accounts.Department` (note: **not** the app-local `AssetDepartment`,
  which is now a stand-alone master), optional `parent_asset`,
  `production_execution.Machine`. Status enum `AssetStatus`
  (RUNNING/IDLE/BREAKDOWN/UNDER_PM/UNDER_REPAIR/RETIRED), `qr_code`, warranty/AMC
  dates. `deactivate()` retires + soft-deletes.
- `AssetPhoto`, `AssetDocument` — monthly/before-after photos and papers.

**Work orders**
- `MaintenanceWorkOrder` — `work_order_no` `MWO-YYYYMMDD-NNNN`. `WorkType`
  (COMPLAINT/BREAKDOWN/GENERAL/PREVENTIVE/INSPECTION/CALIBRATION/AMC_VENDOR/PROJECT),
  `WorkOrderStatus` (DRAFT→OPEN→ASSIGNED→IN_PROGRESS→WAITING_SPARE/WAITING_VENDOR/
  ON_HOLD→COMPLETED→APPROVED→CLOSED), priority, impact, RCA/CAPA text, timestamps.
  Optional links to `production_execution.ProductionRun` and a one-to-one
  `MachineBreakdown`. Derived `response_time_minutes`, `repair_time_minutes`,
  `downtime_minutes`.
- `MaintenanceWorkOrderPhoto` — BEFORE/AFTER/GENERAL.

**Preventive maintenance**
- `PreventiveMaintenancePlan` (`PM-YYYYMMDD-NNNN`) — asset + `PMFrequency`
  (DAILY…YEARLY), `next_due_date`, `advance_days`, `auto_create_work_order`,
  `checklist_required`.
- `MaintenanceChecklistTemplateItem` — template rows (CHECKBOX/PASS_FAIL/NUMBER/TEXT,
  `is_required`, `safety_critical`, min/max).
- `PreventiveMaintenanceExecution` — one due occurrence (`PMExecutionStatus`
  PENDING/IN_PROGRESS/COMPLETED/SKIPPED; OVERDUE is **derived**, not stored),
  optional one-to-one work order.
- `MaintenanceChecklistResult` — filled values, snapshotting the task text.

**Spares store**
- `MaintenanceSpare` (`part_number` unique per company, optional `sap_item_code`,
  `is_critical`, `current_stock`, `reorder_level`, `minimum_stock`,
  `compatible_assets`). `is_low_stock`/`is_below_minimum` are computed.
- `SpareRequest` — raised against a work order; running `issued/consumed/returned`
  quantities and a `refresh_status()` that derives REQUESTED→PARTIALLY_ISSUED→
  ISSUED→PARTIALLY_CONSUMED→CLOSED.
- `SpareMovement` — the stock ledger (RECEIPT/ISSUE/CONSUME/RETURN/ADJUSTMENT).

**Fire store & EHS** (all in `maintenance`)
- `FireCategory` / `MaintenanceFire` / `FireRequest` / `FireMovement` — a
  **standalone copy** of the spare store, backed by its own tables, for the fire
  department's stock.
- `FireShiftReport` + `FireShiftReportItem` (+ `Photo`, `Attachment`) — day/night
  fire-equipment inspection rounds (`FireEquipmentType`, `FireEquipmentStatus`),
  SUBMITTED→REVIEWED.
- `FireEquipmentIssue` + `FireEquipmentIssueItem` — issue/return **register** for
  fire gear loaned to a person (ISSUED/PARTIALLY_RETURNED/RETURNED; `FireReturnCondition`
  OK/DAMAGED/LOST). Draws on `MaintenanceFire` stock.
- `WorkPermit` (+ `WorkPermitWorker`, `WorkPermitAttachment`, `WorkPermitApproval`)
  — permit-to-work (`D-YYYYMMDD-NNNN`). `WorkPermitType` (HOT_WORK, HEIGHT,
  CONFINED_SPACE…), multi-day validity (`valid_date`..`valid_to`, `time_start`/
  `time_end`), isolations, PPE & precautions (JSON lists), and a sign-off
  lifecycle DRAFT→SUBMITTED→APPROVED→IN_PROGRESS→COMPLETED→CLOSED (+ CANCELLED,
  EXPIRED).
- `SafetyViolationType` + `SafetyFine` (+ `SafetyFinePhoto`) — PPE violations with
  a fine amount; PENDING→PAID/WAIVED (`SF-YYYYMMDD-NNNN`).

**Vendor & gate bridge**
- `MaintenanceVendorVisit` — AMC/vendor visit tied to a work order + asset, can
  link a `person_gatein.EntryLog` and a `MaintenanceGateEntry`.
- `MaintenanceGateLink` (one-to-one with `MaintenanceGateEntry`) + `MaintenanceSpareReceipt`
  — the bridge that carries an asset/work-order/spare link, QC flag, GRPO
  reference, and receipt status from a gate entry into the store.

**Gate-in** (`maintenance_gatein/models.py`)
- `MaintenanceGateEntry` — one-to-one with `driver_management.VehicleEntry`
  (`entry_type == "MAINTENANCE"`). Auto `work_order_number` `WO-YYYY-NNN` (note:
  **global**, not per-company). `MaintenanceType` is a global lookup.

**Returnable** (`returnable_items/models.py`)
- `ReturnableGatePass` (header) → `ReturnableGatePassItem` (lines) →
  `ReturnableReturnEvent` (one physical return trip) → `ReturnableReturnEventItem`
  (how much of a line came back on that trip). `is_returnable=False` = a
  non-returnable pass (NRGP) that closes at gate-out. `ReturnableGatePassLog` is
  an append-only timeline (no simple-history in this backend).
- `ReturnableGatePassSequence` — per-company, per-financial-year counter using
  `select_for_update()` (gate operators create passes in bursts, so the
  scan-for-last-row pattern collides — this is deliberate).

## End-to-end flows

### 1. Work order (complaint / breakdown)
1. `POST /work-orders/` (or `POST /scan/work-order/` from an asset scan) →
   status OPEN, `reported_by` stamped. `perform_create` runs `_sync_asset_status`:
   a BREAKDOWN work order flips the asset to BREAKDOWN.
2. `assign` → ASSIGNED (+ `assigned_to`, `target_date`). `start` → IN_PROGRESS
   (stamps `start_time`, asset → UNDER_REPAIR or UNDER_PM by work type).
3. `request-spare` creates a `SpareRequest` and pushes the order to WAITING_SPARE.
   `set-status` moves between the open statuses (but **refuses** COMPLETED/APPROVED/
   CLOSED — those have dedicated actions).
4. `complete` → COMPLETED (stamps end time, RCA/CAPA). `_sync_production_breakdown`
   closes the linked `MachineBreakdown`, computes `breakdown_minutes`, and rolls the
   run's total. `approve` → APPROVED (COMPLETED-only). `close` → CLOSED
   (APPROVED-only); if no other open work orders on the asset, asset → RUNNING.

### 2. Preventive maintenance
1. Define a `PreventiveMaintenancePlan` + checklist template items.
2. `pm-plans/{id}/generate` or `pm-plans/generate-due` walks `next_due_date`
   forward, creating a `PreventiveMaintenanceExecution` per due date (idempotent via
   `get_or_create`) and — if `auto_create_work_order` — a PM work order + a snapshot
   of checklist results. The plan's `next_due_date` advances even when every
   execution in the window already existed (fixes a "stuck due" bug — see the
   comment in `_generate_due_pm_for_plan`).
3. `pm-executions/{id}/start` → IN_PROGRESS (asset → UNDER_PM). `complete` saves
   checklist results and **enforces required items** when `checklist_required`;
   completing also completes the linked work order and releases the asset if no
   other open work. `skip` closes the linked work order with the skip reason.

### 3. Spares: receive → issue → consume → return
1. **Receive from gate**: `POST /api/v1/maintenance-gatein/gate-entries/{id}/maintenance/receive-spare/`
   creates a `MaintenanceSpareReceipt`, increments `MaintenanceSpare.current_stock`,
   writes a RECEIPT `SpareMovement`, and marks the `MaintenanceGateLink` RECEIVED.
   If the link is `qc_required` and QC is not ACCEPTED/WAIVED, the link is set
   BLOCKED and the receipt is refused.
2. **Issue** (`spare-requests/{id}/issue`) checks stock under `select_for_update`,
   decrements stock, writes an ISSUE movement. **Consume** and **return-unused**
   move the request through its status; return-unused puts stock back. **cancel**
   is only allowed while `issued_qty == 0`.
3. The **fire store** (`/fire`, `/fire-requests`, `/fire-movements`) is the same
   flow against `MaintenanceFire`.

### 4. Fire-equipment issue / return register
1. `POST /fire-issues/` with line items → for each line linked to a stocked
   `MaintenanceFire`, stock is decremented and an ISSUE movement written (refuses
   if stock is short).
2. `fire-issues/{id}/return` adds returned quantities; **only OK-condition returns
   are added back to stock** (DAMAGED/LOST are not). `refresh_status()` derives the
   issue status.

### 5. Work permit (permit-to-work)
1. Maintenance drafts a `WorkPermit` (`POST /work-permits/`, DRAFT).
2. `submit` → SUBMITTED and notifies every user with `can_approve_work_permit`
   (the Fire Department Head) via `NotificationService.send_notification_by_permission`.
3. `approve` (SUBMITTED-only, Fire Head) → APPROVED, writes a
   `WorkPermitApproval` row for the FIRE_DEPARTMENT_HEAD role, **sets the required
   `ppe`**, and notifies the submitter.
4. `start` (APPROVED-only) → IN_PROGRESS — **refused if validity has already
   lapsed** (`is_expired_now`). `complete` → COMPLETED (ABANDONED or VERIFIED,
   handover fields). `close` (COMPLETED-only) → CLOSED. `cancel` from any active
   status. `renew` clones an EXPIRED/CANCELLED permit into a fresh DRAFT (new
   serial, `renewed_from` set, workers copied).
5. **Expiry job** `expire_lapsed_work_permits()` (`maintenance/jobs.py`, run by the
   `expire_work_permits` command / `run_work_permit_scheduler` APScheduler loop)
   flips any SUBMITTED/APPROVED/IN_PROGRESS permit past `expires_at` to EXPIRED and
   notifies the Fire Head + submitter.

### 6. Safety fine
`POST /safety-fines/` (Fire Head only) auto-numbers and stamps `issued_by`.
`settle` (PENDING-only) → PAID or WAIVED (a waiver still records remarks).

### 7. Returnable / non-returnable gate pass
Maker-checker + two gate touchpoints. Actions live in
`returnable_items/views.py`; each validates status, stamps actor+time, writes a
`ReturnableGatePassLog` row, and notifies the other side.
1. Department creates a DRAFT (`is_returnable` fixed at creation), adds items.
   `submit` → PENDING_APPROVAL.
2. Higher authority `approve` → PENDING_GATE_OUT (**cannot approve your own
   submission**) or `reject` back to DRAFT.
3. Gate `gate-out`: fills vehicle/driver (or hand-carried), then **returnable →
   OUT**; **non-returnable → CLOSED immediately** (nothing comes back). `reject-at-gate`
   bounces a mismatched pass back to DRAFT.
4. Gate `record-return` (returnable, OUTSTANDING only) creates a
   `ReturnableReturnEvent` + lines; each `pass_item.recalculate_returned()` re-sums
   from its return lines, then `gate_pass.refresh_status()` derives OUT →
   PARTIALLY_RETURNED → RETURNED. **Partial and repeat trips fall out naturally.**
5. Department `acknowledge` (one event or all outstanding) confirms physical
   collection. `close` (RETURNED + all acknowledged). `short-close` closes a pass
   whose items will never return (reason required; unreturned qty stays on the
   lines). `cancel` only before any return event exists.
6. Jobs (`returnable_items/jobs.py`): `notify_due_returnables` (due today) and
   `flag_overdue_returnables` (sets `is_overdue`), both idempotent, guarded by
   `due_notified_at` / `overdue_notified_at`.

### 8. Maintenance gate-in
Gate creates a `VehicleEntry(entry_type=MAINTENANCE)`, then
`POST …/maintenance/` fills the `MaintenanceGateEntry` (auto `WO-YYYY-NNN`). The
serializer's write-only fields (`maintenance_asset/work_order/spare`, `qc_*`,
`grpo_*`) `get_or_create` the `MaintenanceGateLink`. `…/complete/` locks the
entry — but **only after a bill/`GateAttachment` is uploaded**. No QC or
weighbridge weighment is required for this entry type.

### 9. Vendor / AMC visit
1. `POST /vendor-visits/` ties a `MaintenanceVendorVisit` to a work order + asset
   and, unless the work order is already COMPLETED/APPROVED/CLOSED/WAITING_VENDOR,
   pushes it to **WAITING_VENDOR** (the vendor mirror of `request-spare` →
   WAITING_SPARE). Can link a `person_gatein.EntryLog` (the engineer's gate pass)
   and a `MaintenanceGateEntry` (parts they brought).
2. `start` → IN_PROGRESS (stamps `actual_start`); `complete` → COMPLETED (stamps
   `actual_end`, attach service report / invoice); `cancel` → CANCELLED (refused
   once COMPLETED). The work order is **not** auto-advanced off WAITING_VENDOR —
   maintenance moves it on with the normal work-order actions.

## Critical business rules & invariants

- **Company scoping.** Every queryset filters by `request.company.company`. Numbers
  are unique per company (except `MaintenanceGateEntry.work_order_number`, which is
  global, and `ReturnableGatePass.pass_no`, which is DB-unique).
- **Work-order status gates.** `approve` requires COMPLETED; `close` requires
  APPROVED; `set-status` refuses the three closure statuses; nothing can be modified
  once CLOSED (`_ensure_not_closed`).
- **Asset status is derived, never free-typed** through the API — `_sync_asset_status`
  / `_release_asset_if_no_open_work` own it. Manual status only via the asset PUT.
- **Stock never goes negative on issue.** Issue/receive/return lock the spare row
  (`select_for_update`) and validate against `current_stock`. `consume` and
  `return-unused` are capped at `available_to_consume_qty` (issued − consumed −
  returned); a `SpareRequest` can only be `cancel`led while `issued_qty == 0`.
- **Fire returns:** only `FireReturnCondition.OK` gear is added back to stock.
- **A vendor visit pushes its work order to WAITING_VENDOR** on creation (unless
  already completed/closed); it is never auto-cleared — maintenance advances the
  work order manually.
- **Work-permit validity** = `valid_date`..(`valid_to` or `valid_date`) with
  `time_end` (or end-of-day) on the last day. `start` is refused after lapse; the
  expiry job only touches SUBMITTED/APPROVED/IN_PROGRESS.
- **Safety-fine issue/settle is Fire-Head-only** — `CanManageSafetyFine` deliberately
  requires *only* the custom `can_manage_safety_fine`; the Django `add/change_safetyfine`
  model perms must **not** grant it (else a view-only group could POST fines).
- **Returnable maker-checker:** the approver may not be the submitter; a
  non-returnable pass is closed at gate-out and must never appear in the gate-in,
  overdue, or outstanding queues; `cancel` is blocked once any return event exists
  (use short-close).
- **SAP GRPO is a *reference*, not a post, here.** `MaintenanceGateLink` /
  `MaintenanceSpareReceipt` store `grpo_reference` / `grpo_doc_entry` /
  `grpo_doc_num` supplied by the caller. The actual GRPO posting lives in the
  separate `grpo` app; receiving a spare only increments local store stock.

## Integrations & cross-module boundaries

- **SAP HANA (read-only, live).** The **only** direct SAP call in the module is
  `_fetch_sap_spare_stock` in `maintenance/views.py` (`MaintenanceSpareStockAPI`
  → `sap_client.hana.HanaConnection`, reads `OITM/OITW/OWHS` on-hand/committed/
  on-order for a spare's `sap_item_code`). `returnable_items` reads `OITM` for the
  item picker via `ReturnableSapItemSearchView` →
  `production_execution.services.sap_reader.ProductionOrderReader.search_items`.
  Both catch every exception and degrade gracefully (never 500).
- **Gate / driver_management.** `MaintenanceGateEntry` hangs off `VehicleEntry`;
  vendor visits reference `person_gatein.EntryLog`; returnable passes reference
  `vehicle_management.Vehicle/Transporter` and `driver_management.Driver`.
- **Production execution.** Work orders link a `ProductionRun` and a one-to-one
  `MachineBreakdown`; completing a work order closes the breakdown and rolls the
  run's downtime. Assets can link a `Machine`.
- **Notifications.** Work-permit submit/approve/expire and every returnable
  transition push notifications (`notifications.services.NotificationService`),
  several by permission codename rather than by user.
- **grpo app.** Owns real SAP GRPO posting; maintenance only stores the reference.

## Real-world edge cases

- **Bill added after gate-in.** Trigger: gate completes the maintenance entry
  before the supplier bill is in hand. Behaviour: `MaintenanceGateCompleteAPI`
  refuses to lock without a `GateAttachment`. Symptom: operator sees "Bill upload is
  required before completing this maintenance entry". Gap: the entry stays OPEN,
  not queued anywhere.
- **Critical spare received before QC.** Trigger: receive-spare on a `qc_required`
  link whose QC isn't ACCEPTED/WAIVED. Behaviour: link set BLOCKED, receipt
  refused. Symptom: "QC must be accepted or waived before receiving this critical
  spare." Risk: stock silently stuck until someone clears QC.
- **Partial / multi-trip return.** Trigger: vendor returns some of the items, then
  more later (often in a different vehicle). Behaviour: one `ReturnableReturnEvent`
  per trip; status derives PARTIALLY_RETURNED then RETURNED. Symptom: pending qty
  shown per line; overdue clears only when fully returned.
- **Permit validity lapses mid-job.** Trigger: an IN_PROGRESS permit passes
  `expires_at`. Behaviour: the scheduler flips it to EXPIRED and notifies; a fresh
  `start` is refused. Symptom: crew can't restart; must `renew`. Risk: if the
  scheduler isn't running, lapsed permits stay "live" (nothing enforces expiry on
  read).
- **Approving your own returnable pass.** Trigger: submitter also holds
  `can_approve_returnable_gatepass`. Behaviour: `approve` returns 400. Symptom:
  "You cannot approve a gate pass you submitted yourself."
- **Non-returnable pass looked for at the gate.** Trigger: gate tries
  `record-return` on an NRGP. Behaviour: 400 "This is a non-returnable gate pass —
  nothing is coming back." It was already CLOSED at gate-out.
- **SAP down during a spare-stock lookup.** Trigger: HANA unreachable. Behaviour:
  `_fetch_sap_spare_stock` returns `available:false` with the error string; local
  stock still shows. Symptom: SAP columns blank with a message; no crash.
- **Stale / detached gate link.** Trigger: receive-spare when no `MaintenanceGateLink`
  or no `spare` on the link. Behaviour: 400 ("No maintenance asset/spare link…" /
  "A spare master link is required…"). Symptom: material physically in, but store
  stock not updated until the link is completed.
- **Re-running PM generation.** Trigger: `generate-due` run twice. Behaviour:
  executions are `get_or_create`'d and the plan still advances `next_due_date`, so no
  duplicates and the plan doesn't get stuck perpetually due.

## Failure modes / what can break

- **Work-permit scheduler not running** → permits never auto-expire; the Fire Head
  never gets the expiry alert and lapsed permits look active. Manager symptom:
  overdue permits linger on the board.
- **Notifications raise** → swallowed and logged (jobs and permit expiry wrap
  notification calls in try/except) so the state transition still commits, but the
  other party is never told. Symptom: gate/department not alerted to a handoff.
- **GRPO reference typo** → `grpo_doc_num`/`grpo_doc_entry` are free input; a wrong
  value is stored verbatim on the receipt and link. Symptom: store shows a GRPO that
  doesn't reconcile in SAP.
- **`accepted_by` / `can_accept_work_permit` are dead** — the field, permission and
  `CanAcceptWorkPermit` class exist but **no `accept` action is wired** in the
  viewset. Symptom: an "acceptance" step implied by the paper form can't be recorded.
- **Global maintenance WO number** (`WO-YYYY-NNN`) is not company-scoped and parsed
  with a bare `int(split("-")[-1])`; a hand-edited value could collide or misparse.
- **Reports are unpaginated** (`[:500]` slices, in-Python spare-cost sums). Large
  date ranges are slow; the dashboard runs many aggregate queries per request.

## Improvement opportunities & known gaps

- Wire (or delete) the work-permit **accept** step and the `can_accept_work_permit`
  permission so it matches the model + frontend permission constant.
- Post GRPO from the receive-spare flow (or explicitly document that store receipt
  and SAP GRPO are separate) so the two can't drift.
- Company-scope `MaintenanceGateEntry.work_order_number`.
- Enforce permit expiry on read (a stored `EXPIRED` transition on access), not only
  via the scheduler.
- Paginate/stream the reports and move spare-cost aggregation into the DB.

## Permissions & roles

Custom permissions live on the unmanaged `MaintenancePermission` /
`ReturnablePermission` sentinels; groups are seeded from
`maintenance/signals.py::MAINTENANCE_ROLE_PERMISSIONS` and
`returnable_items/constants.py::RETURNABLE_ROLE_PERMISSIONS`.

| Group | Can do |
|---|---|
| `maintenance` / `maintenance_admin` | Everything (`__all__`). |
| `maintenance_head` | Manage assets, work orders (assign/approve/close), PM, spares, **fire store + fire reports (review) + fire issue**, **safety fines**, **work permits (issue/approve/close)**, vendors, reports. Effectively the Fire Department Head. |
| `maintenance_technician` | View assets, create/start/complete work orders, submit fire reports, fire issue, create/submit/start work permits (not approve), view PM/vendor. |
| `maintenance_viewer` | Read-only across the module. |
| `returnable_requester` | Raise + submit passes, nothing else. |
| `returnable_approver` | Approve/reject + cancel + reports (separate group so nobody approves their own). |
| `returnable_department` | Raise, submit, acknowledge, close, cancel, reports. |
| `returnable_gate` | Gate-out, gate-in (record return), reject-at-gate. |
| `returnable_viewer` | Read + reports. |

### Daily Electricity — one right per operation

The electricity register is gated per operation rather than by a single
manage-everything flag, so the meter master, daily data entry, corrections and
deletes can each go to a different person. `can_manage_daily_electricity` remains
the legacy superset (it still grants all of the below), so nothing already
assigned changes.

| Permission | Allows |
|---|---|
| `can_view_daily_electricity` | Read the register (and the meter master). |
| `can_view_electricity_meter` | Read the meter master. |
| `can_manage_electricity_meter` | Add / edit / deactivate meters and their rates. |
| `can_add_daily_electricity` | Record a day's reading. |
| `can_edit_daily_electricity` | Correct an existing reading. |
| `can_delete_daily_electricity` | Delete a reading. |
| `can_manage_daily_electricity` | Legacy superset — all of the above. |

Ready-made groups (`python manage.py ensure_role_groups`): *Maint — Electricity
Meter Manager* (meter master only), *Maint — Electricity Reading Operator* (enter
today's reading, cannot rewrite history), *Maint — Electricity Reading
Supervisor* (enter + correct + delete), *Maint — Daily Electricity Viewer*
(read-only) and *Maint — Daily Electricity Manager* (everything).

Permission classes: `maintenance/permissions.py`, `returnable_items/permissions.py`,
`maintenance_gatein/permissions.py`. Note the `CanGateMaintenanceLink` OR — gate
"material-in" operators (holding only `maintenance_gatein.add_maintenancegateentry`)
get **read** access to assets/options/spares/work-orders/spare-stock so they can
link a gate entry, without exposing the Maintenance module UI. Per-viewset gating is
in `get_permissions`.

## Developer file map (backend)

- `maintenance/models.py` — all CMMS + EHS entities (~1940 lines).
- `maintenance/constants.py` — every status/type enum + `choices_payload`.
- `maintenance/views.py` — dashboard, reports, scan, spare-stock (SAP), and all
  viewsets incl. work-order/PM/spare/fire/work-permit/safety-fine lifecycle actions.
- `maintenance/serializers.py` — DRF serializers + nested-write inputs.
- `maintenance/permissions.py` — DRF permission classes + `CanGateMaintenanceLink`.
- `maintenance/signals.py` — group→permission seeding.
- `maintenance/jobs.py` + `management/commands/{expire_work_permits,run_work_permit_scheduler}.py`
  — permit expiry.
- `maintenance/urls.py` — router; `/dashboard/`, `/reports/`, `/scan/*`,
  `/spares/stock/`, `/alerts/`, `/options/`.
- `maintenance_gatein/models.py`, `views.py`, `serializers.py`,
  `services/maintenance_completion.py` — gate inward + receive-spare + completion.
- `returnable_items/models.py`, `views.py`, `serializers.py`, `constants.py`,
  `jobs.py`, `notifications.py`, `management/commands/{check_returnable_items,run_scheduler}.py`.

## Related docs

- Frontend companion: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/maintenance.md`
- Older backend user guide (partly stale): `maintenance/docs/USER_GUIDE.md`
- GRPO backend (actual SAP posting): `grpo/docs/README.md`
- Notifications: `notifications/docs/`
