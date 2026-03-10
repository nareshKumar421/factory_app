# Production Planning — API Reference

**Base URL:** `/api/v1/production-planning/`

**Required Headers (all endpoints):**
```
Authorization: Token <token>
Company-Code: <company_code>
```

---

## Endpoint Index

| Method | Endpoint | Description | Permission |
|--------|----------|-------------|-----------|
| GET | `/dropdown/items/` | Items from SAP HANA for dropdown | `can_view_production_plan` |
| GET | `/dropdown/uom/` | Units of measure from SAP HANA | `can_view_production_plan` |
| GET | `/dropdown/warehouses/` | Active warehouses from SAP HANA | `can_view_production_plan` |
| GET | `/` | List all production plans | `can_view_production_plan` |
| POST | `/` | Create a new plan (DRAFT) | `can_create_production_plan` |
| GET | `/summary/` | Monthly summary & progress | `can_view_production_plan` |
| GET | `/<plan_id>/` | Plan detail with weekly breakdown | `can_view_production_plan` |
| PATCH | `/<plan_id>/` | Update plan (DRAFT only) | `can_edit_production_plan` |
| DELETE | `/<plan_id>/` | Delete plan (DRAFT only) | `can_delete_production_plan` |
| POST | `/<plan_id>/post-to-sap/` | Post plan to SAP as Production Order | `can_post_plan_to_sap` |
| POST | `/<plan_id>/close/` | Close/complete a plan | `can_close_production_plan` |
| GET | `/<plan_id>/materials/` | List material requirements | `can_view_production_plan` |
| POST | `/<plan_id>/materials/` | Add a material | `can_edit_production_plan` |
| DELETE | `/<plan_id>/materials/<material_id>/` | Remove a material (DRAFT only) | `can_edit_production_plan` |
| GET | `/<plan_id>/weekly-plans/` | List weekly plans | `can_view_production_plan` |
| POST | `/<plan_id>/weekly-plans/` | Create a weekly plan | `can_manage_weekly_plan` |
| PATCH | `/<plan_id>/weekly-plans/<week_id>/` | Update weekly plan | `can_manage_weekly_plan` |
| DELETE | `/<plan_id>/weekly-plans/<week_id>/` | Delete weekly plan | `can_manage_weekly_plan` |
| GET | `/weekly-plans/<week_id>/daily-entries/` | List daily entries for a week | `can_view_daily_production` |
| POST | `/weekly-plans/<week_id>/daily-entries/` | Add daily entry | `can_add_daily_production` |
| PATCH | `/weekly-plans/<week_id>/daily-entries/<entry_id>/` | Edit a daily entry | `can_add_daily_production` |
| GET | `/daily-entries/` | All daily entries (cross-plan) | `can_view_daily_production` |

---

## Dropdown APIs (SAP HANA)

### GET `/dropdown/items/`

Fetch items from SAP HANA for dropdown selection (reads OITM table).

**Query Params**

| Param | Description |
|-------|-------------|
| `type` | `finished` (MakeItem=Y), `raw` (PrchseItem=Y), or omit for all |
| `search` | Search by ItemCode or ItemName (case-insensitive, partial match) |

**Request**
```
GET /api/v1/production-planning/dropdown/items/?type=finished&search=oil
```

**Response 200**
```json
[
  {
    "item_code": "FG-OIL-1L",
    "item_name": "Jivo Sunflower Oil 1L",
    "uom": "LTR",
    "item_group": "Finished Goods",
    "make_item": true,
    "purchase_item": false
  }
]
```

---

### GET `/dropdown/uom/`

Fetch units of measure from SAP HANA (reads OUOM table).

**Response 200**
```json
[
  { "uom_code": "LTR", "uom_name": "Litre" },
  { "uom_code": "KG", "uom_name": "Kilogram" }
]
```

---

### GET `/dropdown/warehouses/`

Fetch active warehouses from SAP HANA (reads OWHS table).

**Response 200**
```json
[
  { "warehouse_code": "WH-MAIN", "warehouse_name": "Main Warehouse" }
]
```

---

## Plan CRUD

### GET `/`

List all production plans.

**Query Params**

| Param | Description |
|-------|-------------|
| `status` | `DRAFT`, `OPEN`, `IN_PROGRESS`, `COMPLETED`, `CLOSED`, `CANCELLED` |
| `month` | Filter by due_date month: `YYYY-MM` |

