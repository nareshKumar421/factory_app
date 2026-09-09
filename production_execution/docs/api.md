# Production Execution — API Reference

> Base URL: `/api/v1/production-execution/`
> All endpoints require: `Authorization: Bearer <token>` + `Company-Code: <code>` headers

---

## Master Data

### Production Lines

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/lines/` | `can_view_production_run` | List lines. Filter: `?is_active=true` |
| POST | `/lines/` | `can_manage_production_lines` | Create line. Body: `{"name", "description"}` |
| PATCH | `/lines/<id>/` | `can_manage_production_lines` | Update line |
| DELETE | `/lines/<id>/` | `can_manage_production_lines` | Soft-delete (sets `is_active=false`) |

### Machines

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/machines/` | `can_view_production_run` | List machines. Filter: `?line_id=&machine_type=&is_active=` |
| POST | `/machines/` | `can_manage_machines` | Create machine. Body: `{"name", "machine_type", "line_id"}` |
| PATCH | `/machines/<id>/` | `can_manage_machines` | Update machine |
| DELETE | `/machines/<id>/` | `can_manage_machines` | Soft-delete |

**Machine types:** `FILLER`, `CAPPER`, `CONVEYOR`, `LABELER`, `CODING`, `SHRINK_PACK`, `STICKER_LABELER`, `TAPPING_MACHINE`

### Checklist Templates

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/checklist-templates/` | `can_view_machine_checklist` | List. Filter: `?machine_type=&frequency=` |
| POST | `/checklist-templates/` | `can_manage_checklist_templates` | Create. Body: `{"machine_type", "task", "frequency", "sort_order"}` |
| PATCH | `/checklist-templates/<id>/` | `can_manage_checklist_templates` | Update |
| DELETE | `/checklist-templates/<id>/` | `can_manage_checklist_templates` | Delete |

---

## Production Runs

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/` | `can_view_production_run` | List runs. Filter: `?date=&line_id=&status=&sap_doc_entry=` |
| POST | `/runs/` | `can_create_production_run` | Create run. `run_number` auto-incremented |
| GET | `/runs/<id>/` | `can_view_production_run` | Run detail (includes logs, breakdowns) |
| PATCH | `/runs/<id>/` | `can_edit_production_run` | Update (DRAFT/IN_PROGRESS only) |
| POST | `/runs/<id>/complete/` | `can_complete_production_run` | Complete run. Recomputes all totals |
| POST | `/runs/plan-check/` | `can_view_production_run` | Readiness of a **proposed** run before it is saved — RM/PM availability, clashes with other plans, derived finish time |

**Create run body:**
```json
{
  "sap_doc_entry": 100,
  "line_id": 1,
  "date": "2026-03-07",
  "brand": "Extra Light",
  "pack": "1L x 12",
  "sap_order_no": "PO-12345",
  "rated_speed": 150.0
}
```

**Run statuses:** `DRAFT` → `IN_PROGRESS` → `COMPLETED`

---

## Plan readiness check — `POST /runs/plan-check/`

Called by the planning screen while the form is being filled, so every field is
optional and the answer covers whatever it can work out. Nothing here writes.

**Body**

```json
{
  "line_id": 1,
  "item_code": "FG0001",
  "required_qty": "500",
  "date": "2026-09-08",
  "planned_start_at": "2026-09-08T06:00:00",
  "planned_end_at": null,
  "planned_end_is_manual": false,
  "rated_speed": "3000",
  "pieces_per_case": 20,
  "exclude_run_id": null,
  "stock_basis": "ON_HAND",
  "materials": [{ "material_code": "PM0001", "opening_qty": "10000" }]
}
```

`materials` carries the quantities as they stand on the form. Send them: without
them the check prices the untouched BOM instead of what the run will really
draw, and a supervisor's edit would be judged against the wrong figure.
`exclude_run_id` stops a run being edited from competing with itself.

**Response (abridged)**

