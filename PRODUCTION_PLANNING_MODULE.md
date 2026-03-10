# Production Planning Module — Design Document

**Project:** Sampooran Factory App v2
**Module Name:** `production_planning`
**Author:** Design Document (AI-assisted)
**Date:** 2026-03-06
**Status:** Draft — Pending Implementation

---

## 1. Overview

The Production Planning module bridges SAP production orders with on-the-ground factory execution. Head office creates production orders in SAP B1. This module fetches those orders, lets planners break them into weekly targets, and lets the production team record daily output — all tracked against the original SAP plan.

### Core Principles
- **SAP is the source of truth** for the plan (what to make, how much, by when)
- **App manages execution** (weekly breakdown, daily production logging, closure)
- **No duplicate data entry** — plan is pulled from SAP, not manually typed
- **Plan lifecycle** — OPEN → IN_PROGRESS → COMPLETED/CLOSED
- **Material requirement** is pre-calculated from SAP BOM components at fetch time

---

## 2. Business Flow

```
SAP Head Office
    |
    | Creates Production Order (OWOR) in SAP B1
    |
    v
[1] Factory Planner: Fetch Monthly Plan
    - Calls API: GET /api/v1/production-planning/sap-orders/?month=2026-03
    - Fetches OWOR records from SAP HANA for the selected month
    - Reviews the list of production orders (ItemCode, PlannedQty, DueDate)
    |
    v
[2] Factory Planner: Import Plan
    - Calls API: POST /api/v1/production-planning/import/
    - Saves selected SAP orders as local ProductionPlan records
    - System auto-calculates material requirements from BOM (WOR1)
    |
    v
[3] Factory Planner: Create Weekly Plan
    - Calls API: POST /api/v1/production-planning/<plan_id>/weekly-plans/
    - Breaks the monthly plan into Week 1, Week 2, Week 3, Week 4
    - Sets target quantity per week and date range
    |
    v
[4] Production Team: Record Daily Production
    - Calls API: POST /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/
    - Records how much was produced each day
    - Progress is tracked: produced_so_far vs weekly_target vs monthly_target
    |
    v
[5] Planner/Manager: Close Plan
    - Calls API: POST /api/v1/production-planning/<plan_id>/close/
    - Marks plan as COMPLETED
    - Optionally syncs closure status back to SAP (future scope)
```

---

## 3. SAP Integration

### 3.1 SAP HANA — Tables Used

| SAP Table | Description | Key Fields Used |
|-----------|-------------|-----------------|
| `OWOR` | Production Orders (header) | DocNum, DocEntry, ItemCode, Dscription, PlannedQty, CmpltQty, RjctQty, PlannedDate, DueDate, Status, CardCode, BPLId |
| `WOR1` | Production Order Components (BOM lines) | DocEntry, ItemCode, Dscription, PlannedQty, IssuedQty, unitMsr |
| `OITM` | Item Master | ItemCode, ItemName, OnHand (for stock check) |

### 3.2 SAP HANA — Status Codes (OWOR.Status)

| SAP Code | Meaning |
|----------|---------|
| `P` | Planned |
| `R` | Released |
| `L` | Closed |
| `C` | Cancelled |

We fetch only `Status IN ('P', 'R')` — i.e., Planned and Released orders.

### 3.3 SAP HANA — Query: Fetch Monthly Production Orders

```sql
SELECT
    T0."DocEntry"      AS doc_entry,
    T0."DocNum"        AS doc_num,
    T0."ItemCode"      AS item_code,
    T0."Dscription"    AS item_name,
    T0."PlannedQty"    AS planned_qty,
    T0."CmpltQty"      AS completed_qty,
    T0."RjctQty"       AS rejected_qty,
    (T0."PlannedQty" - T0."CmpltQty") AS remaining_qty,
    T0."PlannedDate"   AS planned_start_date,
    T0."DueDate"       AS due_date,
    T0."Status"        AS sap_status,
    T0."CardCode"      AS customer_code,
    IFNULL(T0."CardName", '') AS customer_name,
    T0."BPLId"         AS branch_id,
    IFNULL(T0."Comments", '') AS remarks
FROM "{schema}"."OWOR" T0
WHERE T0."Status" IN ('P', 'R')
  AND YEAR(T0."DueDate") = ?
  AND MONTH(T0."DueDate") = ?
ORDER BY T0."DueDate" ASC
```