**Response 200**
```json
[
  {
    "id": 1,
    "item_code": "FG-OIL-1L",
    "item_name": "Jivo Sunflower Oil 1L",
    "uom": "LTR",
    "warehouse_code": "WH-MAIN",
    "planned_qty": "50000.000",
    "completed_qty": "18500.000",
    "progress_percent": 37.0,
    "target_start_date": "2026-03-01",
    "due_date": "2026-03-31",
    "status": "IN_PROGRESS",
    "sap_posting_status": "POSTED",
    "sap_doc_num": 10042,
    "sap_error_message": null,
    "created_by": 3,
    "created_at": "2026-03-06T09:00:00Z"
  }
]
```

---

### POST `/`

Create a new production plan (saved as DRAFT, not yet sent to SAP).

**Request**
```json
{
  "item_code": "FG-OIL-1L",
  "item_name": "Jivo Sunflower Oil 1L",
  "uom": "LTR",
  "warehouse_code": "WH-MAIN",
  "planned_qty": 50000,
  "target_start_date": "2026-03-01",
  "due_date": "2026-03-31",
  "branch_id": 1,
  "remarks": "March production batch",
  "materials": [
    {
      "component_code": "RM-SEEDS",
      "component_name": "Sunflower Seeds",
      "required_qty": 55000,
      "uom": "KG",
      "warehouse_code": "WH-MAIN"
    }
  ]
}
```

**Field Reference**

| Field | Required | Description |
|-------|----------|-------------|
| `item_code` | Yes | Finished product code from dropdown |
| `item_name` | Yes | Finished product name |
| `uom` | No | Unit of measure |
| `warehouse_code` | No | Target warehouse |
| `planned_qty` | Yes | Total planned quantity (> 0) |
| `target_start_date` | Yes | Planned start date |
| `due_date` | Yes | Must be ≥ `target_start_date` |
| `branch_id` | No | SAP branch/business place ID |
| `remarks` | No | Free-text notes |
| `materials` | No | BOM components (see below) |

**Validation**
- `due_date` must be on or after `target_start_date`

**Response 201** — Full plan detail (same as GET `/<plan_id>/`)

---

### GET `/<plan_id>/`

Full plan detail including materials and weekly plans.

**Response 200**
```json
{
  "id": 1,
  "item_code": "FG-OIL-1L",
  "item_name": "Jivo Sunflower Oil 1L",
  "uom": "LTR",
  "warehouse_code": "WH-MAIN",
  "planned_qty": "50000.000",
  "completed_qty": "18500.000",
  "progress_percent": 37.0,
  "target_start_date": "2026-03-01",
  "due_date": "2026-03-31",
  "status": "IN_PROGRESS",
  "sap_posting_status": "POSTED",
  "sap_doc_entry": 42,
  "sap_doc_num": 10042,
  "sap_status": "R",
  "sap_error_message": null,
  "branch_id": 1,
  "remarks": "March batch",
  "created_by": 3,
  "created_at": "2026-03-06T09:00:00Z",
  "closed_by": null,
  "closed_at": null,
  "materials": [
    {
      "id": 1,
      "component_code": "RM-SEEDS",
      "component_name": "Sunflower Seeds",
      "required_qty": "55000.000",
      "uom": "KG",
      "warehouse_code": "WH-MAIN"
    }
  ],
  "weekly_plans": [
    {
      "id": 1,
      "week_number": 1,
      "week_label": "Week 1",
      "start_date": "2026-03-01",
      "end_date": "2026-03-07",
      "target_qty": "12500.000",
      "produced_qty": "12500.000",
      "progress_percent": 100.0,
      "status": "COMPLETED",
      "created_by": 3,
      "created_at": "2026-03-06T09:00:00Z"
    }
  ]
}
```

---

### PATCH `/<plan_id>/`

Update a plan. **Only DRAFT plans can be edited.**

**Request** (send only fields to change)
```json
{
  "planned_qty": 48000,
  "remarks": "Revised down"
}
```

**Response 200** — Updated plan detail.

**Error**
```json
{ "detail": "Only DRAFT plans can be edited. Current status: 'OPEN'." }  // 400
```

---

### DELETE `/<plan_id>/`

Delete a plan. **Only DRAFT plans can be deleted.**

**Response 204** — No content.

**Error**
```json
{ "detail": "Only DRAFT plans can be deleted. Current status: 'OPEN'." }  // 400
```

---

### POST `/<plan_id>/post-to-sap/`

Post a DRAFT or OPEN plan to SAP B1 as a Production Order (`POST /b1s/v2/ProductionOrders`).

On success, the plan's `sap_doc_entry`, `sap_doc_num`, and `sap_status` are populated, `sap_posting_status` becomes `POSTED`, and `status` becomes `OPEN`.

On failure, `sap_posting_status` becomes `FAILED` and `sap_error_message` is set.

**No request body needed.**