```json
{
  "timing": {
    "planned_start_at": "2026-09-08T06:00:00Z",
    "planned_end_at": "2026-09-08T09:20:00Z",
    "derived_end_at": "2026-09-08T09:20:00Z",
    "duration_minutes": 200,
    "bottles": 10000,
    "pieces_per_case": 20,
    "rated_speed": 3000.0,
    "undecidable_because": []
  },
  "materials": {
    "rows": [{
      "item_code": "PM0001", "item_name": "Caps", "uom": "PCS",
      "material_type": "PACKAGING",
      "stock_source": "SAP",
      "searched_warehouses": ["BH-PS", "BH-PC", "BH-PM"],
      "approval_required": true, "approval_qty": 1000.0,
      "qty_at_production_consumption": 3000.0,
      "approval_reason": "3,000.000 is already at BH-PC; the remaining 1,000.000 must be fetched from another godown.",
      "qty_per_case": 20.0, "bom_base_qty": 20.0,
      "required_qty": 10000.0, "bom_required_qty": 10000.0,
      "required_is_overridden": false,
      "on_hand": 8000.0, "committed": 4500.0, "free": 3500.0,
      "other_plan_demand": 2000.0, "balance_after_this_plan": -4000.0,
      "shortfall": 2000.0, "status": "SHORT",
      "warehouses": [{ "warehouse": "BH-PM", "on_hand": 8000.0, "committed": 4500.0 }],
      "competing_runs": [{ "run_id": 41, "run_number": 3, "qty": 2000.0, "line_name": "Line-2" }],
      "on_order_qty": 50000.0, "on_order_earliest_due": "2026-09-09"
    }],
    "summary": { "total_lines": 6, "ok_lines": 4, "tight_lines": 1,
                 "contested_lines": 0, "short_lines": 1, "status": "SHORT" },
    "unusable": [], "resource_lines": [],
    "available": true, "error": "", "warehouses": ["BH-PS", "BH-PC", "BH-PM", "BH-LO", "BH-OT"],
    "basis": "ON_HAND"
  },
  "conflicts": [
    { "type": "LINE_BUSY", "severity": "WARNING", "run_number": 3,
      "window_overlap": true, "message": "Line-1 is already booked 06:00-14:00 by run #3 (...)." }
  ],
  "blocking": { "has_shortage": true, "has_contention": false,
                "has_conflicts": true, "requires_remark": true }
}
```

**How to read the numbers**

