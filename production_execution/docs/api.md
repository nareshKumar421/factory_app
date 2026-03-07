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
| GET | `/runs/` | `can_view_production_run` | List runs. Filter: `?date=&line_id=&status=&production_plan_id=` |
| POST | `/runs/` | `can_create_production_run` | Create run. `run_number` auto-incremented |
| GET | `/runs/<id>/` | `can_view_production_run` | Run detail (includes logs, breakdowns) |
| PATCH | `/runs/<id>/` | `can_edit_production_run` | Update (DRAFT/IN_PROGRESS only) |
| POST | `/runs/<id>/complete/` | `can_complete_production_run` | Complete run. Recomputes all totals |

**Create run body:**
```json
{
  "production_plan_id": 5,
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