**Response 200**
```json
{
  "success": true,
  "plan_id": 1,
  "sap_doc_entry": 42,
  "sap_doc_num": 10042,
  "sap_status": "R",
  "message": "Plan posted to SAP successfully. Production Order: 10042"
}
```

**Error Responses**
```json
{ "detail": "Plan already posted to SAP. DocNum: 10042" }              // 400
{ "detail": "SAP validation error: <message>" }                        // 400
{ "detail": "SAP system is currently unavailable. Please try again." } // 503
{ "detail": "SAP error: <message>" }                                   // 502
```

---

### POST `/<plan_id>/close/`

Close a plan. Recomputes final `completed_qty` before closing. Sets status to `COMPLETED`.

**No request body needed.**

**Response 200**
```json
{
  "success": true,
  "plan_id": 1,
  "status": "COMPLETED",
  "total_produced": "50000.000",
  "planned_qty": "50000.000",
  "message": "Production plan closed. Total produced: 50000.000 units."
}
```

**Error**
```json
{ "detail": "Cannot close a plan with status 'CLOSED'." }  // 400
```

---

## Materials

### GET `/<plan_id>/materials/`

List material requirements for a plan.

**Response 200**
```json
[
  {
    "id": 1,
    "component_code": "RM-SEEDS",
    "component_name": "Sunflower Seeds",
    "required_qty": "55000.000",
    "uom": "KG",
    "warehouse_code": "WH-MAIN"
  }
]
```

---

### POST `/<plan_id>/materials/`

Add a material requirement. Allowed on DRAFT, OPEN, and IN_PROGRESS plans.

**Request**
```json
{
  "component_code": "RM-SEEDS",
  "component_name": "Sunflower Seeds",
  "required_qty": 55000,
  "uom": "KG",
  "warehouse_code": "WH-MAIN"
}
```

**Response 201** — Material object.

---

### DELETE `/<plan_id>/materials/<material_id>/`

Remove a material. **Only DRAFT plans.**

**Response 204** — No content.

---

## Summary

### GET `/summary/`

Monthly summary of all plans.

**Query Params**

| Param | Description |
|-------|-------------|
| `month` | Filter by month: `YYYY-MM` |

**Response 200**
```json
{
  "total_plans": 5,
  "total_planned_qty": 250000.0,
  "total_produced_qty": 95000.0,
  "overall_progress_percent": 38.0,
  "status_breakdown": {
    "DRAFT": 0,
    "OPEN": 1,
    "IN_PROGRESS": 3,
    "COMPLETED": 1,
    "CLOSED": 0,
    "CANCELLED": 0
  },
  "sap_posting_breakdown": {
    "NOT_POSTED": 0,
    "POSTED": 5,
    "FAILED": 0
  }
}
```

---

## Weekly Plans

### GET `/<plan_id>/weekly-plans/`

List weekly plans for a production plan.

**Response 200** — Array of weekly plan objects.

---

### POST `/<plan_id>/weekly-plans/`

Create a weekly target. **Plan must be OPEN or IN_PROGRESS (posted to SAP first).**

**Request**
```json
{
  "week_number": 1,
  "week_label": "Week 1",
  "start_date": "2026-03-01",
  "end_date": "2026-03-07",
  "target_qty": 12500
}
```

**Validation**
- `end_date` ≥ `start_date`
- Both dates within plan's `target_start_date` → `due_date`
- Sum of all weekly `target_qty` must not exceed `planned_qty`
- `week_number` must be unique per plan
- Plan must not be `DRAFT`, `CLOSED`, or `CANCELLED`

**Response 201**
```json
{
  "id": 1,
  "week_number": 1,
  "week_label": "Week 1",
  "start_date": "2026-03-01",
  "end_date": "2026-03-07",
  "target_qty": "12500.000",
  "produced_qty": "0.000",
  "progress_percent": 0.0,
  "status": "PENDING",
  "created_by": 3,
  "created_at": "2026-03-06T09:00:00Z"
}
```

**Side effect:** If plan status was `OPEN`, it transitions to `IN_PROGRESS`.

---

### PATCH `/<plan_id>/weekly-plans/<week_id>/`

Update `week_label` or `target_qty`.

**Request** (partial)
```json
{ "week_label": "Week 1 — revised", "target_qty": 11000 }
```

**Response 200** — Updated weekly plan object.

---

### DELETE `/<plan_id>/weekly-plans/<week_id>/`

Delete a weekly plan. Only allowed if it has no daily entries.

**Response 204** — No content.

**Error**
```json
{ "detail": "Cannot delete a weekly plan that has daily production entries." }  // 400
```

---

## Daily Entries