**Parameters:** year (int), month (int)

---

### 3.4 SAP HANA — Query: Fetch BOM Components for a Production Order

```sql
SELECT
    T1."ItemCode"      AS component_code,
    T1."Dscription"    AS component_name,
    T1."PlannedQty"    AS planned_qty,
    T1."IssuedQty"     AS issued_qty,
    (T1."PlannedQty" - T1."IssuedQty") AS remaining_qty,
    T1."unitMsr"       AS uom
FROM "{schema}"."WOR1" T1
WHERE T1."DocEntry" = ?
ORDER BY T1."LineNum" ASC
```

**Parameter:** doc_entry (int) — the SAP production order DocEntry

---

### 3.5 SAP HANA — Query: Bulk Components for Multiple Orders

Used when importing a batch of production orders to pre-load all material requirements in one query.

```sql
SELECT
    T1."DocEntry"      AS doc_entry,
    T1."ItemCode"      AS component_code,
    T1."Dscription"    AS component_name,
    T1."PlannedQty"    AS planned_qty,
    T1."IssuedQty"     AS issued_qty,
    (T1."PlannedQty" - T1."IssuedQty") AS remaining_qty,
    T1."unitMsr"       AS uom
FROM "{schema}"."WOR1" T1
WHERE T1."DocEntry" IN (?, ?, ?, ...)
ORDER BY T1."DocEntry", T1."LineNum" ASC
```

---

### 3.6 SAP HANA Reader Class

**File:** `sap_client/hana/production_order_reader.py`

**Class:** `HanaProductionOrderReader`

**Methods:**

| Method | Parameters | Returns |
|--------|-----------|---------|
| `get_monthly_orders(year, month)` | year: int, month: int | `List[ProductionOrderDTO]` |
| `get_order_components(doc_entry)` | doc_entry: int | `List[ProductionComponentDTO]` |
| `get_bulk_components(doc_entries)` | doc_entries: list[int] | `Dict[int, List[ProductionComponentDTO]]` |

---

### 3.7 DTOs (Data Transfer Objects)

**File:** `sap_client/dtos.py` — Add these DTOs:

```python
@dataclass
class ProductionComponentDTO:
    component_code: str
    component_name: str
    planned_qty: float
    issued_qty: float
    remaining_qty: float
    uom: str

@dataclass
class ProductionOrderDTO:
    doc_entry: int
    doc_num: int
    item_code: str
    item_name: str
    planned_qty: float
    completed_qty: float
    rejected_qty: float
    remaining_qty: float
    planned_start_date: date
    due_date: date
    sap_status: str          # 'P', 'R'
    customer_code: str
    customer_name: str
    branch_id: int
    remarks: str
    components: List[ProductionComponentDTO] = field(default_factory=list)
```

---

## 4. Django App Structure

**App Name:** `production_planning`

```
production_planning/
    __init__.py
    apps.py
    admin.py
    models.py
    serializers.py
    views.py
    urls.py
    permissions.py
    services.py
    migrations/
        __init__.py
        0001_initial.py
    sap/
        __init__.py
        production_order_reader.py    # HANA queries
```

---

## 5. Database Models

### 5.1 `ProductionPlan` — Monthly plan (imported from SAP)

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField | Primary key |
| `company` | FK → Company | Which company this plan belongs to |
| `sap_doc_entry` | IntegerField | SAP OWOR DocEntry (unique per company) |
| `sap_doc_num` | IntegerField | SAP OWOR DocNum (human-readable) |
| `item_code` | CharField(50) | Finished product ItemCode |
| `item_name` | CharField(255) | Finished product name |
| `planned_qty` | DecimalField(12,3) | Total planned quantity from SAP |
| `completed_qty` | DecimalField(12,3) | Total produced (calculated from daily entries) |
| `target_start_date` | DateField | SAP PlannedDate |
| `due_date` | DateField | SAP DueDate |
| `sap_status` | CharField(2) | SAP status at time of import: P/R |
| `status` | CharField(20) | App status: OPEN/IN_PROGRESS/COMPLETED/CLOSED/CANCELLED |
| `customer_code` | CharField(50) | SAP CardCode (nullable) |
| `customer_name` | CharField(255) | SAP CardName (nullable) |
| `branch_id` | IntegerField | SAP BPLId |
| `remarks` | TextField | SAP Comments |
| `imported_by` | FK → User | Who imported this plan |
| `imported_at` | DateTimeField | When imported |
| `closed_by` | FK → User | Who closed the plan (nullable) |
| `closed_at` | DateTimeField | When closed (nullable) |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |

**unique_together:** `(company, sap_doc_entry)` — prevents duplicate import of same SAP order

---

### 5.2 `PlanMaterialRequirement` — BOM components per plan

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField | Primary key |
| `production_plan` | FK → ProductionPlan | Parent plan |
| `component_code` | CharField(50) | Raw material ItemCode |
| `component_name` | CharField(255) | Raw material name |
| `required_qty` | DecimalField(12,3) | Planned qty from SAP WOR1 |
| `issued_qty` | DecimalField(12,3) | Already issued in SAP at import time |
| `uom` | CharField(20) | Unit of measure |

---

### 5.3 `WeeklyPlan` — Planner breaks monthly into weeks

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField | Primary key |
| `production_plan` | FK → ProductionPlan | Parent monthly plan |
| `week_number` | PositiveSmallIntegerField | 1, 2, 3, 4 (or 5 if month spans 5 weeks) |
| `week_label` | CharField(50) | e.g., "Week 1 (Mar 1–7)" |
| `start_date` | DateField | Week start date |
| `end_date` | DateField | Week end date |
| `target_qty` | DecimalField(12,3) | Planned production for this week |
| `produced_qty` | DecimalField(12,3) | Actual produced (computed from daily entries) |
| `status` | CharField(20) | PENDING / IN_PROGRESS / COMPLETED |
| `created_by` | FK → User | Who created this weekly target |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |

**unique_together:** `(production_plan, week_number)`

**Constraint:** Sum of all weekly `target_qty` must not exceed `production_plan.planned_qty`

---

### 5.4 `DailyProductionEntry` — Production team's daily log

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField | Primary key |
| `weekly_plan` | FK → WeeklyPlan | Which weekly plan this belongs to |
| `production_date` | DateField | The date of production |
| `produced_qty` | DecimalField(12,3) | Quantity produced today |
| `remarks` | TextField(blank=True) | Notes from production team |
| `shift` | CharField(20) | MORNING / AFTERNOON / NIGHT (optional) |
| `recorded_by` | FK → User | Who entered this |
| `created_at` | DateTimeField | auto_now_add |
| `updated_at` | DateTimeField | auto_now |

**unique_together:** `(weekly_plan, production_date, shift)` — prevents double entry for same shift/day

---

## 6. Status Lifecycle

### ProductionPlan Status

```
OPEN
  |-- (planner creates first weekly plan) --> IN_PROGRESS
  |-- (planner imports but no weekly plan yet) --> stays OPEN

IN_PROGRESS
  |-- (all daily entries complete, planner closes) --> COMPLETED
  |-- (plan cancelled by manager) --> CANCELLED

COMPLETED
  |-- (manager confirms and locks) --> CLOSED
```

### WeeklyPlan Status

```
PENDING  --> (first daily entry added) --> IN_PROGRESS --> (week end date passed or target met) --> COMPLETED
```

---

## 7. API Endpoints

**Base URL:** `/api/v1/production-planning/`

### 7.1 SAP Data — Fetch from SAP HANA (not saved yet)

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| GET | `/sap-orders/` | Fetch production orders from SAP HANA for a month | `can_fetch_sap_orders` |

**Query Params:** `?month=2026-03` (YYYY-MM format)

**Response:** List of SAP production orders with their BOM components. Not saved to DB.

---

### 7.2 Production Plan (Monthly)

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| POST | `/import/` | Import selected SAP orders as local plans | `can_import_production_plan` |
| GET | `/` | List all production plans (filterable by status, month) | `can_view_production_plan` |
| GET | `/<plan_id>/` | Detail of a single plan (with weekly breakdown + progress) | `can_view_production_plan` |
| POST | `/<plan_id>/close/` | Close/complete a production plan | `can_close_production_plan` |
| GET | `/<plan_id>/materials/` | List material requirements for a plan | `can_view_production_plan` |

---