- **The requirement is per box x boxes.** `qty_per_case` is `ITT1."Quantity"`
  exactly as authored in SAP, and on this data that is the quantity for **one
  box**: `FG0000118 CANOLA OIL 5 LTR 4 PCS` lists 20 litres of oil, 4 HDPE
  bottles, 4 caps and *1* carton. `required_qty` is that times the case count,
  and nothing else.
  `OITT."Qauntity"` (the BOM's base quantity) comes back as `bom_base_qty` for
  transparency and is **never divided by**. Dividing gives a per-*piece* rate,
  which is right for Planning & Purchase — its plan lines are in pieces — but
  multiplying it by a *case* count understates every line by the pieces-per-box
  (200 litres of oil where 4,000 are needed). The base quantity is not
  dependable either: it is 4 on the 5 LTR 4 PCS, 20 on the 1 LTR 20 PCS, and 1
  on `FG0000013 REFINED OIL 1000 MLS` whose children are per-box just the same.
- **Raw material does not come from SAP.** An `RM` row's `on_hand` is the sum of
  the store keeper's own entries on the Raw Material register
  (`warehouse.RawMaterialStock`), across every warehouse they entered it under
  and regardless of the SAP warehouse scope — the register is hand-curated, so
  filtering it would zero a figure somebody deliberately recorded.
  `stock_source` says which source a row used. An item with **no register row**
  reads `on_hand: 0` with `register_missing: true` and status
  `NO_STOCK_RECORD`: nobody has said the material is there, so the plan may not
  assume it. `committed` and `free` come back **null** for register rows — a
  hand-typed quantity carries no SAP reservations, and borrowing `IsCommited`
  would mix two people's arithmetic in one column. `sap_on_hand` / `sap_free`
  still travel so a stale register is visible; on live data these differ by
  orders of magnitude (32,000 registered against 143.846 in `OITW`).
  Because the register is a database read, RM rows stay usable when the SAP
  stock read fails — only PM rows go `UNKNOWN`.
- **A run raises TWO requests.** Raw material and packing material are asked for
  on separate documents (`BOMRequest.material_kind` = `RM` / `PM`; `MIXED` marks
  requests raised before the split), because they are settled against different
  evidence — RM against the Raw Material register, PM against SAP stock. One
  active request per kind: an open RM request no longer blocks the PM one.
  `POST /warehouse/bom-requests/create/` returns **201** with a `requests` array
  (the first request is also spread at the top level for older clients), or
  **200** with `approval_required: false` when there is genuinely nothing to ask
  for. The run's single `warehouse_approval_status` is the **worst** of its live
  requests — rejected, then pending, then partially approved, then approved — so
  it starts only once both halves are settled. A rejection that has since been
  re-requested is superseded by the follow-up rather than blocking forever.
- **Raw material is always requested, in full.** `approval_required` /
  `approval_qty` say whether a line goes to the warehouse and for how much.
  Raw material always does, at its full quantity — BH-PC staging does not shrink
  an RM request the way it shrinks a PM one — and its approval is checked
  against the **register**, not `OITW`, since SAP disagrees with the tank by
  orders of magnitude. Packing material is requested
  only for the part that must come from a godown other than `BH-PC`
  (Production Consumption — already staged at the line): need 4,000 caps with
  3,000 at BH-PC and the request is for **1,000**. A component SAP classifies
  as neither (`OTHER`) is treated like packing material — the conservative
  reading, since dropping it would silently stop someone being asked for
  material they must hand over. The rule lives in
  `warehouse/services/approval_scope.py` and the request builder and this
  screen both call it, so what the screen promises is what gets requested.
  When every line falls away, `POST /warehouse/bom-requests/create/` returns
  **200** with `approval_required: false` and stamps the run
  `warehouse_approval_status = NOT_REQUIRED`, which passes the start-production
  gate. That is distinct from `NOT_REQUESTED`, which blocks and means nobody
  has submitted anything yet.
- **Where every figure came from.** `searched_warehouses` on each row is the
  scope that applied to *that* component, and `materials.warehouse_scope` is the
  same map for the whole payload (`{"RAW": [...], "PACKAGING": [...]}`). RM and
  PM are read from different stores, so a screen that prints one combined list
  implies oil could be drawn from a carton store. `warehouses[]` (the flat union
  of everything read) is still there for a one-line summary.
  `row.warehouses[]` is the actual holding breakdown, biggest first, and
  **omits warehouses holding zero** — a row of zeroes says nothing about where
  to go and pull the material.
- **Stock scope.** Raw material is counted from the oil stores and packaging from
  the packaging stores — the same per-material-type scope Planning & Purchase
  uses (`PLANNING_PURCHASE_RM_WAREHOUSES` / `_PM_WAREHOUSES`). `BH-WST` (scrap
  and rejected) is never counted.
- **`on_hand` decides a shortfall, not `free`.** The material is physically in
  the building, so the line can physically run it. On this data most components
  are over-committed by open SAP orders, so judging on `free` would report
  nearly every plan as blocked. `free` and `committed` come back on every row so
  the over-commitment stays visible.
- **`other_plan_demand`** is what *this app's* other planned and in-progress runs
  still have to draw, netted of what the warehouse already issued them. Issued
  material has left the store and is already out of `OnHand`; counting it again
  would invent a shortage.
- **`status`** — `OK`, `TIGHT` (enough on hand, but SAP has it committed),
  `CONTESTED` (enough for this plan alone, not once other plans take theirs),
  `SHORT`, `NO_STOCK_RECORD`, and `UNKNOWN`. **`UNKNOWN` means the stock read
  failed, not that stock is zero** — its figures are null and it is never counted
  as a shortage.
- **Conflicts are warnings, never blocks.** `window_unknown` says the two windows
  could not be compared because one of them has no times, so overlap is unproven
  rather than ruled out.
- **Machines are not part of this.** Every production line permanently carries
  the machines it needs, so which machines a run uses is a property of the line
  and never a choice a plan makes. There is no machine clash check and the
  planning screen has no machine picker.

**How the create endpoint uses it**

`POST /runs/` re-runs the material side server-side and returns **400** when a
component is short or contested and `planning_remark` is empty — the override is
allowed, but it has to be written down and attributed. Clashes with other plans
are never gated this way: two runs on one line across a day is ordinary
scheduling. If SAP cannot be reached the guard is skipped rather than holding a
plan hostage to a HANA outage.

**Planning fields on a run**

`planned_start_at`, `planned_end_at`, `planned_end_is_manual` and
`planning_remark` are accepted by `POST /runs/` and `PATCH /runs/<id>/`, and are
returned by the list and detail serializers. When `planned_start_at` is given and
the finish time is not, it is derived as
`required_qty x pieces_per_case / rated_speed` (cases to bottles, then bottles per
hour). A finish time the supervisor typed (`planned_end_is_manual`) is kept as
typed. If speed or bottles-per-case are unknown the finish time stays null — an
invented window would make the clash check confidently wrong.


---

## Hourly Production Logs

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/<run_id>/logs/` | `can_view_production_log` | Get all hourly log entries |
| POST | `/runs/<run_id>/logs/` | `can_edit_production_log` | Bulk create/update (accepts array) |
| PATCH | `/runs/<run_id>/logs/<log_id>/` | `can_edit_production_log` | Update single entry |

**Log entry body:**
```json
{
  "time_slot": "07:00-08:00",
  "time_start": "07:00",
  "time_end": "08:00",
  "produced_cases": 90,
  "machine_status": "RUNNING",
  "recd_minutes": 55,
  "breakdown_detail": "",
  "remarks": ""
}
```

**Pre-defined slots:** 12 hourly slots from 07:00 to 19:00. `recd_minutes` max 60.

---

## Machine Breakdowns

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/<run_id>/breakdowns/` | `can_view_breakdown` | List breakdowns |
| POST | `/runs/<run_id>/breakdowns/` | `can_create_breakdown` | Add breakdown |
| PATCH | `/runs/<run_id>/breakdowns/<id>/` | `can_edit_breakdown` | Update |
| DELETE | `/runs/<run_id>/breakdowns/<id>/` | `can_edit_breakdown` | Delete |

**Body:**
```json
{
  "machine_id": 3,
  "start_time": "2026-03-07T14:00:00",
  "end_time": "2026-03-07T14:35:00",
  "breakdown_minutes": 35,
  "type": "LINE",
  "is_unrecovered": false,
  "reason": "Power cut"
}
```

**Validation:** Machine must belong to the same production line as the run.

---

## Material Usage (Yield)

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/<run_id>/materials/` | `can_view_material_usage` | List. Filter: `?batch_number=` |
| POST | `/runs/<run_id>/materials/` | `can_create_material_usage` | Create (single or array) |
| PATCH | `/runs/<run_id>/materials/<id>/` | `can_edit_material_usage` | Update |

**`wastage_qty`** is auto-calculated: `opening_qty + issued_qty - closing_qty`

---

## Machine Runtime

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/<run_id>/machine-runtime/` | `can_view_machine_runtime` | List entries |
| POST | `/runs/<run_id>/machine-runtime/` | `can_create_machine_runtime` | Bulk create (array) |
| PATCH | `/runs/<run_id>/machine-runtime/<id>/` | `can_create_machine_runtime` | Update |

---

## Manpower

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/runs/<run_id>/manpower/` | `can_view_manpower` | List entries |
| POST | `/runs/<run_id>/manpower/` | `can_create_manpower` | Create/upsert by shift |
| PATCH | `/runs/<run_id>/manpower/<id>/` | `can_create_manpower` | Update |

Manpower entries are **upserted by shift** — posting the same shift twice updates the existing entry.

---

## Line Clearance

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/line-clearance/` | `can_view_line_clearance` | List. Filter: `?date=&line_id=&status=` |
| POST | `/line-clearance/` | `can_create_line_clearance` | Create (auto-creates 9 checklist items) |
| GET | `/line-clearance/<id>/` | `can_view_line_clearance` | Detail with items |
| PATCH | `/line-clearance/<id>/` | `can_create_line_clearance` | Update items + signatures (DRAFT only) |
| POST | `/line-clearance/<id>/submit/` | `can_create_line_clearance` | Submit for QA (DRAFT → SUBMITTED) |
| POST | `/line-clearance/<id>/approve/` | `can_approve_line_clearance_qa` | QA approve/reject |

**Status flow:** `DRAFT` → `SUBMITTED` → `CLEARED` or `NOT_CLEARED`

**Submit requires:** All 9 items must have a result (YES/NO) + at least one signature.

**Approve body:** `{"approved": true}` or `{"approved": false}`

---

## Machine Checklists

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/machine-checklists/` | `can_view_machine_checklist` | List. Filter: `?machine_id=&month=&year=&frequency=` |
| POST | `/machine-checklists/` | `can_create_machine_checklist` | Create single entry |
| POST | `/machine-checklists/bulk/` | `can_create_machine_checklist` | Bulk create/update (array) |
| PATCH | `/machine-checklists/<id>/` | `can_create_machine_checklist` | Update entry |

---

## Waste Management

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/waste/` | `can_view_waste_log` | List. Filter: `?run_id=&approval_status=` |
| POST | `/waste/` | `can_create_waste_log` | Create waste log |
| GET | `/waste/<id>/` | `can_view_waste_log` | Detail |
| POST | `/waste/<id>/approve/engineer/` | `can_approve_waste_engineer` | Engineer sign |
| POST | `/waste/<id>/approve/am/` | `can_approve_waste_am` | AM sign (requires engineer first) |
| POST | `/waste/<id>/approve/store/` | `can_approve_waste_store` | Store sign (requires AM first) |
| POST | `/waste/<id>/approve/hod/` | `can_approve_waste_hod` | HOD sign (requires store first) → FULLY_APPROVED |

**Approval body:** `{"sign": "Approver Name"}`

**Sequential:** Engineer → AM → Store → HOD. Cannot skip levels.

---

## Reports

| Method | URL | Permission | Description |
|--------|-----|------------|-------------|
| GET | `/reports/daily-production/?date=` | `can_view_reports` | Daily production report (required: `date`) |
| GET | `/reports/yield/<run_id>/` | `can_view_reports` | Yield report (materials + runtime + manpower) |
| GET | `/reports/line-clearance/` | `can_view_reports` | Clearance report. Filter: `?date_from=&date_to=` |
| GET | `/reports/analytics/` | `can_view_reports` | Analytics dashboard. Filter: `?date_from=&date_to=&line_id=` |

**Analytics returns:**
```json
{
  "total_runs": 10,
  "total_production": 5000,
  "total_pe_minutes": 4800,
  "total_breakdown_minutes": 200,
  "available_time_minutes": 7200,
  "operating_time_minutes": 7000,
  "availability_percent": 97.2
}
```

---

## Response Format

**Success (list):**
```json
[{"id": 1, "name": "Line-1", ...}]
```

**Success (detail):**
```json
{"id": 1, "name": "Line-1", ...}
```

**Error:**
```json
{"detail": "Error message here."}
```

**Validation error:**
```json
{"detail": "Invalid data.", "errors": {"field": ["Error message"]}}
```
