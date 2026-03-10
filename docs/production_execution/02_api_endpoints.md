# Production Execution — API Endpoints

> Base URL: `/api/v1/production-execution/`
> All endpoints require: `Authorization: Token <token>` + `Company-Code: <code>`
> View pattern: Class-based APIView (same as `production_planning`)

---

## Master Data

### Production Lines

```
GET    /api/v1/production-execution/lines/
       → List all production lines for the company
       → Query params: ?is_active=true

POST   /api/v1/production-execution/lines/
       → Create a new production line
       → Body: { "name": "Line-1", "description": "..." }

PATCH  /api/v1/production-execution/lines/<line_id>/
       → Update a production line

DELETE /api/v1/production-execution/lines/<line_id>/
       → Soft-delete (set is_active=false)
```

### Machines

```
GET    /api/v1/production-execution/machines/
       → List machines
       → Query params: ?line_id=1&machine_type=FILLER&is_active=true

POST   /api/v1/production-execution/machines/
       → Create a machine
       → Body: { "name": "10-Head Filler", "machine_type": "FILLER", "line_id": 1 }

PATCH  /api/v1/production-execution/machines/<machine_id>/
       → Update a machine

DELETE /api/v1/production-execution/machines/<machine_id>/
       → Soft-delete (set is_active=false)
```

### Checklist Templates

```
GET    /api/v1/production-execution/checklist-templates/
       → List templates
       → Query params: ?machine_type=FILLER&frequency=DAILY

POST   /api/v1/production-execution/checklist-templates/
       → Create a template task
       → Body: { "machine_type": "FILLER", "task": "Clean oil storage tank", "frequency": "DAILY", "sort_order": 1 }

PATCH  /api/v1/production-execution/checklist-templates/<id>/
       → Update a template

DELETE /api/v1/production-execution/checklist-templates/<id>/
       → Delete a template
```

---

## Production Runs

```
GET    /api/v1/production-execution/runs/
       → List production runs
       → Query params: ?date=2026-03-07&line_id=1&status=IN_PROGRESS&production_plan_id=5

POST   /api/v1/production-execution/runs/
       → Start a new production run
       → Body:
         {
           "production_plan_id": 5,
           "line_id": 1,
           "date": "2026-03-07",
           "brand": "Extra Light",
           "pack": "1L x 12",
           "sap_order_no": "PO-12345",
           "rated_speed": 150.0
         }
       → run_number auto-incremented per plan+date
       → Returns 201 with run detail

GET    /api/v1/production-execution/runs/<run_id>/
       → Get run detail (includes logs, breakdowns, summary fields)

PATCH  /api/v1/production-execution/runs/<run_id>/
       → Update run header fields (rated_speed, brand, pack, etc.)
       → Only DRAFT or IN_PROGRESS runs

POST   /api/v1/production-execution/runs/<run_id>/complete/
       → Complete a run
       → Triggers: recompute all summary fields, lock for editing
```

---

## Hourly Production Logs

```
GET    /api/v1/production-execution/runs/<run_id>/logs/
       → Get all 12 hourly log entries for a run
       → Returns pre-defined slots (07:00-08:00 through 18:00-19:00)

POST   /api/v1/production-execution/runs/<run_id>/logs/
       → Create or bulk-update hourly logs
       → Body (array):
         [
           {
             "time_slot": "07:00-08:00",
             "time_start": "07:00",
             "time_end": "08:00",
             "produced_cases": 90,
             "machine_status": "RUNNING",
             "recd_minutes": 55,
             "breakdown_detail": "Sticker changeover"
           },
           ...
         ]

PATCH  /api/v1/production-execution/runs/<run_id>/logs/<log_id>/
       → Update a single hourly log entry
```

---

## Machine Breakdowns

```
GET    /api/v1/production-execution/runs/<run_id>/breakdowns/
       → List breakdowns for a run

POST   /api/v1/production-execution/runs/<run_id>/breakdowns/
       → Add a breakdown
       → Body:
         {
           "machine_id": 3,
           "start_time": "2026-03-07T14:00:00",
           "end_time": "2026-03-07T14:35:00",
           "breakdown_minutes": 35,
           "type": "LINE",
           "is_unrecovered": false,
           "reason": "Power cut",
           "remarks": ""
         }

PATCH  /api/v1/production-execution/runs/<run_id>/breakdowns/<id>/
       → Update a breakdown

DELETE /api/v1/production-execution/runs/<run_id>/breakdowns/<id>/
       → Delete a breakdown
```

---

## Material Usage (Yield)

```
GET    /api/v1/production-execution/runs/<run_id>/materials/
       → Get material usage for a run
       → Query params: ?batch_number=1

POST   /api/v1/production-execution/runs/<run_id>/materials/
       → Add or bulk-create material entries
       → Body (single or array):
         {
           "material_name": "Bottle (500gm)",
           "material_code": "RM-BOT-500",
           "opening_qty": 5000,
           "issued_qty": 2000,
           "closing_qty": 4800,
           "uom": "PCS",
           "batch_number": 1
         }
       → wastage_qty auto-calculated on save

PATCH  /api/v1/production-execution/runs/<run_id>/materials/<id>/
       → Update a material entry
```

---

## Machine Runtime

