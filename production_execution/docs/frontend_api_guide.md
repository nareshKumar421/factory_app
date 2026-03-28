# Production Execution — Frontend API Guide

> Complete reference for frontend developers integrating with the Production Execution module.

---

## Table of Contents

1. [Setup & Authentication](#1-setup--authentication)
2. [Enums & Constants](#2-enums--constants)
3. [Error Handling](#3-error-handling)
4. [Production Lines](#4-production-lines)
5. [Machines](#5-machines)
6. [Checklist Templates](#6-checklist-templates)
7. [Production Runs](#7-production-runs)
8. [Hourly Production Logs](#8-hourly-production-logs)
9. [Machine Breakdowns](#9-machine-breakdowns)
10. [Material Usage (Yield)](#10-material-usage-yield)
11. [Machine Runtime](#11-machine-runtime)
12. [Manpower](#12-manpower)
13. [Line Clearance](#13-line-clearance)
14. [Machine Checklists](#14-machine-checklists)
15. [Waste Management](#15-waste-management)
16. [Reports](#16-reports)
17. [Workflows & State Diagrams](#17-workflows--state-diagrams)
18. [Pre-defined Constants](#18-pre-defined-constants)

---

## 1. Setup & Authentication

### Base URL

```
/api/v1/production-execution/
```

### Required Headers

Every request must include:

| Header | Value | Description |
|--------|-------|-------------|
| `Authorization` | `Bearer <access_token>` | JWT access token from login |
| `Company-Code` | `JIVO_OIL` / `JIVO_MART` / `JIVO_BEVERAGES` | Identifies the company context |
| `Content-Type` | `application/json` | Required for POST/PATCH requests |

### Example Axios Setup

```javascript
import axios from 'axios';

const api = axios.create({
  baseURL: '/api/v1/production-execution',
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth headers via interceptor
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  const companyCode = localStorage.getItem('company_code');
  config.headers['Authorization'] = `Bearer ${token}`;
  config.headers['Company-Code'] = companyCode;
  return config;
});
```

### Permission Requirements

Each endpoint requires the user to have specific permissions. If a user lacks the required permission, the API returns `403 Forbidden`. Permissions are listed per endpoint below.

---

## 2. Enums & Constants

Use these exact string values in request bodies and for interpreting responses.

### Machine Types

```javascript
const MACHINE_TYPES = [
  { value: "FILLER",           label: "Filler" },
  { value: "CAPPER",           label: "Capper" },
  { value: "CONVEYOR",         label: "Conveyor" },
  { value: "LABELER",          label: "Labeler" },
  { value: "CODING",           label: "Coding" },
  { value: "SHRINK_PACK",      label: "Shrink Pack" },
  { value: "STICKER_LABELER",  label: "Sticker Labeler" },
  { value: "TAPPING_MACHINE",  label: "Tapping Machine" },
];
```

### Run Statuses

```javascript
const RUN_STATUSES = [
  { value: "DRAFT",       label: "Draft",       color: "gray" },
  { value: "IN_PROGRESS", label: "In Progress", color: "blue" },
  { value: "COMPLETED",   label: "Completed",   color: "green" },
];
```

### Machine Statuses (for hourly logs)

```javascript
const MACHINE_STATUSES = [
  { value: "RUNNING",    label: "Running" },
  { value: "IDLE",       label: "Idle" },
  { value: "BREAKDOWN",  label: "Breakdown" },
  { value: "CHANGEOVER", label: "Changeover" },
];
```

### Breakdown Types

```javascript
const BREAKDOWN_TYPES = [
  { value: "LINE",     label: "Line" },
  { value: "EXTERNAL", label: "External" },
];
```

### Checklist Frequencies

```javascript
const CHECKLIST_FREQUENCIES = [
  { value: "DAILY",   label: "Daily" },
  { value: "WEEKLY",  label: "Weekly" },
  { value: "MONTHLY", label: "Monthly" },
];
```

### Checklist Statuses

```javascript
const CHECKLIST_STATUSES = [
  { value: "OK",     label: "OK" },
  { value: "NOT_OK", label: "Not OK" },
  { value: "NA",     label: "N/A" },
];
```

### Clearance Result (for checklist items)

```javascript
const CLEARANCE_RESULTS = [
  { value: "YES", label: "Yes" },
  { value: "NO",  label: "No" },
  { value: "NA",  label: "N/A" },
];
```

### Clearance Statuses

```javascript
const CLEARANCE_STATUSES = [
  { value: "DRAFT",       label: "Draft" },
  { value: "SUBMITTED",   label: "Submitted" },
  { value: "CLEARED",     label: "Cleared" },
  { value: "NOT_CLEARED", label: "Not Cleared" },
];
```

### Waste Approval Statuses

```javascript
const WASTE_APPROVAL_STATUSES = [
  { value: "PENDING",            label: "Pending" },
  { value: "PARTIALLY_APPROVED", label: "Partially Approved" },
  { value: "FULLY_APPROVED",     label: "Fully Approved" },
];
```

### Shift Choices

```javascript
const SHIFTS = [
  { value: "MORNING",   label: "Morning" },
  { value: "AFTERNOON", label: "Afternoon" },
  { value: "NIGHT",     label: "Night" },
];
```

---

## 3. Error Handling

### Error Response Formats

**Validation error (400):**

```json
{
  "detail": "Invalid data.",
  "errors": {
    "name": ["This field is required."],
    "machine_type": ["\"INVALID\" is not a valid choice."]
  }
}
```

**Business logic error (400):**

```json
{
  "detail": "Cannot edit a COMPLETED run."
}
```

**Not found (404):**

```json
{
  "detail": "Production run 999 not found."
}
```

**Authentication error (401):**

```json
{
  "detail": "Given token not valid for any token type",
  "code": "token_not_valid"
}
```

**Permission denied (403):**

```json
{
  "detail": "You do not have permission to perform this action."
}
```

### Recommended Error Handler

```javascript
function handleApiError(error) {
  if (!error.response) {
    return "Network error. Please check your connection.";
  }
  const { status, data } = error.response;
  switch (status) {
    case 400:
      if (data.errors) {
        // Validation errors - show per-field
        return data.errors;
      }
      return data.detail;
    case 401:
      // Redirect to login / refresh token
      return "Session expired. Please log in again.";
    case 403:
      return "You don't have permission for this action.";
    case 404:
      return data.detail || "Resource not found.";
    default:
      return "Something went wrong. Please try again.";
  }
}
```

---

## 4. Production Lines

### List Lines

```
GET /lines/
```

**Permission:** `can_view_production_run`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `is_active` | `string` | `"true"` or `"false"` — filter by active status |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "name": "Line-1",
    "description": "Main bottling line",
    "is_active": true,
    "created_at": "2026-03-01T10:00:00+05:30",
    "updated_at": "2026-03-01T10:00:00+05:30"
  }
]
```

### Create Line

```
POST /lines/
```

**Permission:** `can_manage_production_lines`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | Yes | Line name (max 100 chars) |
| `description` | `string` | No | Description (default: `""`) |

```json
{
  "name": "Line-2",
  "description": "Secondary bottling line"
}
```

**Response: `201 Created`**

```json
{
  "id": 2,
  "name": "Line-2",
  "description": "Secondary bottling line",
  "is_active": true,
  "created_at": "2026-03-07T10:00:00+05:30",
  "updated_at": "2026-03-07T10:00:00+05:30"
}
```

### Update Line

```
PATCH /lines/{line_id}/
```

**Permission:** `can_manage_production_lines`

**Request Body (all fields optional):**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | New name |
| `description` | `string` | New description |
| `is_active` | `boolean` | Active status |

```json
{
  "description": "Updated description"
}
```

**Response: `200 OK`** — Returns full line object.

### Delete (Soft-Delete) Line

```
DELETE /lines/{line_id}/
```

**Permission:** `can_manage_production_lines`

Sets `is_active = false`. Does NOT physically delete the record.

**Response: `204 No Content`** — Empty body.

---

## 5. Machines

### List Machines

```
GET /machines/
```

**Permission:** `can_view_production_run`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `line_id` | `integer` | Filter by production line |
| `machine_type` | `string` | Filter by type (e.g., `FILLER`) |
| `is_active` | `string` | `"true"` or `"false"` |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "name": "Filler Machine A",
    "machine_type": "FILLER",
    "line": 1,
    "line_name": "Line-1",
    "is_active": true,
    "created_at": "2026-03-01T10:00:00+05:30",
    "updated_at": "2026-03-01T10:00:00+05:30"
  }
]
```

### Create Machine

```
POST /machines/
```

**Permission:** `can_manage_machines`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | Yes | Machine name (max 200 chars) |
| `machine_type` | `string` | Yes | One of `MACHINE_TYPES` values |
| `line_id` | `integer` | Yes | FK to production line |

```json
{
  "name": "Capper Unit B",
  "machine_type": "CAPPER",
  "line_id": 1
}
```

**Response: `201 Created`** — Returns full machine object with `line_name`.

### Update Machine

```
PATCH /machines/{machine_id}/
```

**Permission:** `can_manage_machines`

**Request Body (all optional):**

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | New name |
| `machine_type` | `string` | New type |
| `line_id` | `integer` | Reassign to different line |
| `is_active` | `boolean` | Active status |

**Response: `200 OK`** — Returns full machine object.

### Delete (Soft-Delete) Machine

```
DELETE /machines/{machine_id}/
```

**Permission:** `can_manage_machines`

**Response: `204 No Content`**

---

## 6. Checklist Templates

### List Templates

```
GET /checklist-templates/
```

**Permission:** `can_view_machine_checklist`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `machine_type` | `string` | Filter by machine type |
| `frequency` | `string` | Filter by frequency (`DAILY`, `WEEKLY`, `MONTHLY`) |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "machine_type": "FILLER",
    "task": "Check oil level",
    "frequency": "DAILY",
    "sort_order": 1,
    "is_active": true,
    "created_at": "2026-03-01T10:00:00+05:30",
    "updated_at": "2026-03-01T10:00:00+05:30"
  }
]
```

### Create Template

```
POST /checklist-templates/
```

**Permission:** `can_manage_checklist_templates`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `machine_type` | `string` | Yes | One of `MACHINE_TYPES` values |
| `task` | `string` | Yes | Task description (max 500 chars) |
| `frequency` | `string` | Yes | `DAILY`, `WEEKLY`, or `MONTHLY` |
| `sort_order` | `integer` | No | Display order (default: `0`) |

```json
{
  "machine_type": "FILLER",
  "task": "Check oil level and pressure gauge",
  "frequency": "DAILY",
  "sort_order": 1
}
```

**Response: `201 Created`** — Returns full template object.

### Update Template

```
PATCH /checklist-templates/{template_id}/
```

**Permission:** `can_manage_checklist_templates`

**Request Body (all optional):** `machine_type`, `task`, `frequency`, `sort_order`, `is_active`

**Response: `200 OK`**

### Delete Template

```
DELETE /checklist-templates/{template_id}/
```

**Permission:** `can_manage_checklist_templates`

This is a **hard delete** (permanently removes the record).

**Response: `204 No Content`**

---

## 7. Production Runs

### List Runs

```
GET /runs/
```

**Permission:** `can_view_production_run`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `date` | `string` | `YYYY-MM-DD` format |
| `line_id` | `integer` | Filter by line |
| `status` | `string` | `DRAFT`, `IN_PROGRESS`, or `COMPLETED` |
| `sap_doc_entry` | `integer` | Filter by SAP OWOR DocEntry |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "sap_doc_entry": 100,
    "run_number": 1,
    "date": "2026-03-07",
    "line": 1,
    "line_name": "Line-1",
    "brand": "Extra Light",
    "pack": "1L x 12",
    "sap_order_no": "PO-12345",
    "rated_speed": "150.00",
    "total_production": 1200,
    "total_breakdown_time": 35,
    "status": "IN_PROGRESS",
    "created_by": 1,
    "created_at": "2026-03-07T07:00:00+05:30"
  }
]
```

### Create Run

```
POST /runs/
```

**Permission:** `can_create_production_run`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sap_doc_entry` | `integer` | No | SAP OWOR DocEntry (from SAP production order) |
| `line_id` | `integer` | Yes | FK to production line (must be active) |
| `date` | `string` | Yes | `YYYY-MM-DD` format |
| `brand` | `string` | No | Brand name (default: `""`) |
| `pack` | `string` | No | Pack size (default: `""`) |
| `sap_order_no` | `string` | No | SAP order reference (default: `""`) |
| `rated_speed` | `number` | No | Rated speed in units/min (default: `null`) |

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

**Response: `201 Created`** — Returns full run detail object (see Get Run Detail below).

**Business Rules:**
- `sap_doc_entry` is the SAP OWOR `DocEntry` fetched from `GET /sap/orders/`. It links the run directly to the SAP production order.
- `run_number` is auto-incremented per SAP order+date combination. No need to send it.
- Status is always set to `DRAFT` on creation.

### Get Run Detail

```
GET /runs/{run_id}/
```

**Permission:** `can_view_production_run`

**Response: `200 OK`**

```json
{
  "id": 1,
  "sap_doc_entry": 100,
  "run_number": 1,
  "date": "2026-03-07",
  "line": 1,
  "line_name": "Line-1",
  "brand": "Extra Light",
  "pack": "1L x 12",
  "sap_order_no": "PO-12345",
  "rated_speed": "150.00",
  "total_production": 1200,
  "total_minutes_pe": 660,
  "total_minutes_me": 660,
  "total_breakdown_time": 35,
  "line_breakdown_time": 20,
  "external_breakdown_time": 15,
  "unrecorded_time": 25,
  "status": "IN_PROGRESS",
  "created_by": 1,
  "created_at": "2026-03-07T07:00:00+05:30",
  "updated_at": "2026-03-07T15:30:00+05:30",
  "logs": [
    {
      "id": 1,
      "time_slot": "07:00-08:00",
      "time_start": "07:00:00",
      "time_end": "08:00:00",
      "produced_cases": 90,
      "machine_status": "RUNNING",
      "recd_minutes": 55,
      "breakdown_detail": "",
      "remarks": "",
      "created_at": "2026-03-07T08:05:00+05:30",
      "updated_at": "2026-03-07T08:05:00+05:30"
    }
  ],
  "breakdowns": [
    {
      "id": 1,
      "machine": 3,
      "machine_name": "Filler Machine A",
      "start_time": "2026-03-07T14:00:00+05:30",
      "end_time": "2026-03-07T14:35:00+05:30",
      "breakdown_minutes": 35,
      "type": "LINE",
      "is_unrecovered": false,
      "reason": "Power cut",
      "remarks": "",
      "created_at": "2026-03-07T14:40:00+05:30",
      "updated_at": "2026-03-07T14:40:00+05:30"
    }
  ]
}
```

### Update Run

```
PATCH /runs/{run_id}/
```

**Permission:** `can_edit_production_run`

**Request Body (all optional):**

| Field | Type | Description |
|-------|------|-------------|
| `brand` | `string` | Brand name |
| `pack` | `string` | Pack size |
| `sap_order_no` | `string` | SAP order reference |
| `rated_speed` | `number` | Rated speed |

```json
{
  "brand": "Extra Light Updated",
  "rated_speed": 160.0
}
```

**Response: `200 OK`** — Returns full run detail object.

**Business Rules:**
- Cannot update a `COMPLETED` run (returns `400`).
- If run is `DRAFT`, status auto-changes to `IN_PROGRESS` on first update.

### Complete Run

```
POST /runs/{run_id}/complete/
```

**Permission:** `can_complete_production_run`

**Request Body:** None (empty body or `{}`)

**Response: `200 OK`** — Returns full run detail with recomputed totals.

**What happens on completion:**
- `total_production` = sum of all hourly log `produced_cases`
- `total_minutes_pe` = sum of all hourly log `recd_minutes`
- `total_breakdown_time` = sum of all breakdown `breakdown_minutes`
- `line_breakdown_time` = sum of breakdowns where `type = "LINE"`
- `external_breakdown_time` = sum of breakdowns where `type = "EXTERNAL"`
- `unrecorded_time` = `720 - total_minutes_pe - total_breakdown_time` (720 = 12-hour shift)
- Status changes to `COMPLETED`
- Cannot be undone

---

## 8. Hourly Production Logs

### Get Logs

```
GET /runs/{run_id}/logs/
```

**Permission:** `can_view_production_log`

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "time_slot": "07:00-08:00",
    "time_start": "07:00:00",
    "time_end": "08:00:00",
    "produced_cases": 90,
    "machine_status": "RUNNING",
    "recd_minutes": 55,
    "breakdown_detail": "",
    "remarks": "",
    "created_at": "2026-03-07T08:05:00+05:30",
    "updated_at": "2026-03-07T08:05:00+05:30"
  }
]
```

### Bulk Create/Update Logs

```
POST /runs/{run_id}/logs/
```

**Permission:** `can_edit_production_log`

Accepts a **single object OR an array**. Logs are upserted by `time_start` — if a log with the same `time_start` already exists for this run, it gets updated.

**Request Body (array of log entries):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `time_slot` | `string` | Yes | Display label, e.g. `"07:00-08:00"` |
| `time_start` | `string` | Yes | `HH:MM` format (e.g., `"07:00"`) |
| `time_end` | `string` | Yes | `HH:MM` format (e.g., `"08:00"`) |
| `produced_cases` | `integer` | No | Cases produced this hour (default: `0`, min: `0`) |
| `machine_status` | `string` | No | One of `MACHINE_STATUSES` (default: `"RUNNING"`) |
| `recd_minutes` | `integer` | No | Recorded production minutes (default: `0`, max: `60`) |
| `breakdown_detail` | `string` | No | Breakdown notes (default: `""`) |
| `remarks` | `string` | No | Additional remarks (default: `""`) |

```json
[
  {
    "time_slot": "07:00-08:00",
    "time_start": "07:00",
    "time_end": "08:00",
    "produced_cases": 90,
    "machine_status": "RUNNING",
    "recd_minutes": 55,
    "breakdown_detail": "",
    "remarks": ""
  },
  {
    "time_slot": "08:00-09:00",
    "time_start": "08:00",
    "time_end": "09:00",
    "produced_cases": 110,
    "machine_status": "RUNNING",
    "recd_minutes": 60,
    "breakdown_detail": "",
    "remarks": ""
  }
]
```

**Response: `201 Created`** — Returns array of saved log objects.

**Business Rules:**
- Cannot add logs to a `COMPLETED` run.
- Run totals are automatically recomputed after saving logs.
- If run is `DRAFT`, status auto-changes to `IN_PROGRESS`.

### Pre-defined Time Slots

Use these 12 slots for the hourly log form:

```javascript
const TIME_SLOTS = [
  { slot: "07:00-08:00", start: "07:00", end: "08:00" },
  { slot: "08:00-09:00", start: "08:00", end: "09:00" },
  { slot: "09:00-10:00", start: "09:00", end: "10:00" },
  { slot: "10:00-11:00", start: "10:00", end: "11:00" },
  { slot: "11:00-12:00", start: "11:00", end: "12:00" },
  { slot: "12:00-13:00", start: "12:00", end: "13:00" },
  { slot: "13:00-14:00", start: "13:00", end: "14:00" },
  { slot: "14:00-15:00", start: "14:00", end: "15:00" },
  { slot: "15:00-16:00", start: "15:00", end: "16:00" },
  { slot: "16:00-17:00", start: "16:00", end: "17:00" },
  { slot: "17:00-18:00", start: "17:00", end: "18:00" },
  { slot: "18:00-19:00", start: "18:00", end: "19:00" },
];
```

### Update Single Log

```
PATCH /runs/{run_id}/logs/{log_id}/
```

**Permission:** `can_edit_production_log`

**Request Body (all optional):**

| Field | Type | Description |
|-------|------|-------------|
| `produced_cases` | `integer` | Updated cases |
| `machine_status` | `string` | Updated status |
| `recd_minutes` | `integer` | Updated minutes (max 60) |
| `breakdown_detail` | `string` | Updated detail |
| `remarks` | `string` | Updated remarks |

**Response: `200 OK`** — Returns updated log object.

---

## 9. Machine Breakdowns

### List Breakdowns

```
GET /runs/{run_id}/breakdowns/
```

**Permission:** `can_view_breakdown`

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "machine": 3,
    "machine_name": "Filler Machine A",
    "start_time": "2026-03-07T14:00:00+05:30",
    "end_time": "2026-03-07T14:35:00+05:30",
    "breakdown_minutes": 35,
    "type": "LINE",
    "is_unrecovered": false,
    "reason": "Power cut",
    "remarks": "",
    "created_at": "2026-03-07T14:40:00+05:30",
    "updated_at": "2026-03-07T14:40:00+05:30"
  }
]
```

### Create Breakdown

```
POST /runs/{run_id}/breakdowns/
```

**Permission:** `can_create_breakdown`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `machine_id` | `integer` | Yes | FK to machine (must belong to same line as run) |
| `start_time` | `string` | Yes | ISO 8601 datetime: `"2026-03-07T14:00:00"` |
| `end_time` | `string` | No | ISO 8601 datetime (must be >= start_time) |
| `breakdown_minutes` | `integer` | No | Minutes (auto-calculated if `end_time` provided and this is `0`) |
| `type` | `string` | Yes | `"LINE"` or `"EXTERNAL"` |
| `is_unrecovered` | `boolean` | No | Default: `false` |
| `reason` | `string` | Yes | Reason for breakdown (max 500 chars) |
| `remarks` | `string` | No | Additional remarks (default: `""`) |

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

**Response: `201 Created`** — Returns breakdown object.

**Validation:**
- `machine_id` must belong to the same production line as the run. Otherwise returns `400` with `"Machine does not belong to the same line as the run."`.
- `end_time` must be >= `start_time`.
- If `breakdown_minutes` is `0` and both `start_time`/`end_time` are provided, minutes are auto-calculated from the time difference.
- Run totals are recomputed after adding a breakdown.

### Update Breakdown

```
PATCH /runs/{run_id}/breakdowns/{breakdown_id}/
```

**Permission:** `can_edit_breakdown`

**Request Body (all optional):** `machine_id`, `start_time`, `end_time`, `breakdown_minutes`, `type`, `is_unrecovered`, `reason`, `remarks`

**Response: `200 OK`** — Returns updated breakdown object.

### Delete Breakdown

```
DELETE /runs/{run_id}/breakdowns/{breakdown_id}/
```

**Permission:** `can_edit_breakdown`

**Response: `204 No Content`**

Run totals are recomputed after deletion.

---

## 10. Material Usage (Yield)

### List Materials

```
GET /runs/{run_id}/materials/
```

**Permission:** `can_view_material_usage`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `batch_number` | `integer` | Filter by batch (1, 2, or 3) |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "material_code": "RM-001",
    "material_name": "PET Bottles 1L",
    "opening_qty": "100.000",
    "issued_qty": "500.000",
    "closing_qty": "80.000",
    "wastage_qty": "520.000",
    "uom": "PCS",
    "batch_number": 1,
    "created_at": "2026-03-07T09:00:00+05:30",
    "updated_at": "2026-03-07T09:00:00+05:30"
  }
]
```

**Note:** `wastage_qty` is **read-only** and auto-calculated as: `opening_qty + issued_qty - closing_qty`

### Create Materials

```
POST /runs/{run_id}/materials/
```

**Permission:** `can_create_material_usage`

Accepts a **single object OR an array**.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `material_code` | `string` | No | SAP material code (default: `""`) |
| `material_name` | `string` | Yes | Material name |
| `opening_qty` | `number` | No | Opening quantity (default: `0`, min: `0`) |
| `issued_qty` | `number` | No | Issued quantity (default: `0`, min: `0`) |
| `closing_qty` | `number` | No | Closing quantity (default: `0`, min: `0`) |
| `uom` | `string` | No | Unit of measure (default: `""`) |
| `batch_number` | `integer` | No | Batch 1, 2, or 3 (default: `1`) |

```json
[
  {
    "material_code": "RM-001",
    "material_name": "PET Bottles 1L",
    "opening_qty": 100,
    "issued_qty": 500,
    "closing_qty": 80,
    "uom": "PCS",
    "batch_number": 1
  }
]
```

**Response: `201 Created`** — Returns array of material objects (including auto-calculated `wastage_qty`).

### Update Material

```
PATCH /runs/{run_id}/materials/{material_id}/
```

**Permission:** `can_edit_material_usage`

**Request Body (all optional):**

| Field | Type | Description |
|-------|------|-------------|
| `material_code` | `string` | Updated code |
| `material_name` | `string` | Updated name |
| `opening_qty` | `number` | Updated opening qty |
| `issued_qty` | `number` | Updated issued qty |
| `closing_qty` | `number` | Updated closing qty |
| `uom` | `string` | Updated UOM |
| `batch_number` | `integer` | Updated batch |

**Response: `200 OK`** — Returns updated material object. `wastage_qty` is auto-recalculated.

---

## 11. Machine Runtime

### List Runtime Entries

```
GET /runs/{run_id}/machine-runtime/
```

**Permission:** `can_view_machine_runtime`

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "machine": 3,
    "machine_type": "FILLER",
    "runtime_minutes": 600,
    "downtime_minutes": 30,
    "remarks": "",
    "created_at": "2026-03-07T09:00:00+05:30",
    "updated_at": "2026-03-07T09:00:00+05:30"
  }
]
```

### Bulk Create Runtime

```
POST /runs/{run_id}/machine-runtime/
```

**Permission:** `can_create_machine_runtime`

Accepts a **single object OR an array**.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `machine_id` | `integer` | No | FK to machine (optional) |
| `machine_type` | `string` | Yes | One of `MACHINE_TYPES` values |
| `runtime_minutes` | `integer` | No | Runtime in minutes (default: `0`) |
| `downtime_minutes` | `integer` | No | Downtime in minutes (default: `0`) |
| `remarks` | `string` | No | Remarks (default: `""`) |

```json
[
  {
    "machine_id": 3,
    "machine_type": "FILLER",
    "runtime_minutes": 600,
    "downtime_minutes": 30,
    "remarks": ""
  },
  {
    "machine_type": "CAPPER",
    "runtime_minutes": 580,
    "downtime_minutes": 50
  }
]
```

**Response: `201 Created`** — Returns array of runtime objects.

### Update Runtime

```
PATCH /runs/{run_id}/machine-runtime/{runtime_id}/
```

**Permission:** `can_create_machine_runtime`

**Request Body (all optional):** `machine_type`, `runtime_minutes`, `downtime_minutes`, `remarks`

**Response: `200 OK`** — Returns updated runtime object.

---

## 12. Manpower

### List Manpower Entries

```
GET /runs/{run_id}/manpower/
```

**Permission:** `can_view_manpower`

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "shift": "MORNING",
    "worker_count": 12,
    "supervisor": "Raj Kumar",
    "engineer": "Anil Sharma",
    "remarks": "",
    "created_at": "2026-03-07T07:30:00+05:30",
    "updated_at": "2026-03-07T07:30:00+05:30"
  }
]
```

### Create / Upsert Manpower

```
POST /runs/{run_id}/manpower/
```

**Permission:** `can_create_manpower`

Posting the same `shift` value twice **updates** the existing entry (upsert behavior).

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `shift` | `string` | Yes | `"MORNING"`, `"AFTERNOON"`, or `"NIGHT"` |
| `worker_count` | `integer` | No | Number of workers (default: `0`) |
| `supervisor` | `string` | No | Supervisor name (default: `""`) |
| `engineer` | `string` | No | Engineer name (default: `""`) |
| `remarks` | `string` | No | Remarks (default: `""`) |

```json
{
  "shift": "MORNING",
  "worker_count": 12,
  "supervisor": "Raj Kumar",
  "engineer": "Anil Sharma"
}
```

**Response: `201 Created`** — Returns manpower object.

### Update Manpower

```
PATCH /runs/{run_id}/manpower/{manpower_id}/
```

**Permission:** `can_create_manpower`

**Request Body (all optional):** `shift`, `worker_count`, `supervisor`, `engineer`, `remarks`

**Response: `200 OK`** — Returns updated manpower object.

---

## 13. Line Clearance

### List Clearances

```
GET /line-clearance/
```

**Permission:** `can_view_line_clearance`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `date` | `string` | `YYYY-MM-DD` format |
| `line_id` | `integer` | Filter by line |
| `status` | `string` | `DRAFT`, `SUBMITTED`, `CLEARED`, or `NOT_CLEARED` |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "date": "2026-03-07",
    "line": 1,
    "line_name": "Line-1",
    "sap_doc_entry": 100,
    "document_id": "PRD-OIL-FRM-15-00-00-04",
    "status": "DRAFT",
    "qa_approved": false,
    "created_by": 1,
    "created_at": "2026-03-07T06:00:00+05:30"
  }
]
```

### Create Clearance

```
POST /line-clearance/
```

**Permission:** `can_create_line_clearance`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `date` | `string` | Yes | `YYYY-MM-DD` |
| `line_id` | `integer` | Yes | FK to production line |
| `sap_doc_entry` | `integer` | No | SAP OWOR DocEntry (from SAP production order) |
| `document_id` | `string` | No | Form/document reference (default: `""`) |

```json
{
  "date": "2026-03-07",
  "line_id": 1,
  "sap_doc_entry": 100,
  "document_id": "PRD-OIL-FRM-15-00-00-04"
}
```

**Response: `201 Created`**

9 standard checklist items are **auto-created** with the clearance. The response includes them:

```json
{
  "id": 1,
  "date": "2026-03-07",
  "line": 1,
  "line_name": "Line-1",
  "sap_doc_entry": 100,
  "document_id": "PRD-OIL-FRM-15-00-00-04",
  "verified_by": null,
  "qa_approved": false,
  "qa_approved_by": null,
  "qa_approved_at": null,
  "production_supervisor_sign": "",
  "production_incharge_sign": "",
  "status": "DRAFT",
  "created_by": 1,
  "created_at": "2026-03-07T06:00:00+05:30",
  "updated_at": "2026-03-07T06:00:00+05:30",
  "items": [
    {
      "id": 1,
      "checkpoint": "Previous product, labels and packaging materials removed",
      "sort_order": 1,
      "result": "NA",
      "remarks": ""
    },
    {
      "id": 2,
      "checkpoint": "Machine/equipment cleaned and free from product residues",
      "sort_order": 2,
      "result": "NA",
      "remarks": ""
    }
    // ... 9 items total
  ]
}
```

### Standard Checklist Items (auto-created)

```javascript
const CLEARANCE_CHECKLIST_ITEMS = [
  "Previous product, labels and packaging materials removed",
  "Machine/equipment cleaned and free from product residues",
  "Utensils, scoops and accessories cleaned and available",
  "Packaging area free from previous batch coding material",
  "Work area (tables, conveyors, floor) cleaned and sanitized",
  "Waste bins emptied and cleaned",
  "Required packaging material verified against BOM",
  "Coding machine updated with correct product/batch details",
  "Environmental conditions (temperature/humidity) within limits",
];
```

### Get Clearance Detail

```
GET /line-clearance/{clearance_id}/
```

**Permission:** `can_view_line_clearance`

**Response: `200 OK`** — Returns full clearance object with items (same structure as create response above).

### Update Clearance (DRAFT only)

```
PATCH /line-clearance/{clearance_id}/
```

**Permission:** `can_create_line_clearance`

**Request Body (all optional):**

| Field | Type | Description |
|-------|------|-------------|
| `items` | `array` | Array of item updates (see below) |
| `production_supervisor_sign` | `string` | Supervisor signature |
| `production_incharge_sign` | `string` | Incharge signature |

**Item update object:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | `integer` | Yes | The item ID from the GET response |
| `result` | `string` | Yes | `"YES"`, `"NO"`, or `"NA"` |
| `remarks` | `string` | No | Item-level remarks |

```json
{
  "items": [
    { "id": 1, "result": "YES", "remarks": "" },
    { "id": 2, "result": "YES", "remarks": "" },
    { "id": 3, "result": "NO", "remarks": "Needs cleaning" },
    { "id": 4, "result": "YES", "remarks": "" },
    { "id": 5, "result": "YES", "remarks": "" },
    { "id": 6, "result": "YES", "remarks": "" },
    { "id": 7, "result": "YES", "remarks": "" },
    { "id": 8, "result": "YES", "remarks": "" },
    { "id": 9, "result": "YES", "remarks": "" }
  ],
  "production_supervisor_sign": "Raj Kumar",
  "production_incharge_sign": "Suresh Patel"
}
```

**Response: `200 OK`** — Returns updated clearance with items.

**Business Rules:** Only `DRAFT` clearances can be edited. Returns `400` otherwise.

### Submit Clearance

```
POST /line-clearance/{clearance_id}/submit/
```

**Permission:** `can_create_line_clearance`

**Request Body:** None (empty body or `{}`)

**Response: `200 OK`** — Returns clearance with status changed to `SUBMITTED`.

**Validation (returns `400` if not met):**
- All 9 items must have `result` set to `YES` or `NO` (not `NA`).
- At least one signature (`production_supervisor_sign` OR `production_incharge_sign`) must be provided.
- Clearance must be in `DRAFT` status.

### Approve / Reject Clearance (QA)

```
POST /line-clearance/{clearance_id}/approve/
```

**Permission:** `can_approve_line_clearance_qa`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approved` | `boolean` | Yes | `true` = CLEARED, `false` = NOT_CLEARED |

```json
{ "approved": true }
```

**Response: `200 OK`** — Returns clearance with updated status, `qa_approved`, `qa_approved_by`, `qa_approved_at`.

**Business Rules:** Only `SUBMITTED` clearances can be approved/rejected.

### Clearance Status Flow

```
DRAFT  ──[submit]──>  SUBMITTED  ──[approve: true]──>   CLEARED
                                  ──[approve: false]──>  NOT_CLEARED
```

---

## 14. Machine Checklists

### List Checklist Entries

```
GET /machine-checklists/
```

**Permission:** `can_view_machine_checklist`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `machine_id` | `integer` | Filter by machine |
| `month` | `integer` | Filter by month (1-12) |
| `year` | `integer` | Filter by year (e.g., 2026) |
| `frequency` | `string` | `DAILY`, `WEEKLY`, or `MONTHLY` |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "machine": 3,
    "machine_name": "Filler Machine A",
    "machine_type": "FILLER",
    "date": "2026-03-07",
    "month": 3,
    "year": 2026,
    "template": 1,
    "task_description": "Check oil level",
    "frequency": "DAILY",
    "status": "OK",
    "operator": "Ravi",
    "shift_incharge": "Raj",
    "remarks": "",
    "created_at": "2026-03-07T08:00:00+05:30",
    "updated_at": "2026-03-07T08:00:00+05:30"
  }
]
```

### Create Single Entry

```
POST /machine-checklists/
```

**Permission:** `can_create_machine_checklist`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `machine_id` | `integer` | Yes | FK to machine |
| `template_id` | `integer` | Yes | FK to checklist template |
| `date` | `string` | Yes | `YYYY-MM-DD` |
| `status` | `string` | No | `"OK"`, `"NOT_OK"`, or `"NA"` (default: `"NA"`) |
| `operator` | `string` | No | Operator name (default: `""`) |
| `shift_incharge` | `string` | No | Shift incharge name (default: `""`) |
| `remarks` | `string` | No | Remarks (default: `""`) |

```json
{
  "machine_id": 3,
  "template_id": 1,
  "date": "2026-03-07",
  "status": "OK",
  "operator": "Ravi",
  "shift_incharge": "Raj"
}
```

**Response: `201 Created`** — Returns checklist entry object.

**Note:** `task_description`, `frequency`, `machine_type`, `month`, `year` are auto-populated from the template and date. No need to send them.

### Bulk Create/Update Entries

```
POST /machine-checklists/bulk/
```

**Permission:** `can_create_machine_checklist`

**Request Body:** Array of entry objects (same fields as single create).

Entries are **upserted** by unique key `(machine, template, date)` — if an entry already exists for that combination, it gets updated.

```json
[
  {
    "machine_id": 3,
    "template_id": 1,
    "date": "2026-03-07",
    "status": "OK",
    "operator": "Ravi"
  },
  {
    "machine_id": 3,
    "template_id": 2,
    "date": "2026-03-07",
    "status": "NOT_OK",
    "operator": "Ravi",
    "remarks": "Needs repair"
  }
]
```

**Response: `201 Created`** — Returns array of entry objects.

### Update Single Entry

```
PATCH /machine-checklists/{entry_id}/
```

**Permission:** `can_create_machine_checklist`

**Request Body (all optional):** `status`, `operator`, `shift_incharge`, `remarks`

**Response: `200 OK`** — Returns updated entry object.

---

## 15. Waste Management

### List Waste Logs

```
GET /waste/
```

**Permission:** `can_view_waste_log`

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `run_id` | `integer` | Filter by production run |
| `approval_status` | `string` | `PENDING`, `PARTIALLY_APPROVED`, or `FULLY_APPROVED` |

**Response: `200 OK`**

```json
[
  {
    "id": 1,
    "production_run": 1,
    "material_code": "RM-001",
    "material_name": "PET Bottles 1L",
    "wastage_qty": "25.000",
    "uom": "PCS",
    "reason": "Damaged during filling",
    "engineer_sign": "",
    "engineer_signed_by": null,
    "engineer_signed_at": null,
    "am_sign": "",
    "am_signed_by": null,
    "am_signed_at": null,
    "store_sign": "",
    "store_signed_by": null,
    "store_signed_at": null,
    "hod_sign": "",
    "hod_signed_by": null,
    "hod_signed_at": null,
    "wastage_approval_status": "PENDING",
    "created_at": "2026-03-07T15:00:00+05:30",
    "updated_at": "2026-03-07T15:00:00+05:30"
  }
]
```

### Create Waste Log

```
POST /waste/
```

**Permission:** `can_create_waste_log`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `production_run_id` | `integer` | Yes | FK to production run |
| `material_code` | `string` | No | SAP material code (default: `""`) |
| `material_name` | `string` | Yes | Material name |
| `wastage_qty` | `number` | Yes | Wastage quantity (decimal, 3 places) |
| `uom` | `string` | No | Unit of measure (default: `""`) |
| `reason` | `string` | No | Reason for wastage (default: `""`) |

```json
{
  "production_run_id": 1,
  "material_code": "RM-001",
  "material_name": "PET Bottles 1L",
  "wastage_qty": 25.0,
  "uom": "PCS",
  "reason": "Damaged during filling"
}
```

**Response: `201 Created`** — Returns waste log object with `wastage_approval_status: "PENDING"`.

### Get Waste Log Detail

```
GET /waste/{waste_id}/
```

**Permission:** `can_view_waste_log`

**Response: `200 OK`** — Returns full waste log object.

### Approve Waste — Engineer (Step 1)

```
POST /waste/{waste_id}/approve/engineer/
```

**Permission:** `can_approve_waste_engineer`

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sign` | `string` | Yes | Approver's name/signature |
| `remarks` | `string` | No | Optional remarks |

```json
{
  "sign": "Anil Sharma"
}
```

**Response: `200 OK`** — Returns waste log with `engineer_sign`, `engineer_signed_by`, `engineer_signed_at` populated and `wastage_approval_status: "PARTIALLY_APPROVED"`.

### Approve Waste — AM (Step 2)

```
POST /waste/{waste_id}/approve/am/
```

**Permission:** `can_approve_waste_am`

**Request Body:** Same as engineer (`{ "sign": "Name" }`)

**Prerequisite:** Engineer must have signed first. Returns `400` with `"Engineer must sign before AM."` if not.

**Response: `200 OK`** — Returns waste log with AM fields populated.

### Approve Waste — Store (Step 3)

```
POST /waste/{waste_id}/approve/store/
```

**Permission:** `can_approve_waste_store`

**Request Body:** Same as above.

**Prerequisite:** AM must have signed first.

**Response: `200 OK`** — Returns waste log with Store fields populated.

### Approve Waste — HOD (Step 4 — Final)

```
POST /waste/{waste_id}/approve/hod/
```

**Permission:** `can_approve_waste_hod`

**Request Body:** Same as above.

**Prerequisite:** Store must have signed first.

**Response: `200 OK`** — Returns waste log with `wastage_approval_status: "FULLY_APPROVED"`.

### Waste Approval Flow

```
PENDING
  │
  ├─ POST /approve/engineer/  ──>  PARTIALLY_APPROVED
  │                                    │
  │                                    ├─ POST /approve/am/
  │                                    │        │
  │                                    │        ├─ POST /approve/store/
  │                                    │        │        │
  │                                    │        │        ├─ POST /approve/hod/  ──>  FULLY_APPROVED
```

**Rules:**
- Sequential only — cannot skip levels
- Each level requires the previous level to be signed
- `wastage_approval_status` changes to `PARTIALLY_APPROVED` after engineer signs
- `wastage_approval_status` changes to `FULLY_APPROVED` only after HOD signs

### UI Hint — Show Approval Progress

```javascript
function getApprovalProgress(waste) {
  const steps = [
    { label: "Engineer", signed: !!waste.engineer_signed_at, sign: waste.engineer_sign },
    { label: "AM",       signed: !!waste.am_signed_at,       sign: waste.am_sign },
    { label: "Store",    signed: !!waste.store_signed_at,     sign: waste.store_sign },
    { label: "HOD",      signed: !!waste.hod_signed_at,      sign: waste.hod_sign },
  ];
  const currentStep = steps.findIndex(s => !s.signed);
  return { steps, currentStep };
}
```

---

## 16. Reports

### Daily Production Report

```
GET /reports/daily-production/?date=2026-03-07
```

**Permission:** `can_view_reports`

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `date` | `string` | **Yes** | `YYYY-MM-DD` format |
| `line_id` | `integer` | No | Filter by line |

**Response: `200 OK`** — Returns array of full run detail objects (same as Get Run Detail, including `logs` and `breakdowns`).

```json
[
  {
    "id": 1,
    "sap_doc_entry": 100,
    "run_number": 1,
    "date": "2026-03-07",
    "line": 1,
    "line_name": "Line-1",
    "brand": "Extra Light",
    "pack": "1L x 12",
    "total_production": 1200,
    "total_minutes_pe": 660,
    "total_minutes_me": 660,
    "total_breakdown_time": 35,
    "line_breakdown_time": 20,
    "external_breakdown_time": 15,
    "unrecorded_time": 25,
    "status": "COMPLETED",
    "logs": [...],
    "breakdowns": [...]
  }
]
```

**Error if `date` is missing:** `400` with `{"detail": "date query parameter is required."}`

### Yield Report

```
GET /reports/yield/{run_id}/
```

**Permission:** `can_view_reports`

**Response: `200 OK`**

```json
{
  "run": {
    "id": 1,
    "sap_doc_entry": 100,
    "run_number": 1,
    "date": "2026-03-07",
    "line": 1,
    "line_name": "Line-1",
    "brand": "Extra Light",
    "pack": "1L x 12",
    "sap_order_no": "PO-12345",
    "rated_speed": "150.00",
    "total_production": 1200,
    "total_breakdown_time": 35,
    "status": "COMPLETED",
    "created_by": 1,
    "created_at": "2026-03-07T07:00:00+05:30"
  },
  "materials": [
    {
      "id": 1,
      "material_code": "RM-001",
      "material_name": "PET Bottles 1L",
      "opening_qty": "100.000",
      "issued_qty": "500.000",
      "closing_qty": "80.000",
      "wastage_qty": "520.000",
      "uom": "PCS",
      "batch_number": 1
    }
  ],
  "machine_runtimes": [
    {
      "id": 1,
      "machine": 3,
      "machine_type": "FILLER",
      "runtime_minutes": 600,
      "downtime_minutes": 30,
      "remarks": ""
    }
  ],
  "manpower": [
    {
      "id": 1,
      "shift": "MORNING",
      "worker_count": 12,
      "supervisor": "Raj Kumar",
      "engineer": "Anil Sharma",
      "remarks": ""
    }
  ]
}
```

### Line Clearance Report

```
GET /reports/line-clearance/
```

**Permission:** `can_view_reports`

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `date_from` | `string` | No | Start date `YYYY-MM-DD` |
| `date_to` | `string` | No | End date `YYYY-MM-DD` |

**Response: `200 OK`** — Returns array of clearance list objects.

```json
[
  {
    "id": 1,
    "date": "2026-03-07",
    "line": 1,
    "line_name": "Line-1",
    "sap_doc_entry": 100,
    "document_id": "PRD-OIL-FRM-15-00-00-04",
    "status": "CLEARED",
    "qa_approved": true,
    "created_by": 1,
    "created_at": "2026-03-07T06:00:00+05:30"
  }
]
```

### Analytics Dashboard

```
GET /reports/analytics/
```

**Permission:** `can_view_reports`

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `date_from` | `string` | No | Start date `YYYY-MM-DD` |
| `date_to` | `string` | No | End date `YYYY-MM-DD` |
| `line_id` | `integer` | No | Filter by line |

**Response: `200 OK`**

```json
{
  "total_runs": 10,
  "total_production": 5000,
  "total_pe_minutes": 4800,
  "total_breakdown_minutes": 200,
  "total_line_breakdown_minutes": 120,
  "total_external_breakdown_minutes": 80,
  "available_time_minutes": 7200,
  "operating_time_minutes": 7000,
  "availability_percent": 97.2
}
```

**Calculations explained:**
- `available_time_minutes` = `total_runs * 720` (12 hours per run)
- `operating_time_minutes` = `available_time_minutes - total_breakdown_minutes`
- `availability_percent` = `(operating_time / available_time) * 100`, rounded to 1 decimal

Only `COMPLETED` runs are included in analytics.

---

## 17. Workflows & State Diagrams

### Production Run Lifecycle

```
┌─────────┐       ┌──────────────┐       ┌───────────┐
│  DRAFT  │ ───>  │ IN_PROGRESS  │ ───>  │ COMPLETED │
└─────────┘       └──────────────┘       └───────────┘
   Created          Auto on first          POST /complete/
   via POST         edit/log entry         Locks the run
```

**When a run is COMPLETED:**
- No logs, breakdowns, materials, runtime, or manpower can be added/edited
- All totals are frozen
- This is irreversible

### Line Clearance Lifecycle

```
┌─────────┐       ┌───────────┐       ┌─────────┐
│  DRAFT  │ ───>  │ SUBMITTED │ ───>  │ CLEARED │
└─────────┘       └───────────┘       └─────────┘
 Fill items +        POST               POST /approve/
 signatures          /submit/            { approved: true }
                                              OR
                                     ┌─────────────┐
                                     │ NOT_CLEARED  │
                                     └─────────────┘
                                      { approved: false }
```

### Waste Approval Chain

```
┌─────────┐     ┌────────┐     ┌────┐     ┌───────┐     ┌─────┐     ┌────────────────┐
│ PENDING │ ──> │Engineer│ ──> │ AM │ ──> │ Store │ ──> │ HOD │ ──> │ FULLY_APPROVED │
└─────────┘     └────────┘     └────┘     └───────┘     └─────┘     └────────────────┘
                 PARTIALLY_APPROVED ──────────────────────────────>
```

---

## 18. Pre-defined Constants

### Permissions Reference

For checking user capabilities on the frontend:

```javascript
const PERMISSIONS = {
  // Master Data
  MANAGE_LINES:      "production_execution.can_manage_production_lines",
  MANAGE_MACHINES:   "production_execution.can_manage_machines",
  MANAGE_TEMPLATES:  "production_execution.can_manage_checklist_templates",

  // Production Runs
  VIEW_RUN:          "production_execution.can_view_production_run",
  CREATE_RUN:        "production_execution.can_create_production_run",
  EDIT_RUN:          "production_execution.can_edit_production_run",
  COMPLETE_RUN:      "production_execution.can_complete_production_run",

  // Hourly Logs
  VIEW_LOG:          "production_execution.can_view_production_log",
  EDIT_LOG:          "production_execution.can_edit_production_log",

  // Breakdowns
  VIEW_BREAKDOWN:    "production_execution.can_view_breakdown",
  CREATE_BREAKDOWN:  "production_execution.can_create_breakdown",
  EDIT_BREAKDOWN:    "production_execution.can_edit_breakdown",

  // Material Usage
  VIEW_MATERIAL:     "production_execution.can_view_material_usage",
  CREATE_MATERIAL:   "production_execution.can_create_material_usage",
  EDIT_MATERIAL:     "production_execution.can_edit_material_usage",

  // Machine Runtime
  VIEW_RUNTIME:      "production_execution.can_view_machine_runtime",
  CREATE_RUNTIME:    "production_execution.can_create_machine_runtime",

  // Manpower
  VIEW_MANPOWER:     "production_execution.can_view_manpower",
  CREATE_MANPOWER:   "production_execution.can_create_manpower",

  // Line Clearance
  VIEW_CLEARANCE:    "production_execution.can_view_line_clearance",
  CREATE_CLEARANCE:  "production_execution.can_create_line_clearance",
  APPROVE_CLEARANCE: "production_execution.can_approve_line_clearance_qa",

  // Machine Checklists
  VIEW_CHECKLIST:    "production_execution.can_view_machine_checklist",
  CREATE_CHECKLIST:  "production_execution.can_create_machine_checklist",

  // Waste
  VIEW_WASTE:        "production_execution.can_view_waste_log",
  CREATE_WASTE:      "production_execution.can_create_waste_log",
  APPROVE_ENGINEER:  "production_execution.can_approve_waste_engineer",
  APPROVE_AM:        "production_execution.can_approve_waste_am",
  APPROVE_STORE:     "production_execution.can_approve_waste_store",
  APPROVE_HOD:       "production_execution.can_approve_waste_hod",

  // Reports
  VIEW_REPORTS:      "production_execution.can_view_reports",
};
```

### URL Quick Reference

| Section | Method | URL |
|---------|--------|-----|
| **Lines** | GET/POST | `/lines/` |
| | PATCH/DELETE | `/lines/{id}/` |
| **Machines** | GET/POST | `/machines/` |
| | PATCH/DELETE | `/machines/{id}/` |
| **Templates** | GET/POST | `/checklist-templates/` |
| | PATCH/DELETE | `/checklist-templates/{id}/` |
| **Runs** | GET/POST | `/runs/` |
| | GET/PATCH | `/runs/{id}/` |
| | POST | `/runs/{id}/complete/` |
| **Logs** | GET/POST | `/runs/{id}/logs/` |
| | PATCH | `/runs/{id}/logs/{log_id}/` |
| **Breakdowns** | GET/POST | `/runs/{id}/breakdowns/` |
| | PATCH/DELETE | `/runs/{id}/breakdowns/{bd_id}/` |
| **Materials** | GET/POST | `/runs/{id}/materials/` |
| | PATCH | `/runs/{id}/materials/{mat_id}/` |
| **Runtime** | GET/POST | `/runs/{id}/machine-runtime/` |
| | PATCH | `/runs/{id}/machine-runtime/{rt_id}/` |
| **Manpower** | GET/POST | `/runs/{id}/manpower/` |
| | PATCH | `/runs/{id}/manpower/{mp_id}/` |
| **Clearance** | GET/POST | `/line-clearance/` |
| | GET/PATCH | `/line-clearance/{id}/` |
| | POST | `/line-clearance/{id}/submit/` |
| | POST | `/line-clearance/{id}/approve/` |
| **Checklists** | GET/POST | `/machine-checklists/` |
| | POST | `/machine-checklists/bulk/` |
| | PATCH | `/machine-checklists/{id}/` |
| **Waste** | GET/POST | `/waste/` |
| | GET | `/waste/{id}/` |
| | POST | `/waste/{id}/approve/engineer/` |
| | POST | `/waste/{id}/approve/am/` |
| | POST | `/waste/{id}/approve/store/` |
| | POST | `/waste/{id}/approve/hod/` |
| **Reports** | GET | `/reports/daily-production/?date=` |
| | GET | `/reports/yield/{run_id}/` |
| | GET | `/reports/line-clearance/` |
| | GET | `/reports/analytics/` |
| **SAP BOM** | GET | `/sap/bom/?item_code={ItemCode}` |

> All URLs are relative to the base URL: `/api/v1/production-execution/`
>
> **Note:** When creating a production run without a `materials` array, the system auto-fetches BOM components from SAP. See [BOM Auto-Fetch Guide](bom_auto_fetch_guide.md) for details.