### 7.3 Weekly Plans

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| GET | `/<plan_id>/weekly-plans/` | List all weekly plans for a production plan | `can_view_production_plan` |
| POST | `/<plan_id>/weekly-plans/` | Create a weekly plan (target) | `can_manage_weekly_plan` |
| PATCH | `/<plan_id>/weekly-plans/<week_id>/` | Update weekly target or dates | `can_manage_weekly_plan` |
| DELETE | `/<plan_id>/weekly-plans/<week_id>/` | Delete a weekly plan (only if no daily entries) | `can_manage_weekly_plan` |

---

### 7.4 Daily Production Entries

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| GET | `/weekly-plans/<week_id>/daily-entries/` | List all daily entries for a week | `can_view_daily_production` |
| POST | `/weekly-plans/<week_id>/daily-entries/` | Add today's production entry | `can_add_daily_production` |
| PATCH | `/weekly-plans/<week_id>/daily-entries/<entry_id>/` | Edit an entry (within same day only) | `can_add_daily_production` |
| GET | `/daily-entries/` | List all daily entries (filterable by date range, plan) | `can_view_daily_production` |

---

### 7.5 Dashboard / Summary

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| GET | `/summary/` | Monthly summary: total plans, produced vs target, open plans | `can_view_production_plan` |

---

## 8. Request / Response Samples

### 8.1 GET `/sap-orders/?month=2026-03`

**Response 200:**
```json
[
  {
    "doc_entry": 1234,
    "doc_num": 50001,
    "item_code": "FG-OIL-1L",
    "item_name": "Jivo Sunflower Oil 1L",
    "planned_qty": "50000.000",
    "completed_qty": "0.000",
    "remaining_qty": "50000.000",
    "planned_start_date": "2026-03-01",
    "due_date": "2026-03-31",
    "sap_status": "R",
    "sap_status_label": "Released",
    "customer_code": "C10001",
    "customer_name": "Retail Distribution",
    "branch_id": 1,
    "remarks": "March production batch",
    "components": [
      {
        "component_code": "RM-SUNFLOWER-SEEDS",
        "component_name": "Sunflower Seeds (Grade A)",
        "required_qty": "55000.000",
        "issued_qty": "0.000",
        "remaining_qty": "55000.000",
        "uom": "KG"
      },
      {
        "component_code": "RM-BOTTLE-1L",
        "component_name": "PET Bottle 1L",
        "required_qty": "50000.000",
        "issued_qty": "0.000",
        "remaining_qty": "50000.000",
        "uom": "PCS"
      }
    ],
    "already_imported": false
  }
]
```

**Notes:**
- `already_imported: true` means this SAP order is already in the local DB — prevents double import
- Components list comes from WOR1 (BOM components)

---

### 8.2 POST `/import/`

**Request:**
```json
{
  "orders": [
    {"doc_entry": 1234},
    {"doc_entry": 1235}
  ]
}
```

**Response 201:**
```json
{
  "imported": 2,
  "skipped": 0,
  "plans": [
    {
      "id": 10,
      "sap_doc_num": 50001,
      "item_code": "FG-OIL-1L",
      "item_name": "Jivo Sunflower Oil 1L",
      "planned_qty": "50000.000",
      "due_date": "2026-03-31",
      "status": "OPEN"
    }
  ]
}
```

---

### 8.3 GET `/<plan_id>/` — Plan Detail with Progress

**Response 200:**
```json
{
  "id": 10,
  "sap_doc_entry": 1234,
  "sap_doc_num": 50001,
  "item_code": "FG-OIL-1L",
  "item_name": "Jivo Sunflower Oil 1L",
  "planned_qty": "50000.000",
  "completed_qty": "18500.000",
  "progress_percent": 37.0,
  "target_start_date": "2026-03-01",
  "due_date": "2026-03-31",
  "status": "IN_PROGRESS",
  "weekly_plans": [
    {
      "id": 1,
      "week_number": 1,
      "week_label": "Week 1 (Mar 1–7)",
      "start_date": "2026-03-01",
      "end_date": "2026-03-07",
      "target_qty": "12500.000",
      "produced_qty": "12500.000",
      "progress_percent": 100.0,
      "status": "COMPLETED"
    },
    {
      "id": 2,
      "week_number": 2,
      "week_label": "Week 2 (Mar 8–14)",
      "start_date": "2026-03-08",
      "end_date": "2026-03-14",
      "target_qty": "12500.000",
      "produced_qty": "6000.000",
      "progress_percent": 48.0,
      "status": "IN_PROGRESS"
    }
  ],
  "materials": [
    {
      "component_code": "RM-SUNFLOWER-SEEDS",
      "component_name": "Sunflower Seeds (Grade A)",
      "required_qty": "55000.000",
      "uom": "KG"
    }
  ]
}
```