```
GET    /api/v1/production-execution/runs/<run_id>/machine-runtime/
       → Get machine runtime entries for a run

POST   /api/v1/production-execution/runs/<run_id>/machine-runtime/
       → Add or bulk-create machine runtime
       → Body (array):
         [
           { "machine_type": "FILLER", "runtime_minutes": 420, "downtime_minutes": 10 },
           { "machine_type": "CAPPER", "runtime_minutes": 415, "downtime_minutes": 15 },
           ...
         ]

PATCH  /api/v1/production-execution/runs/<run_id>/machine-runtime/<id>/
       → Update a machine runtime entry
```

---

## Manpower

```
GET    /api/v1/production-execution/runs/<run_id>/manpower/
       → Get manpower entries for a run

POST   /api/v1/production-execution/runs/<run_id>/manpower/
       → Add manpower entry
       → Body:
         {
           "shift": "MORNING",
           "worker_count": 12,
           "supervisor": "Ramesh Kumar",
           "engineer": "Suresh Singh"
         }

PATCH  /api/v1/production-execution/runs/<run_id>/manpower/<id>/
       → Update manpower entry
```

---

## Line Clearance

```
GET    /api/v1/production-execution/line-clearance/
       → List clearances
       → Query params: ?date=2026-03-07&line_id=1&status=CLEARED

POST   /api/v1/production-execution/line-clearance/
       → Create a line clearance (auto-creates 9 checklist items)
       → Body:
         {
           "date": "2026-03-07",
           "line_id": 1,
           "production_plan_id": 5,
           "document_id": "PRD-OIL-FRM-15-00-00-04"
         }

GET    /api/v1/production-execution/line-clearance/<id>/
       → Get clearance detail (with checklist items)

PATCH  /api/v1/production-execution/line-clearance/<id>/
       → Update clearance (checklist results, signatures)
       → Body:
         {
           "items": [
             { "id": 1, "result": "YES", "remarks": "" },
             { "id": 2, "result": "YES", "remarks": "" },
             ...
           ],
           "production_supervisor_sign": "Supervisor Name",
           "production_incharge_sign": "Incharge Name"
         }

POST   /api/v1/production-execution/line-clearance/<id>/submit/
       → Submit for QA approval (DRAFT -> SUBMITTED)

POST   /api/v1/production-execution/line-clearance/<id>/approve/
       → QA approve or reject
       → Body: { "approved": true }
       → If approved: status -> CLEARED, qa_approved=true
       → If rejected: status -> NOT_CLEARED
```

---

## Machine Checklists

```
GET    /api/v1/production-execution/machine-checklists/
       → List entries (calendar view data)
       → Query params: ?machine_id=3&month=3&year=2026&frequency=DAILY

POST   /api/v1/production-execution/machine-checklists/
       → Create a single entry

POST   /api/v1/production-execution/machine-checklists/bulk/
       → Bulk create/update (for calendar save)
       → Body (array):
         [
           {
             "machine_id": 3,
             "template_id": 10,
             "date": "2026-03-07",
             "status": "OK",
             "operator": "Ramesh",
             "shift_incharge": "Suresh"
           },
           ...
         ]

PATCH  /api/v1/production-execution/machine-checklists/<id>/
       → Update a single entry
```

---

## Waste Management

```
GET    /api/v1/production-execution/waste/
       → List waste logs
       → Query params: ?run_id=5&approval_status=PENDING

POST   /api/v1/production-execution/waste/
       → Create waste log
       → Body:
         {
           "production_run_id": 5,
           "material_name": "Bottle",
           "material_code": "RM-BOT-500",
           "wastage_qty": 200,
           "uom": "PCS",
           "reason": "Dented during transport"
         }

GET    /api/v1/production-execution/waste/<id>/
       → Get waste detail

POST   /api/v1/production-execution/waste/<id>/approve/engineer/
       → Engineer approval
       → Body: { "sign": "Engineer Name", "remarks": "" }

POST   /api/v1/production-execution/waste/<id>/approve/am/
       → AM approval (requires engineer signed first)

POST   /api/v1/production-execution/waste/<id>/approve/store/
       → Store approval (requires AM signed first)

POST   /api/v1/production-execution/waste/<id>/approve/hod/
       → HOD approval (requires store signed first)
       → After HOD signs: status -> FULLY_APPROVED
```

---

## Reports

```
GET    /api/v1/production-execution/reports/daily-production/
       → Daily production report
       → Query params: ?date=2026-03-07&line_id=1
       → Returns: runs with hourly logs, breakdowns, totals

GET    /api/v1/production-execution/reports/yield/<run_id>/
       → Yield report for a specific run
       → Returns: material usage + machine runtime + manpower

GET    /api/v1/production-execution/reports/line-clearance/
       → Line clearance summary
       → Query params: ?date_from=2026-03-01&date_to=2026-03-31

GET    /api/v1/production-execution/reports/analytics/
       → Dashboard analytics
       → Query params: ?date_from=...&date_to=...&line_id=...
       → Returns: OEE, line efficiency, material loss %, downtime hours, production vs plan %
```

---

## Response Format

All endpoints follow the project's standard response pattern:

**Success (list):**
```json
[
  { "id": 1, "name": "Line-1", ... },
  { "id": 2, "name": "Line-2", ... }
]
```

**Success (detail):**
```json
{ "id": 1, "name": "Line-1", ... }
```

**Error:**
```json
{ "detail": "Error message here." }
```

**Validation error:**
```json
{ "detail": "Invalid data.", "errors": { "field": ["Error message"] } }
```