### GET `/weekly-plans/<week_id>/daily-entries/`

List daily entries for a weekly plan.

**Response 200**
```json
[
  {
    "id": 5,
    "production_date": "2026-03-03",
    "produced_qty": "2100.000",
    "shift": "MORNING",
    "remarks": "Normal run",
    "recorded_by": 7,
    "created_at": "2026-03-03T18:00:00Z"
  }
]
```

---

### POST `/weekly-plans/<week_id>/daily-entries/`

Add a daily production entry.

**Request**
```json
{
  "production_date": "2026-03-03",
  "produced_qty": 2100,
  "shift": "MORNING",
  "remarks": "Line 2 had 30 min delay"
}
```

**Field Reference**

| Field | Required | Description |
|-------|----------|-------------|
| `production_date` | Yes | `YYYY-MM-DD` |
| `produced_qty` | Yes | Must be > 0 |
| `shift` | No | `MORNING`, `AFTERNOON`, `NIGHT` |
| `remarks` | No | Free text |

**Validation**
- `production_date` must be within the weekly plan's date range
- `production_date` cannot be in the future
- `(weekly_plan, production_date, shift)` must be unique
- Plan must not be `CLOSED` or `CANCELLED`

**Response 201**
```json
{
  "id": 5,
  "production_date": "2026-03-03",
  "produced_qty": "2100.000",
  "shift": "MORNING",
  "remarks": "Line 2 had 30 min delay",
  "recorded_by": 7,
  "created_at": "2026-03-03T18:00:00Z",
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

---

### PATCH `/weekly-plans/<week_id>/daily-entries/<entry_id>/`

Edit an existing entry's `produced_qty` or `remarks`.

**Request** (partial)
```json
{ "produced_qty": 2200, "remarks": "Corrected quantity" }
```

**Response 200** — Updated entry object.

---

### GET `/daily-entries/`

All daily entries across all plans.

**Query Params**

| Param | Description |
|-------|-------------|
| `plan_id` | Filter by production plan |
| `date_from` | From date inclusive: `YYYY-MM-DD` |
| `date_to` | To date inclusive: `YYYY-MM-DD` |

**Response 200** — Array of daily entry objects.

---

## Error Reference

| HTTP Code | Condition |
|-----------|-----------|
| 400 | Validation error, business rule violation |
| 401 | Missing or invalid auth token |
| 403 | Missing `Company-Code` header or insufficient permissions |
| 404 | Resource not found |
| 502 | SAP HANA returned a data error |
| 503 | SAP HANA connection unavailable |

---

## SAP Integration Reference

### Tables Read (HANA)

| Table | Used For |
|-------|---------|
| `OITM` | Item dropdown (finished goods / raw materials) |
| `OUOM` | Unit of measure dropdown |
| `OWHS` | Warehouse dropdown |

### SAP Service Layer (Write)

| Endpoint | Used For |
|----------|---------|
| `POST /b1s/v2/ProductionOrders` | Create production order from local plan |

### SAP Payload (ProductionOrder)

```json
{
  "ItemNo": "FG-OIL-1L",
  "PlannedQuantity": 50000.0,
  "PlannedStartDate": "2026-03-01",
  "DueDate": "2026-03-31",
  "Status": "R",
  "Warehouse": "WH-MAIN",
  "BranchID": 1,
  "Remarks": "March batch",
  "ProductionOrderLines": [
    {
      "ItemNo": "RM-SEEDS",
      "PlannedQuantity": 55000.0,
      "Warehouse": "WH-MAIN"
    }
  ]
}
```

---

## End-to-End Flow

```
[Planner opens form]
        |
        | GET /dropdown/items/?type=finished
        | GET /dropdown/uom/
        | GET /dropdown/warehouses/
        v
[Fill plan requirements in Sampooran]
        |
        | POST /                    → status: DRAFT, sap_posting_status: NOT_POSTED
        v
[Review & confirm]
        |
        | POST /<plan_id>/post-to-sap/
        |   → SAP Service Layer: POST /b1s/v2/ProductionOrders
        |   → status: OPEN, sap_posting_status: POSTED, sap_doc_num assigned
        v
[Planner creates weekly breakdown]
        |
        | POST /<plan_id>/weekly-plans/   (Week 1, 2, 3, 4)
        |   → status transitions: OPEN → IN_PROGRESS
        v
[Production team records daily output]
        |
        | POST /weekly-plans/<week_id>/daily-entries/
        |   → WeeklyPlan.produced_qty and ProductionPlan.completed_qty auto-updated
        v
[Manager closes plan]
        |
        | POST /<plan_id>/close/
        |   → status: COMPLETED (locked)
```