---

### 8.4 POST `/<plan_id>/weekly-plans/`

**Request:**
```json
{
  "week_number": 1,
  "week_label": "Week 1 (Mar 1–7)",
  "start_date": "2026-03-01",
  "end_date": "2026-03-07",
  "target_qty": 12500
}
```

**Response 201:**
```json
{
  "id": 1,
  "week_number": 1,
  "week_label": "Week 1 (Mar 1–7)",
  "start_date": "2026-03-01",
  "end_date": "2026-03-07",
  "target_qty": "12500.000",
  "produced_qty": "0.000",
  "progress_percent": 0.0,
  "status": "PENDING"
}
```

**Validation:**
- `target_qty` must be > 0
- Sum of all weekly targets must not exceed `production_plan.planned_qty`
- `start_date` and `end_date` must fall within the plan's `target_start_date` → `due_date`
- `week_number` must be unique per plan
- `end_date` must be >= `start_date`

---

### 8.5 POST `/weekly-plans/<week_id>/daily-entries/`

**Request:**
```json
{
  "production_date": "2026-03-03",
  "produced_qty": 2100,
  "shift": "MORNING",
  "remarks": "Line 2 maintenance caused 30 min delay"
}
```

**Response 201:**
```json
{
  "id": 5,
  "production_date": "2026-03-03",
  "produced_qty": "2100.000",
  "shift": "MORNING",
  "remarks": "Line 2 maintenance caused 30 min delay",
  "recorded_by": 7,
  "weekly_plan_progress": {
    "week_target": "12500.000",
    "produced_so_far": "6100.000",
    "remaining": "6400.000",
    "progress_percent": 48.8
  },
  "plan_progress": {
    "plan_target": "50000.000",
    "produced_so_far": "18600.000",
    "progress_percent": 37.2
  }
}
```

**Validation:**
- `production_date` must fall within `weekly_plan.start_date` → `weekly_plan.end_date`
- `production_date` cannot be in the future (today at most)
- `produced_qty` must be > 0
- Duplicate check: `(weekly_plan, production_date, shift)` — cannot enter twice for same shift

---

### 8.6 POST `/<plan_id>/close/`

**Request:** (no body needed)

**Response 200:**
```json
{
  "success": true,
  "plan_id": 10,
  "status": "COMPLETED",
  "total_produced": "50000.000",
  "planned_qty": "50000.000",
  "message": "Production plan closed. Total produced: 50000 units."
}
```

**Validation:**
- Plan must be in `OPEN` or `IN_PROGRESS` status
- Plan cannot be closed if `status` is already `CLOSED` or `CANCELLED`

---

## 9. Permissions

| Permission Code | Description | Role |
|----------------|-------------|------|
| `can_fetch_sap_orders` | Fetch production orders from SAP HANA | Planner, Manager |
| `can_import_production_plan` | Import SAP orders into local DB | Planner, Manager |
| `can_view_production_plan` | View plans, progress, materials | Planner, Manager, Production Supervisor |
| `can_manage_weekly_plan` | Create/edit weekly plan targets | Planner, Manager |
| `can_add_daily_production` | Add daily production entries | Production Team, Supervisor |
| `can_view_daily_production` | View daily production entries | All roles |
| `can_close_production_plan` | Close/complete a production plan | Manager, Planner |

Django Group: `production_planning` — assigned to relevant users.

---

## 10. Key Business Rules

1. **One SAP order → One ProductionPlan** — Uniqueness enforced by `(company, sap_doc_entry)`.
2. **Weekly targets are optional** — Planners can import a plan and add weekly targets later. Plan stays `OPEN` until first weekly plan is created.
3. **Daily entries drive progress** — `WeeklyPlan.produced_qty` and `ProductionPlan.completed_qty` are derived from sum of `DailyProductionEntry.produced_qty`. Never stored redundantly — always computed fresh or cached via `save()` signal.
4. **Cannot exceed monthly target** — Sum of all weekly `target_qty` for a plan cannot exceed `planned_qty`.
5. **Daily entry date restriction** — A daily entry's `production_date` must be within its weekly plan's date range.
6. **No future-dated entries** — `production_date` cannot be tomorrow or later.
7. **Close is manual** — The plan does not auto-close even if `completed_qty >= planned_qty`. A planner/manager must explicitly close it.
8. **Closed plan is locked** — No new weekly plans or daily entries can be added to a `CLOSED` plan.
9. **Shift is optional** — If the factory runs a single shift, `shift` can be omitted and the unique constraint is `(weekly_plan, production_date)`.
10. **Material requirements are a snapshot** — Copied from SAP WOR1 at import time. If SAP BOM changes later, local records are not auto-updated (manual re-import needed).

