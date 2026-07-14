# Production Execution — Backend (`production_execution`)

> Django app: `production_execution`
> Base URL: `/api/v1/production-execution/` (mounted in `config/urls.py`)
> Every endpoint requires an authenticated user **and** company context
> (`IsAuthenticated` + `company.permissions.HasCompanyContext`). The active company
> is resolved from `request.company.company.code`.
>
> Frontend counterpart: [FactoryFlow `docs/modules/production.md`](../../../FactoryFlow/docs/modules/production.md)
> (path relative to the repo roots: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/production.md`).

> **Accuracy note (2026-07):** this file was rewritten from the code. The previous
> version described an "hourly ProductionLog" model and SAP goods-receipt-on-complete
> that **no longer match the code**. Production is now driven from a product **SKU**
> (not necessarily a SAP production order), the timeline is modelled with
> **segments + breakdowns**, and finished-goods posting to SAP has moved to the
> **warehouse** app's FG-receipt flow. Trust this file and the code over older docs.

---

## Overview — what it does & who uses it

This app runs the factory floor **after** a product is chosen to be made: line
clearance, the live run timeline (start / stop / breakdown), material-consumption &
yield capture, machine runtime, manpower, machine maintenance checklists, waste
logging & approval, per-run resource costing, in-process/final QC records, and a
large analytics/report surface (OEE, downtime, yield, cost, waste, plan-vs-actual,
SAP stock movement).

Primary users:

- **Line supervisors / operators** — create runs, run the timeline, log breakdowns,
  capture material closing quantities and waste.
- **Shift engineers / maintenance** — breakdowns auto-raise maintenance work orders.
- **QA** — approve line clearance (may act through the `quality_control` app).
- **Store / HOD** — approve waste.
- **Production managers** — reports, OEE, cost, movement dashboards.

Rollout is still ramping, so several capabilities are **built but partially wired**
(4-level waste approval, SAP goods-receipt-on-complete) — called out under
_Improvement opportunities_.

**Depends on:** `company` (tenant scope), `sap_client` (HANA + Service Layer),
`warehouse` (BOM approval gate + FG receipt to SAP), `maintenance` (breakdown work
orders), `quality_control` (QC records + line-clearance QA), `notifications`.

---

## Key concepts & entities

Models live in `models.py`, organised in three levels plus resource/cost/QC groups.

### Level 1 — Master data
| Model | Purpose |
|-------|---------|
| `ProductionLine` | A packing/production line. Unique per `(company, name)`. Soft-deleted via `is_active`. |
| `Machine` | Physical machine on a line; typed by `MachineType` (FILLER, CAPPER, LABELER, …). |
| `MachineChecklistTemplate` | Reusable maintenance task per `machine_type` + `frequency` (DAILY/WEEKLY/MONTHLY). |
| `BreakdownCategory` | Configurable breakdown type (replaces the legacy `BreakdownType` enum). |
| `LineSkuConfig` | Preset for a line (rated speed, labour count, cost rates, supervisor/operators). Auto-fill priority: exact `sku_code` match → line-level default (blank `sku_code`). Presets are **copied** onto a run at creation, not referenced live. |

### Level 2 — The run and its timeline
| Model | Purpose |
|-------|---------|
| `ProductionRun` | **Central entity.** Unique per `(company, date, run_number)`; `run_number` auto-increments per company/day. Holds `sap_doc_entry` (optional OWOR link), `product`, `required_qty`, `warehouse_approval_status`, `rated_speed`, `machines` (M2M), summary totals, `rejected_qty`/`reworked_qty`, SAP receipt/sync fields, and `status` (`DRAFT`→`IN_PROGRESS`→`COMPLETED`). All 31 module permissions are declared on its `Meta`. |
| `ProductionSegment` | One **running period**. `is_active=True` while producing; closed on stop/breakdown with `produced_cases`. `duration_minutes` is derived. |
| `MachineBreakdown` | A stoppage keyed to a `BreakdownCategory` (+ optional `machine`). `is_active` while ongoing; `is_unrecovered` when stopped unfixed; reverse-links to a `MaintenanceWorkOrder`. |
| `ProductionMaterialUsage` | Per-material yield row: `opening/issued/closing`, and `wastage_qty = opening + issued − closing`. |
| `MachineRuntime` | Runtime/downtime minutes per machine type. |
| `ProductionManpower` | Worker count per `shift` (unique per `run+shift`). |

### Level 3 — Quality & maintenance
| Model | Purpose |
|-------|---------|
| `LineClearance` (+ `LineClearanceItem`) | Pre-production checklist, optionally linked to a run. Status `DRAFT`→`SUBMITTED`→`CLEARED`/`NOT_CLEARED`. On create, 9 `STANDARD_CLEARANCE_ITEMS` are inserted. Current UX drives a single `all_checks_passed` toggle that bulk-sets every item on submit. |
| `MachineChecklistEntry` | A performed checklist task; unique per `(machine, template, date)`; `OK`/`NOT_OK`/`NA`. |
| `WasteLog` | Waste record with **four** sequential sign slots (engineer → AM → store → HOD) and `wastage_approval_status`. |

### Resource tracking & cost
`ResourceElectricity`, `ResourceWater`, `ResourceGas`, `ResourceCompressedAir`,
`ResourceLabour`, `ResourceMachineCost`, `ResourceOverhead` — each computes
`total_cost` in its own `save()`. `ProductionRunCost` is a `OneToOne` roll-up
(materials + labour + machine + utilities + overhead → `total_cost`, `per_unit_cost`)
recomputed by `services/cost_calculator.recalculate_run_cost()` after every
resource/material change.

### QC
`InProcessQCCheck` (many per run; min/max/actual + PASS/FAIL/NA) and `FinalQCCheck`
(`OneToOne`; JSON `parameters`, overall PASS/FAIL/CONDITIONAL). These endpoints still
exist here, but the **QC UI has moved** to the `quality_control` app / `/qc/production`.

---

## End-to-end flows (as the server sees them)

Business logic is centralised in `services/production_service.py`
(`ProductionExecutionService`, instantiated per request with the company code).

### 1. Create a run — `POST /runs/` → `create_run()`
1. Resolve & validate the line (must be `is_active`).
2. `run_number = last_run_for_company_and_date + 1`.
3. Copy preset cost fields (`electricity_cost_per_unit`, `labour_cost_per_hour`) and
   set the `machines` M2M from `machine_ids`.
4. **Materials:** if `materials[]` supplied, save them; **otherwise**
   `auto_populate_materials_from_bom()` pulls the BOM from SAP — priority
   `sap_doc_entry` (WOR1 order components) → `product` item code (OITT/ITT1 master).
   Any SAP failure here is **caught and logged as a warning**; the run is still created.
5. Sync a default `ResourceLabour` entry from `labour_count`, then
   `recalculate_run_cost()`. Status = `DRAFT`.

### 2. Request materials from the warehouse (BOM approval gate)
Not in this app — the **warehouse** app owns it. Submitting a `BOMRequest`
(`warehouse.services.warehouse_service`) sets `run.warehouse_approval_status='PENDING'`;
warehouse approval sets it to `APPROVED`/`PARTIALLY_APPROVED`, rejection to `REJECTED`.
`MaterialUsageListCreateAPI.get` enriches material rows with the latest `BOMRequest`
line status (requested/approved/available stock).

### 3. Line clearance — `create → update → submit → approve`
1. `create_clearance()` inserts the 9 standard items (`status=DRAFT`).
2. `update_clearance()` sets `all_checks_passed` + `production_supervisor_sign`
   (DRAFT only).
3. `submit_clearance()` requires a supervisor name, bulk-sets every item to YES/NO
   from the toggle, → `SUBMITTED`.
4. `approve_clearance()` (SUBMITTED only) → `CLEARED` (approved) or `NOT_CLEARED`.
   Permission `CanApproveLineClearanceQA` **also accepts** `quality_control.can_approve_line_clearance_qc`.

### 4. Start production — `POST /runs/{id}/start-production/` → `start_production()`
Hard gates, each raising `ValueError` → HTTP 400:
- `warehouse_approval_status` must **not** be `NOT_REQUESTED`, `PENDING`, or `REJECTED`
  (i.e. must be `APPROVED`/`PARTIALLY_APPROVED`).
- A `LineClearance` with `status=CLEARED` must exist for the run.
- No active segment and no active breakdown may already exist.

On success: create the first active `ProductionSegment`, flip `DRAFT`→`IN_PROGRESS`.

### 5. Timeline actions (all block on a `COMPLETED` run)
- **Stop** (`stop_production`): close the active segment, record `produced_cases`,
  recompute totals.
- **Add breakdown** (`add_breakdown`): close the active segment, open a breakdown by
  `breakdown_category` (+ optional machine). If `create_maintenance_work_order`
  (default true) and an asset resolves, a `MaintenanceWorkOrder` is created and the
  asset set to `BREAKDOWN`.
- **Resolve breakdown** (`resolve_breakdown`) — three actions:
  `start_production` (close breakdown, open a **new** segment), `stop_production`
  (close only), `stop_unrecovered` (also mark `is_unrecovered`). The linked
  maintenance WO is synced (→ COMPLETED, or IN_PROGRESS for unrecovered) and the
  asset set to `UNDER_REPAIR`.
- Segment/breakdown remark edits, and legacy direct breakdown CRUD, also exist.
- `_recompute_run_totals()` re-sums running minutes (from closed segments) and
  breakdown minutes after every change.

### 6. Complete the run — `POST /runs/{id}/complete/` → `complete_run()`
Requires **no** active segment and **no** active breakdown. Sets `total_production`,
recomputes totals, flips → `COMPLETED` (which **locks** all edits).

> **SAP goods receipt on completion is intentionally disabled** —
> `_post_goods_receipt_to_sap()` is commented out in `complete_run()` because runs now
> start from a SKU and usually have no `OWOR` DocEntry to receipt against. FG posting to
> SAP happens later through the **warehouse FG receipt** (after Final QC PASS).

### 7. After completion (downstream, other apps)
Final QC is requested (`quality_control`); on APPROVED + PASS the frontend creates a
**warehouse FG receipt**, and warehouse posts the goods receipt to SAP.

---

## Critical business rules & invariants

- **Company scoping:** every service query filters by the request's company. A run
  created in company A is invisible from company B (see the cross-company memo).
- **`run_number`** is unique per `(company, date)` and auto-increments; concurrent
  creates on the same day can race on the `unique_together`.
- **COMPLETED = locked:** `update_run`, material/runtime/manpower/breakdown edits all
  raise `ValueError` on a completed run.
- **Start gates:** warehouse approval **and** a CLEARED line clearance are both
  mandatory before a run can start (§4).
- **Material wastage** is always `opening + issued − closing` (recomputed on save).
- **Resource cost** `total_cost` is computed in each model's `save()`; the run roll-up
  is recomputed after every resource mutation. `per_unit_cost = total_cost / total_production`
  (0 when nothing produced).
- **Preset copy semantics:** `LineSkuConfig` values are copied onto the run at creation;
  later edits to the preset do **not** retro-change existing runs.
- **Line-clearance QA & clearance viewing** honor `quality_control` permissions in
  addition to this app's own (`permissions.py`).
- **OEE availability** uses a **fixed 720-minute** available window per run
  (`OEEAnalyticsAPI`), so availability is only meaningful for ~12-hour runs.

---

## Integrations & cross-module boundaries

| Boundary | Direction | What crosses |
|----------|-----------|--------------|
| `sap_client` → HANA | read | `services/sap_reader.ProductionOrderReader` runs raw SQL against `OWOR/WOR1/OITT/ITT1/OITM/OWHS` for orders, BOM, item search. `_execute()` opens a direct `hdbcli` connection per query. |
| `sap_client` → Service Layer | write | `services/sap_writer.GoodsReceiptWriter` posts `InventoryGenEntries` (`BaseType=202`). **Currently unused** by the completion flow. Both SAP calls use `verify=False` (TLS not verified). |
| SAP stock movement | read | `services/production_movement_service` + `_reader` build the movement dashboard; view maps `SAPConnectionError→503`, `SAPDataError→502`. |
| `warehouse` | in ← | Warehouse writes `run.warehouse_approval_status` (BOM approval) and owns FG receipts / SAP FG posting. This app **reads** `BOMRequest` lines to enrich material rows. |
| `maintenance` | out → | Breakdowns create/sync `MaintenanceWorkOrder`s and flip asset status. |
| `quality_control` | both | Line-clearance QA permission; FinalQC gates the FG receipt. |
| `notifications` | out → | `signals.py` fires `notify_production_run_sap_result` when `sap_sync_status` transitions to SUCCESS/FAILED (rarely, since receipt posting is disabled). |

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **SAP down while creating a run** → `auto_populate_materials_from_bom` throws; the
   exception is caught and logged, run is created with **no materials** → operator sees
   an empty materials tab and must add rows manually → silent data gap; no banner tells
   them the BOM never loaded.
2. **BOM submitted to warehouse but not yet approved** → `start_production` raises
   "BOM request is pending warehouse approval" → Start button 400s / is disabled →
   run stalls until warehouse acts; no SLA/escalation.
3. **Warehouse rejects the BOM** → status `REJECTED`; start stays blocked with the
   rejection message → operator must re-submit a corrected BOM request; there is no
   "resubmit" affordance in this app (owned by warehouse).
4. **Line clearance rejected (`NOT_CLEARED`)** → `start_production` only accepts a
   `CLEARED` clearance, so a rejected one never satisfies the gate → operator must create
   a **new** clearance and get it approved; the old NOT_CLEARED record lingers.
5. **Trying to complete with production still running / an open breakdown** →
   `complete_run` raises "Stop production first" / "Resolve it first" → Complete 400s →
   correct guard, but totals won't reflect the un-stopped segment until it's closed.
6. **Breakdown logged with no machine** → allowed (`machine` is optional); if no
   maintenance asset resolves, **no** work order is created (just logged) → maintenance
   team may never see a line-level stoppage → downtime is captured for OEE but no WO.
7. **Completed run needs a correction** → every edit path raises `ValueError` → nothing
   is editable → the only recovery is DB-level intervention; there is no reopen action.
8. **Retry SAP goods receipt on a SKU run** → `retry_sap_goods_receipt` requires a
   `sap_doc_entry`; SKU runs have none → 400 "not linked to a SAP production order" →
   the retry endpoint is effectively dead for the current SKU-based flow.
9. **Cross-company blank data** → a manager switched to a sibling company sees none of
   the other company's runs/clearances (all reads are company-scoped) → expected, but a
   frequent "where did my run go?" support question.
10. **Duplicate/again-submitted BOM request** → material enrichment picks the **latest**
    `BOMRequest` (`order_by('-created_at').first()`) → older requests are ignored →
    generally correct, but approved-then-superseded requests can confuse the displayed
    approved quantities.

---

## Failure modes / what can break

| Failure | Symptom a user/manager notices |
|---------|--------------------------------|
| HANA unreachable | SKU search, BOM preview, SAP order list, and the movement report return **503**; new runs get empty BOMs. |
| Service Layer / movement data error | Movement report returns **502** ("SAP data error"). |
| `Company-Code` missing/unknown | `HasCompanyContext` denies; service raises "Company not found". |
| Start gate not met | 400 with a precise reason (warehouse/clearance). |
| Concurrent same-day run creation | Possible `IntegrityError` on `(company, date, run_number)`. |
| Waste "approval" | A single approve call jumps straight to `FULLY_APPROVED` (see gaps) — no engineer→AM→store→HOD trail is enforced. |
| Notification worker down | SAP-result notifications are queued on commit and can be lost if the notification pipeline fails (logged, non-fatal). |

---

## Improvement opportunities & known gaps

- **Waste approval is single-step in code.** The model has 4 sequential sign slots and
  4 role permissions/endpoints, but every role endpoint subclasses `WasteApproveAPI`,
  and `approve_waste()` sets the **HOD** sign and `FULLY_APPROVED` in one shot.
  `PARTIALLY_APPROVED` is never produced. The sequential chain is unimplemented.
- **SAP goods-receipt-on-complete is dead code** (`_post_goods_receipt_to_sap`,
  `retry_sap_goods_receipt`, the `sap_sync_*` fields, and the notification signal).
  Either wire it to the SKU/FG-receipt path or remove it to avoid confusion.
- **OEE availability** is hard-coded to 720 min; parameterise per shift/run length.
- **BOM auto-fetch failure is silent** — surface a warning flag on the run so operators
  know materials didn't load.
- **`verify=False`** on both SAP HTTP paths — TLS verification is disabled.
- **N+1 risk** in list/detail serializers (breakdown → maintenance WO/asset,
  material → waste aggregation); watch under load (see the performance-audit memo).

---

## Permissions & roles

31 permissions are declared on `ProductionRun.Meta.permissions`; the Django auth group
`production_execution` is created by migration `0002`. DRF classes are in
`permissions.py`. Highlights:

| Capability | Permission(s) |
|------------|---------------|
| View / create / edit / complete run | `can_view_production_run`, `can_create_production_run`, `can_edit_production_run`, `can_complete_production_run` |
| Master data | `can_manage_production_lines`, `can_manage_machines`, `can_manage_checklist_templates` |
| Breakdowns | `can_view/create/edit_breakdown` |
| Materials / runtime / manpower | `can_view/create/edit_material_usage`, `can_view/create_machine_runtime`, `can_view/create_manpower` |
| Line clearance | `can_view_line_clearance`, `can_create_line_clearance`, `can_approve_line_clearance_qa` (**or** `quality_control.can_approve_line_clearance_qc`) |
| Machine checklists | `can_view/create_machine_checklist` |
| Waste | `can_view/create_waste_log`, `can_approve_waste_engineer/am/store/hod` |
| Reports | `can_view_reports` |

Resource-tracking & QC endpoints reuse `CanCreateMaterialUsage` / `CanViewProductionRun`
rather than dedicated perms.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/production_execution/`)**
- `models.py` — all entities & enums.
- `services/production_service.py` — `ProductionExecutionService`: runs, timeline, clearance, checklists, waste, reports.
- `services/cost_calculator.py` — `recalculate_run_cost()`.
- `services/sap_reader.py` — `ProductionOrderReader` (HANA reads: orders/BOM/items).
- `services/sap_writer.py` — `GoodsReceiptWriter` (Service Layer; currently unused).
- `services/production_movement_service.py` / `production_movement_reader.py` — SAP stock-movement dashboard.
- `services/report_service.py` — OEE-trend / downtime-pareto / cost / waste-trend / monthly / plan-vs-prod / procurement-vs-planned.
- `views.py` — ~60 `APIView`s (master data, runs, timeline, resources, cost, QC, reports, SAP proxies, line configs).
- `serializers.py` — request/response shapes.
- `permissions.py` — DRF permission classes.
- `urls.py` — URL patterns (all under the base URL above).
- `signals.py` / `notifications.py` — SAP-sync notification wiring.
- `management/commands/setup_production_groups.py` — group/permission setup.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow/src/modules/production/`)** — see the paired doc.

---

## Related docs

- **Frontend:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/production.md`
- Same-folder guides (verify against code before trusting): `docs/api.md`,
  `docs/cost_guide.md`, `docs/qc_guide.md`, `docs/resources_guide.md`,
  `docs/bom_auto_fetch_guide.md`, `docs/frontend_api_guide.md`.
