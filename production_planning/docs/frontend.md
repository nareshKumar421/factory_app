# Production Planning — Frontend Developer Guide

> **Module:** Production Planning
> **Base URL:** `/api/v1/production-planning/`
> **Auth:** Token-based (`Authorization: Token <token>`)
> **Company header required on every request:** `Company-Code: <code>`

---

## Table of Contents

1. [Authentication & Headers](#1-authentication--headers)
2. [Enum / Constant Values](#2-enum--constant-values)
3. [Plan Lifecycle (State Machine)](#3-plan-lifecycle-state-machine)
4. [Error Response Format](#4-error-response-format)
5. [Dropdown APIs (form field data)](#5-dropdown-apis) — Items, UoM, Warehouses, **BOM Auto-Detect**
6. [Production Plan — CRUD](#6-production-plan--crud)
7. [Post Plan to SAP](#7-post-plan-to-sap)
8. [Close Plan](#8-close-plan)
9. [Summary / Dashboard](#9-summary--dashboard)
10. [Material Requirements](#10-material-requirements)
11. [Weekly Plans](#11-weekly-plans)
12. [Daily Production Entries](#12-daily-production-entries)
13. [UI Logic Guide](#13-ui-logic-guide)
14. [Status Code Reference](#14-status-code-reference)

---

## 1. Authentication & Headers

Every API call must include these two headers:

```
Authorization: Token <user_token>
Company-Code: <company_code>
Content-Type: application/json     ← for POST / PATCH requests
```

**How to get the token:** Login API (handled by core auth module — outside this module's scope).

**Missing/wrong token → `401 Unauthorized`**
**Missing Company-Code header → `403 Forbidden`**
**No permission for the action → `403 Forbidden`**

---

## 2. Enum / Constant Values

Use these exact string values when sending or comparing status fields.

### Plan Status (`status`)

| Value | Label | Description |
|-------|-------|-------------|
| `DRAFT` | Draft | Created locally, not sent to SAP yet |
| `OPEN` | Open | Posted to SAP, no weekly plans yet |
| `IN_PROGRESS` | In Progress | Weekly plans created, production running |
| `COMPLETED` | Completed | Manager closed the plan |
| `CLOSED` | Closed | Fully confirmed and locked |
| `CANCELLED` | Cancelled | Cancelled |

### SAP Posting Status (`sap_posting_status`)

| Value | Label | Description |
|-------|-------|-------------|
| `NOT_POSTED` | Not Posted | Plan not sent to SAP yet |
| `POSTED` | Posted | Successfully in SAP (has `sap_doc_num`) |
| `FAILED` | Failed | SAP returned an error (see `sap_error_message`) |

### Weekly Plan Status (`status` on weekly plan)

| Value | Label | Description |
|-------|-------|-------------|
| `PENDING` | Pending | No daily entries yet |
| `IN_PROGRESS` | In Progress | Has entries but not yet complete |
| `COMPLETED` | Completed | `produced_qty` ≥ `target_qty` |

### Shift (`shift` on daily entry) — optional field

| Value | Label |
|-------|-------|
| `MORNING` | Morning |
| `AFTERNOON` | Afternoon |
| `NIGHT` | Night |
| `null` | Not specified |

---

## 3. Plan Lifecycle (State Machine)

```
         CREATE
            │
            ▼
         [DRAFT]  ──── PATCH / DELETE allowed
            │
     POST to SAP
            │
            ▼
          [OPEN]  ──── Add weekly plans here
            │
    First weekly plan
            │
            ▼
      [IN_PROGRESS]  ──── Daily entries recorded here
            │
      Manager closes
            │
            ▼
        [COMPLETED]  ──── Locked (no edits)
```

**Key rules for the UI:**
- Only show **Edit / Delete** plan buttons when `status === 'DRAFT'`
- Only show **Post to SAP** button when `sap_posting_status !== 'POSTED'` and `status` is `DRAFT` or `OPEN`
- Only show **Add Weekly Plan** when `status === 'OPEN' || status === 'IN_PROGRESS'`
- Only show **Add Daily Entry** when `status !== 'CLOSED' && status !== 'CANCELLED' && status !== 'COMPLETED'`
- Only show **Close Plan** button when `status !== 'CLOSED' && status !== 'CANCELLED' && status !== 'COMPLETED'`
- Show SAP error banner when `sap_posting_status === 'FAILED'` with `sap_error_message`

---

## 4. Error Response Format

All error responses follow one of two shapes:

### Simple error
```json
{ "detail": "Human-readable error message." }
```

### Validation error (field-level)
```json
{
  "detail": "Invalid data.",
  "errors": {
    "due_date": ["due_date must be on or after target_start_date."],
    "planned_qty": ["Ensure this value is greater than or equal to 0.001."]
  }
}
```

For validation errors, show the message from `errors.<fieldName>[0]` next to the relevant field. Show `detail` as a general/toast error when no field-level errors are present.

---

## 5. Dropdown APIs

Call these when loading the **Create Plan** or **Add Material** forms to populate select fields. All are GET requests — no body needed.

---

### 5.1 Items Dropdown

```
GET /api/v1/production-planning/dropdown/items/
```

**Query Params**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | No | `finished` — finished goods only  \|  `raw` — raw materials only  \|  omit — all items |
| `search` | string | No | Search by item code or name (partial, case-insensitive) |

**Examples**
```
GET /dropdown/items/?type=finished            → for the "product to produce" dropdown
GET /dropdown/items/?type=raw&search=seeds   → for raw material search in BOM
GET /dropdown/items/                         → all items
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
  },
  {
    "item_code": "RM-SEEDS-01",
    "item_name": "Sunflower Seeds Grade A",
    "uom": "KG",
    "item_group": "Raw Materials",
    "make_item": false,
    "purchase_item": true
  }
]
```

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Success |
| 401 | Not authenticated |
| 403 | No company code / no permission |
| 503 | SAP HANA unavailable |
| 502 | SAP data error |

**UI Tip:** Use `item_code` as the value and `item_name` (or `item_code — item_name`) as the label. When user picks an item, auto-fill the `uom` field from `item.uom`.

---

### 5.2 UoM Dropdown

```
GET /api/v1/production-planning/dropdown/uom/
```

**Response 200**
```json
[
  { "uom_code": "LTR", "uom_name": "Litre" },
  { "uom_code": "KG",  "uom_name": "Kilogram" },
  { "uom_code": "PCS", "uom_name": "Pieces" }
]
```

Use `uom_code` as the value and `uom_name` as the label.

**Status Codes:** Same as Items (200 / 401 / 403 / 502 / 503).

---

### 5.3 Warehouses Dropdown

```
GET /api/v1/production-planning/dropdown/warehouses/
```

**Response 200**
```json
[
  { "warehouse_code": "WH-MAIN",   "warehouse_name": "Main Warehouse" },
  { "warehouse_code": "WH-STORE",  "warehouse_name": "Storage Unit A" }
]
```

Use `warehouse_code` as the value and `warehouse_name` as the label.

**Status Codes:** Same as Items.

---

### 5.4 BOM Auto-Detect (with Quantity & Shortage)

Call this **after the user selects a finished-good item and enters planned_qty**.
It fetches the production BOM from SAP (OITT/ITT1), scales component quantities, and compares against live stock to show shortages.

```
GET /api/v1/production-planning/dropdown/bom/
    ?item_code=FG-OIL-1L
    &planned_qty=500
```

**Query Params**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_code` | string | **Yes** | The finished-good item code selected by the user |
| `planned_qty` | number | No | Planned production quantity (default: 1) |

**Response 200**
```json
{
  "item_code": "FG-OIL-1L",
  "item_name": "Jivo Sunflower Oil 1L",
  "planned_qty": 500.0,
  "bom_found": true,
  "has_shortage": true,
  "components": [
    {
      "component_code": "RM-OIL-CRUDE",
      "component_name": "Crude Sunflower Oil",
      "uom": "LTR",
      "qty_per_unit": 1.05,
      "required_qty": 525.0,
      "available_stock": 400.0,
      "shortage_qty": 125.0,
      "has_shortage": true
    },
    {
      "component_code": "RM-BOTTLE-1L",
      "component_name": "PET Bottle 1L",
      "uom": "Nos",
      "qty_per_unit": 1.0,
      "required_qty": 500.0,
      "available_stock": 600.0,
      "shortage_qty": 0.0,
      "has_shortage": false
    }
  ]
}
```

**Response field meanings**

| Field | Description |
|-------|-------------|
| `bom_found` | `false` if no production BOM exists for this item in SAP |
| `has_shortage` | `true` if ANY component has `shortage_qty > 0` |
| `qty_per_unit` | BOM quantity per 1 unit of finished good |
| `required_qty` | `qty_per_unit × planned_qty` — what you need to produce |
| `available_stock` | Current stock in SAP (across all warehouses) |
| `shortage_qty` | `max(0, required_qty − available_stock)` |
| `has_shortage` | Per-component flag for easy row highlighting |

**UI Logic**

```
1. User selects item from Items Dropdown (type=finished)
2. User enters planned_qty
3. On item select OR qty change → call GET /dropdown/bom/?item_code=X&planned_qty=N
4. If bom_found = false → show warning "No BOM found for this item in SAP"
5. Auto-populate the Materials table with returned components
6. Highlight rows where has_shortage = true (red background / warning icon)
7. Show a banner if top-level has_shortage = true:
   "⚠ Some materials are short. Review before posting to SAP."
8. User can still adjust quantities manually before saving
```

**Status Codes**

| Code | Meaning |
|------|---------|
| 200 | BOM returned (check `bom_found` — may be empty if no BOM in SAP) |
| 400 | Missing or invalid `item_code` / `planned_qty` |
| 401 | Not authenticated |
| 403 | Missing Company-Code header |
| 502 | SAP HANA query error |
| 503 | SAP HANA connection unavailable |

---

## 6. Production Plan — CRUD

---

### 6.1 List Plans

```
GET /api/v1/production-planning/
```

**Query Params**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | No | One of the plan status values |
| `month` | string | No | `YYYY-MM` format — filters by `due_date` month |

**Examples**
```
GET /api/v1/production-planning/                           → all plans
GET /api/v1/production-planning/?status=IN_PROGRESS        → only in-progress
GET /api/v1/production-planning/?month=2026-03             → March plans only
GET /api/v1/production-planning/?status=DRAFT&month=2026-03
```

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

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Success (empty list `[]` if no plans) |
| 401 | Not authenticated |
| 403 | No company code / no permission |

---

### 6.2 Create Plan

```
POST /api/v1/production-planning/
Content-Type: application/json
```

**Request Body**

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
      "component_code": "RM-SEEDS-01",
      "component_name": "Sunflower Seeds Grade A",
      "required_qty": 55000,
      "uom": "KG",
      "warehouse_code": "WH-MAIN"
    }
  ]
}
```

**Field Reference**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `item_code` | string | **Yes** | Max 50 chars. From items dropdown. |
| `item_name` | string | **Yes** | Max 255 chars. |
| `uom` | string | No | Max 20 chars. From UoM dropdown. |
| `warehouse_code` | string | No | Max 20 chars. From warehouses dropdown. |
| `planned_qty` | decimal | **Yes** | Must be > 0. |
| `target_start_date` | date | **Yes** | `YYYY-MM-DD` |
| `due_date` | date | **Yes** | `YYYY-MM-DD`. Must be ≥ `target_start_date`. |
| `branch_id` | integer | No | SAP Branch ID. Can be `null`. |
| `remarks` | string | No | Free text. |
| `materials` | array | No | BOM components. See material fields below. |

**Material fields inside `materials[]`**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `component_code` | string | **Yes** | Max 50 chars. From items dropdown (raw). |
| `component_name` | string | **Yes** | Max 255 chars. |
| `required_qty` | decimal | **Yes** | Must be > 0. |
| `uom` | string | No | Max 20 chars. |
| `warehouse_code` | string | No | Max 20 chars. |

**Response 201** — Full plan detail (see section 6.3 for shape)

**Status Codes**

| Code | Condition |
|------|-----------|
| 201 | Plan created successfully |
| 400 | Validation error — check `errors` object for field-level messages |
| 401 | Not authenticated |
| 403 | No permission (`can_create_production_plan`) |

**Common validation errors**

| Field | Error message |
|-------|---------------|
| `due_date` | `due_date must be on or after target_start_date.` |
| `planned_qty` | `Ensure this value is greater than or equal to 0.001.` |
| `item_code` | `This field is required.` |

---

### 6.3 Get Plan Detail

```
GET /api/v1/production-planning/<plan_id>/
```

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
      "component_code": "RM-SEEDS-01",
      "component_name": "Sunflower Seeds Grade A",
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
    },
    {
      "id": 2,
      "week_number": 2,
      "week_label": "Week 2",
      "start_date": "2026-03-08",
      "end_date": "2026-03-14",
      "target_qty": "12500.000",
      "produced_qty": "6000.000",
      "progress_percent": 48.0,
      "status": "IN_PROGRESS",
      "created_by": 3,
      "created_at": "2026-03-06T09:05:00Z"
    }
  ]
}
```

**Note on nullable fields:**
- `sap_doc_entry`, `sap_doc_num`, `sap_status` — `null` until plan is posted to SAP
- `sap_error_message` — `null` unless posting failed
- `closed_by`, `closed_at` — `null` until plan is closed

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Success |
| 400 | Plan not found (returns `{ "detail": "..." }`) |
| 401 | Not authenticated |
| 403 | No permission |

---

### 6.4 Update Plan

```
PATCH /api/v1/production-planning/<plan_id>/
Content-Type: application/json
```

**Only DRAFT plans can be updated.** Send only the fields you want to change.

**Request Body** (all fields optional)
```json
{
  "item_code": "FG-OIL-2L",
  "item_name": "Jivo Sunflower Oil 2L",
  "uom": "LTR",
  "warehouse_code": "WH-STORE",
  "planned_qty": 48000,
  "target_start_date": "2026-03-05",
  "due_date": "2026-03-31",
  "branch_id": 2,
  "remarks": "Revised plan"
}
```

**Response 200** — Updated plan detail (same shape as 6.3)

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Updated successfully |
| 400 | Validation error OR plan is not DRAFT |
| 401 | Not authenticated |
| 403 | No permission (`can_edit_production_plan`) |

**Common errors**
```json
{ "detail": "Only DRAFT plans can be edited. Current status: 'OPEN'." }
```

---

### 6.5 Delete Plan

```
DELETE /api/v1/production-planning/<plan_id>/
```

**Only DRAFT plans can be deleted.** No request body needed.

**Response 204** — No content (empty body)

**Status Codes**

| Code | Condition |
|------|-----------|
| 204 | Deleted successfully |
| 400 | Plan is not DRAFT |
| 401 | Not authenticated |
| 403 | No permission (`can_delete_production_plan`) |

---

## 7. Post Plan to SAP

```
POST /api/v1/production-planning/<plan_id>/post-to-sap/
```

Sends the plan to SAP B1 as a Production Order. No request body needed.

**When to show this button:** `sap_posting_status !== 'POSTED'` AND `status` is `DRAFT` or `OPEN`.
This means it's also usable as a **retry** button when `sap_posting_status === 'FAILED'`.

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

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Posted to SAP successfully |
| 400 | Plan already posted OR plan status not valid for posting |
| 401 | Not authenticated |
| 403 | No permission (`can_post_plan_to_sap`) |
| 503 | SAP system unavailable — show retry message to user |
| 502 | SAP data error |

**Error Responses**
```json
{ "detail": "Plan already posted to SAP. DocNum: 10042" }
{ "detail": "Only DRAFT or OPEN plans can be posted to SAP. Current status: 'IN_PROGRESS'." }
{ "detail": "SAP validation error: Item FG-OIL-1L not found in SAP." }
{ "detail": "SAP system is currently unavailable. Please try again later." }
```

**UI behaviour after success:**
- Reload plan detail to see updated `status`, `sap_doc_num`, `sap_posting_status`
- Show success toast: `"Production Order #10042 created in SAP"`

**UI behaviour on 503/502:**
- Show error banner: `"SAP is currently unavailable. Your plan is saved — please try again later."`
- Do NOT disable the Post button — user should be able to retry

---

## 8. Close Plan

```
POST /api/v1/production-planning/<plan_id>/close/
```

Closes the plan and marks it as COMPLETED. No request body needed.

**Response 200**
```json
{
  "success": true,
  "plan_id": 1,
  "status": "COMPLETED",
  "total_produced": "48200.000",
  "planned_qty": "50000.000",
  "message": "Production plan closed. Total produced: 48200.000 units."
}
```

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Closed successfully |
| 400 | Plan already closed/cancelled |
| 401 | Not authenticated |
| 403 | No permission (`can_close_production_plan`) |

**Error Response**
```json
{ "detail": "Cannot close a plan with status 'CLOSED'." }
```

**UI Tip:** Show a confirmation dialog before calling this. After success, reload plan detail and disable all editing controls.

---

## 9. Summary / Dashboard

```
GET /api/v1/production-planning/summary/
```

**Query Params**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `month` | string | No | `YYYY-MM` — filter by due_date month |

**Response 200**
```json
{
  "total_plans": 5,
  "total_planned_qty": 250000.0,
  "total_produced_qty": 95000.0,
  "overall_progress_percent": 38.0,
  "status_breakdown": {
    "DRAFT": 1,
    "OPEN": 1,
    "IN_PROGRESS": 2,
    "COMPLETED": 1,
    "CLOSED": 0,
    "CANCELLED": 0
  },
  "sap_posting_breakdown": {
    "NOT_POSTED": 1,
    "POSTED": 4,
    "FAILED": 0
  }
}
```

**Status Codes:** 200 / 401 / 403

**UI Tip:** Use `overall_progress_percent` for a top-level progress bar. Use `status_breakdown` for a status distribution chart. Show `sap_posting_breakdown.FAILED` as a warning badge if > 0.

---

## 10. Material Requirements

---

### 10.1 List Materials

```
GET /api/v1/production-planning/<plan_id>/materials/
```

**Response 200**
```json
[
  {
    "id": 1,
    "component_code": "RM-SEEDS-01",
    "component_name": "Sunflower Seeds Grade A",
    "required_qty": "55000.000",
    "uom": "KG",
    "warehouse_code": "WH-MAIN"
  }
]
```

**Status Codes:** 200 / 401 / 403 / 404

---

### 10.2 Add Material

```
POST /api/v1/production-planning/<plan_id>/materials/
Content-Type: application/json
```

Allowed for DRAFT, OPEN, and IN_PROGRESS plans.

**Request Body**
```json
{
  "component_code": "RM-FILTER-01",
  "component_name": "Filter Paper",
  "required_qty": 5000,
  "uom": "PCS",
  "warehouse_code": "WH-MAIN"
}
```

**Field Reference**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `component_code` | string | **Yes** | Max 50 chars |
| `component_name` | string | **Yes** | Max 255 chars |
| `required_qty` | decimal | **Yes** | Must be > 0 |
| `uom` | string | No | Max 20 chars |
| `warehouse_code` | string | No | Max 20 chars |

**Response 201**
```json
{
  "id": 2,
  "component_code": "RM-FILTER-01",
  "component_name": "Filter Paper",
  "required_qty": "5000.000",
  "uom": "PCS",
  "warehouse_code": "WH-MAIN"
}
```

**Status Codes**

| Code | Condition |
|------|-----------|
| 201 | Created |
| 400 | Validation error OR plan is CLOSED/CANCELLED/COMPLETED |
| 401 | Not authenticated |
| 403 | No permission (`can_edit_production_plan`) |

---

### 10.3 Delete Material

```
DELETE /api/v1/production-planning/<plan_id>/materials/<material_id>/
```

**Only DRAFT plans.** No request body.

**Response 204** — No content

**Status Codes**

| Code | Condition |
|------|-----------|
| 204 | Deleted |
| 400 | Plan is not DRAFT OR material not found |
| 401 | Not authenticated |
| 403 | No permission |

---

## 11. Weekly Plans

---

### 11.1 List Weekly Plans

```
GET /api/v1/production-planning/<plan_id>/weekly-plans/
```

**Response 200**
```json
[
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
  },
  {
    "id": 2,
    "week_number": 2,
    "week_label": "Week 2",
    "start_date": "2026-03-08",
    "end_date": "2026-03-14",
    "target_qty": "12500.000",
    "produced_qty": "6000.000",
    "progress_percent": 48.0,
    "status": "IN_PROGRESS",
    "created_by": 3,
    "created_at": "2026-03-06T09:05:00Z"
  }
]
```

**Status Codes:** 200 / 401 / 403 / 404

---

### 11.2 Create Weekly Plan

```
POST /api/v1/production-planning/<plan_id>/weekly-plans/
Content-Type: application/json
```

**Plan must be OPEN or IN_PROGRESS** (i.e., already posted to SAP).

**Request Body**
```json
{
  "week_number": 3,
  "week_label": "Week 3",
  "start_date": "2026-03-15",
  "end_date": "2026-03-21",
  "target_qty": 12500
}
```

**Field Reference**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `week_number` | integer | **Yes** | Must be unique per plan (1, 2, 3 ...) |
| `week_label` | string | No | Max 100 chars. Free text (e.g., "Week 1") |
| `start_date` | date | **Yes** | `YYYY-MM-DD`. Within plan's date range. |
| `end_date` | date | **Yes** | `YYYY-MM-DD`. Must be ≥ `start_date`. Within plan's date range. |
| `target_qty` | decimal | **Yes** | Must be > 0. Sum of all weeks must not exceed `planned_qty`. |

**Response 201**
```json
{
  "id": 3,
  "week_number": 3,
  "week_label": "Week 3",
  "start_date": "2026-03-15",
  "end_date": "2026-03-21",
  "target_qty": "12500.000",
  "produced_qty": "0.000",
  "progress_percent": 0.0,
  "status": "PENDING",
  "created_by": 3,
  "created_at": "2026-03-06T09:10:00Z"
}
```

**Side effect:** If the plan was `OPEN`, it automatically transitions to `IN_PROGRESS`. Reload plan detail after this call.

**Status Codes**

| Code | Condition |
|------|-----------|
| 201 | Created |
| 400 | Validation error — see below |
| 401 | Not authenticated |
| 403 | No permission (`can_manage_weekly_plan`) |

**Common validation errors**
```json
{ "detail": "Invalid data.", "errors": { "non_field_errors": ["end_date must be >= start_date."] } }
{ "detail": "Invalid data.", "errors": { "non_field_errors": ["Total weekly target would exceed plan quantity. Available: 25000.000."] } }
{ "detail": "Invalid data.", "errors": { "non_field_errors": ["Week 2 already exists for this plan."] } }
{ "detail": "Cannot add weekly plans to a plan with status 'DRAFT'. Post the plan to SAP first." }
```

---

### 11.3 Update Weekly Plan

```
PATCH /api/v1/production-planning/<plan_id>/weekly-plans/<week_id>/
Content-Type: application/json
```

Can only update `week_label` and `target_qty`.

**Request Body** (partial — send only what changes)
```json
{
  "week_label": "Week 3 — revised",
  "target_qty": 11000
}
```

**Response 200** — Updated weekly plan object (same shape as list item)

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Updated |
| 400 | Validation error OR plan closed/cancelled |
| 401 | Not authenticated |
| 403 | No permission |

---

### 11.4 Delete Weekly Plan

```
DELETE /api/v1/production-planning/<plan_id>/weekly-plans/<week_id>/
```

**Cannot delete if the week has any daily entries.**

**Response 204** — No content

**Status Codes**

| Code | Condition |
|------|-----------|
| 204 | Deleted |
| 400 | Week has daily entries OR plan closed/cancelled |
| 401 | Not authenticated |
| 403 | No permission |

**Error**
```json
{ "detail": "Cannot delete a weekly plan that has daily production entries." }
```

---

## 12. Daily Production Entries

---

### 12.1 List Daily Entries for a Week

```
GET /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/
```

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
  },
  {
    "id": 6,
    "production_date": "2026-03-03",
    "produced_qty": "1900.000",
    "shift": "AFTERNOON",
    "remarks": "",
    "recorded_by": 7,
    "created_at": "2026-03-03T20:00:00Z"
  }
]
```

**Status Codes:** 200 / 401 / 403 / 404

---

### 12.2 Add Daily Entry

```
POST /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/
Content-Type: application/json
```

**Request Body**
```json
{
  "production_date": "2026-03-05",
  "produced_qty": 2100,
  "shift": "MORNING",
  "remarks": "Line 2 had 30 min delay"
}
```

**Field Reference**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `production_date` | date | **Yes** | `YYYY-MM-DD`. Must be within week's date range. Cannot be future date. |
| `produced_qty` | decimal | **Yes** | Must be > 0 |
| `shift` | string | No | `MORNING`, `AFTERNOON`, `NIGHT`, or omit/`null` |
| `remarks` | string | No | Free text |

**Response 201** — includes live progress data (useful for immediate UI update)
```json
{
  "id": 7,
  "production_date": "2026-03-05",
  "produced_qty": "2100.000",
  "shift": "MORNING",
  "remarks": "Line 2 had 30 min delay",
  "recorded_by": 7,
  "created_at": "2026-03-05T18:00:00Z",
  "weekly_plan_progress": {
    "week_target": "12500.000",
    "produced_so_far": "8200.000",
    "remaining": "4300.000",
    "progress_percent": 65.6
  },
  "plan_progress": {
    "plan_target": "50000.000",
    "produced_so_far": "20700.000",
    "progress_percent": 41.4
  }
}
```

**Status Codes**

| Code | Condition |
|------|-----------|
| 201 | Entry added |
| 400 | Validation error — see below |
| 401 | Not authenticated |
| 403 | No permission (`can_add_daily_production`) |

**Common validation errors**
```json
{ "errors": { "non_field_errors": ["production_date must be within week range: 2026-03-01 to 2026-03-07."] } }
{ "errors": { "non_field_errors": ["production_date cannot be in the future."] } }
{ "errors": { "non_field_errors": ["Entry already exists for 2026-03-05 (MORNING shift)."] } }
{ "errors": { "non_field_errors": ["Cannot add entries to a plan with status 'CLOSED'."] } }
```

**UI Tip:** After a successful POST, use the `weekly_plan_progress` and `plan_progress` data to update progress bars immediately without a full page reload.

---

### 12.3 Edit Daily Entry

```
PATCH /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/<entry_id>/
Content-Type: application/json
```

Can only update `produced_qty` and `remarks`.

**Request Body** (partial)
```json
{
  "produced_qty": 2250,
  "remarks": "Corrected: actual count was 2250"
}
```

**Response 200** — Updated entry object (same shape as list item — without progress data)

**Status Codes**

| Code | Condition |
|------|-----------|
| 200 | Updated |
| 400 | Entry not found |
| 401 | Not authenticated |
| 403 | No permission |

---

### 12.4 All Daily Entries (Cross-Plan)

```
GET /api/v1/production-planning/daily-entries/
```

**Query Params**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `plan_id` | integer | No | Filter by production plan |
| `date_from` | date | No | `YYYY-MM-DD` — from date inclusive |
| `date_to` | date | No | `YYYY-MM-DD` — to date inclusive |

**Response 200** — Same array structure as 12.1

**Status Codes:** 200 / 401 / 403

---

## 13. UI Logic Guide

### Form: Create Plan

1. Load dropdowns in parallel before showing form:
   - `GET /dropdown/items/?type=finished` → item select
   - `GET /dropdown/uom/` → UoM select
   - `GET /dropdown/warehouses/` → warehouse select
2. When user picks an item, auto-fill `uom` from `item.uom`
3. `due_date` should not be selectable before `target_start_date` (client-side date picker constraint)
4. Materials section: allow adding multiple rows, each with its own item search (`?type=raw&search=<query>`)
5. On submit: `POST /` → on 201 redirect to plan detail; on 400 show field errors

### Plan Detail Page — Button Visibility

```
Plan status    | Edit | Delete | Post to SAP | Add Weekly Plan | Add Entry | Close
---------------|------|--------|-------------|-----------------|-----------|------
DRAFT          |  ✅  |   ✅   |     ✅      |       ❌        |    ❌     |  ❌
OPEN           |  ❌  |   ❌   |     ✅*     |       ✅        |    ❌     |  ✅
IN_PROGRESS    |  ❌  |   ❌   |     ✅*     |       ✅        |    ✅     |  ✅
COMPLETED      |  ❌  |   ❌   |     ❌      |       ❌        |    ❌     |  ❌
CLOSED         |  ❌  |   ❌   |     ❌      |       ❌        |    ❌     |  ❌
CANCELLED      |  ❌  |   ❌   |     ❌      |       ❌        |    ❌     |  ❌
```

`*` Post to SAP only when `sap_posting_status !== 'POSTED'`

### SAP Posting Status Badge

| `sap_posting_status` | Badge color | Label |
|----------------------|-------------|-------|
| `NOT_POSTED` | Grey | Not Posted |
| `POSTED` | Green | Posted to SAP · Doc# {sap_doc_num} |
| `FAILED` | Red | SAP Failed — show `sap_error_message` in tooltip/banner |

### Weekly Plan Form

- Auto-suggest `week_number` = (existing weeks count + 1)
- `start_date` and `end_date` must be within plan's `target_start_date` → `due_date`
- Show remaining allocatable quantity: `planned_qty - sum(weekly target_qty)`
- Validate client-side before submitting

### Daily Entry Form

- `production_date` picker: restrict to current week's `start_date` → `end_date` AND not future
- `shift` is optional; if your factory does not use shifts, omit it entirely
- After submit: use `weekly_plan_progress` from response to update progress bar without refetch

### Progress Bars

- Weekly progress bar: `progress_percent` from weekly plan object
- Plan overall progress: `progress_percent` from plan object
- `progress_percent` is already calculated server-side as `(completed_qty / planned_qty) * 100`
- Quantities are returned as strings with 3 decimal places (e.g., `"12500.000"`) — parse as float for display

### Numbers & Formatting

- All decimal quantities come as strings with 3 decimal places: `"50000.000"`
- Parse with `parseFloat()` before math operations
- `progress_percent` comes as a float: `37.0` — display as `37%`

---

## 14. Status Code Reference

| HTTP Code | Meaning | What to do in UI |
|-----------|---------|-----------------|
| `200` | Success | Use response data |
| `201` | Created | Show success toast, navigate/reload |
| `204` | Deleted | Remove item from list, show toast |
| `400` | Bad request / validation error / business rule violated | Show `errors` field-level messages or `detail` as toast |
| `401` | Unauthorized — invalid/missing token | Redirect to login |
| `403` | Forbidden — missing Company-Code header OR no permission | Show "You don't have permission" message |
| `404` | Not found | Show "Not found" page or remove from list |
| `502` | SAP HANA data error | Show: "SAP returned an error: {detail}" |
| `503` | SAP HANA unreachable | Show: "SAP is currently unavailable. Please try again later." with retry button |