---

## 11. `sap_client` Changes Required

### New File: `sap_client/hana/production_order_reader.py`

New HANA reader class following the same pattern as `HanaPOReader`:
- `get_monthly_orders(year, month)` — Query OWOR for a given month
- `get_order_components(doc_entry)` — Query WOR1 for one production order
- `get_bulk_components(doc_entries)` — Query WOR1 for multiple orders in one query

### New DTOs in `sap_client/dtos.py`

Add `ProductionOrderDTO` and `ProductionComponentDTO` dataclasses.

---

## 12. `config/settings.py` Change

Add `'production_planning'` to `INSTALLED_APPS`.

---

## 13. `config/urls.py` Change

Add:
```python
path("api/v1/production-planning/", include("production_planning.urls")),
```

---

## 14. Migration Plan

| Migration | Description |
|-----------|-------------|
| `0001_initial.py` | Creates ProductionPlan, PlanMaterialRequirement, WeeklyPlan, DailyProductionEntry |
| `0002_add_custom_permissions.py` | Adds all 7 custom permissions |
| `0003_create_production_planning_group.py` | Creates Django group + assigns permissions |

---

## 15. Implementation Order

Build in this sequence to minimize rework:

```
Step 1: sap_client/hana/production_order_reader.py  (HANA queries)
Step 2: sap_client/dtos.py  (add new DTOs)
Step 3: production_planning/models.py  (all 4 models)
Step 4: production_planning/migrations/  (0001 + 0002 + 0003)
Step 5: production_planning/serializers.py
Step 6: production_planning/services.py  (import logic, progress computation)
Step 7: production_planning/permissions.py
Step 8: production_planning/views.py
Step 9: production_planning/urls.py
Step 10: config/settings.py + config/urls.py  (register app)
Step 11: production_planning/admin.py
```

---

## 16. Summary of All Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `production_planning/__init__.py` | App init |
| `production_planning/apps.py` | App config |
| `production_planning/models.py` | 4 models |
| `production_planning/serializers.py` | Request/response serializers |
| `production_planning/views.py` | All API views |
| `production_planning/urls.py` | URL patterns |
| `production_planning/services.py` | Business logic (import, progress) |
| `production_planning/permissions.py` | Custom permission classes |
| `production_planning/admin.py` | Django admin registration |
| `production_planning/migrations/0001_initial.py` | DB schema |
| `production_planning/migrations/0002_add_custom_permissions.py` | Permissions |
| `production_planning/migrations/0003_create_production_planning_group.py` | Group |
| `sap_client/hana/production_order_reader.py` | SAP HANA queries |

### Modified Files

| File | Change |
|------|--------|
| `sap_client/dtos.py` | Add `ProductionOrderDTO`, `ProductionComponentDTO` |
| `config/settings.py` | Add `'production_planning'` to INSTALLED_APPS |
| `config/urls.py` | Add production-planning URL include |

---

## 17. Open Questions (To Confirm Before Implementation)

1. **Company filter** — Should plans be scoped per company (using `Company-Code` header like GRPO)? Assumed YES.
2. **SAP write-back** — When plan is closed, should we update SAP OWOR status to 'Closed'? Assumed NO for now (read-only SAP integration for this module).
3. **Shift field** — Is shift tracking required? If the factory runs a single shift, the shift field can be removed from `DailyProductionEntry`.
4. **Multiple products per SAP order** — OWOR has one ItemCode per production order. If head office creates one order per product, the current design handles it correctly.
5. **Week numbering** — Should week numbers be calendar-based (ISO week) or plan-relative (1, 2, 3, 4)? Current design uses plan-relative for flexibility.
6. **Notifications** — Should managers receive a notification when a plan's `completed_qty` reaches 100% of `planned_qty`? The project has a `notifications` app.
