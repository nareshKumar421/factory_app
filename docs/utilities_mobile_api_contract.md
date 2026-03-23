# Utilities Mobile — API Contract (Frontend Reference)

Complete API reference for the frontend/mobile team to integrate with the new Django endpoints.

---

## Common Headers (All Requests)

```
Authorization: Bearer <jwt_access_token>
Company-Code: JIVO_OIL
Content-Type: application/json
```

## Common Error Responses

| Status | Meaning | Body |
|---|---|---|
| 400 | Validation error / business rule violation | `{"error": "message"}` or `{"field": ["error"]}` |
| 401 | Missing/invalid/expired JWT token | `{"detail": "..."}` |
| 403 | User doesn't belong to company or lacks permission | `{"detail": "..."}` |
| 404 | Resource not found | `{"detail": "Not found."}` |
| 429 | Rate limited (50/hr anon, 500/hr auth) | `{"detail": "Request was throttled."}` |

---

## 1. Docking APIs

**Base:** `/api/v1/docking/`

### 1.1 List Docking Entries

```
GET /api/v1/docking/entries/
```

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `status` | string | Filter: DRAFT, DOCKED, INVOICE_VERIFIED, COMPLETED, CANCELLED |
| `entry_type` | string | Filter: INWARD, OUTWARD, EMPTY_TRUCK |
| `date_from` | date | Filter: created_at >= (YYYY-MM-DD) |
| `date_to` | date | Filter: created_at <= (YYYY-MM-DD) |

**Response:** `200 OK`

```json
[
  {
    "id": 42,
    "entry_number": "DOCK-JIVO_OIL-20260323-001",
    "vehicle_number": "UP32AT1234",
    "driver_name": "Ramesh Kumar",
    "transporter_name": "ABC Transport",
    "entry_type": "INWARD",
    "status": "DOCKED",
    "dock_in_time": "2026-03-23T09:15:00Z",
    "dock_out_time": null,
    "is_locked": false,
    "invoice_count": 1,
    "photo_count": 2,
    "created_by_name": "Naresh Kumar",
    "created_at": "2026-03-23T09:10:00Z"
  }
]
```

### 1.2 Create Docking Entry

```
POST /api/v1/docking/entries/
```

**Request:**

```json
{
  "vehicle_number": "UP32AT1234",
  "driver_name": "Ramesh Kumar",
  "driver_contact": "9876543210",
  "transporter_name": "ABC Transport",
  "entry_type": "INWARD",
  "remarks": ""
}
```

**Response:** `201 Created` — Full entry detail (same as 1.3)

### 1.3 Get Docking Entry Detail

```
GET /api/v1/docking/entries/{id}/
```

**Response:** `200 OK`

```json
{
  "id": 42,
  "entry_number": "DOCK-JIVO_OIL-20260323-001",
  "vehicle_number": "UP32AT1234",
  "driver_name": "Ramesh Kumar",
  "driver_contact": "9876543210",
  "transporter_name": "ABC Transport",
  "entry_type": "INWARD",
  "status": "DOCKED",
  "dock_in_time": "2026-03-23T09:15:00Z",
  "dock_out_time": null,
  "remarks": "",
  "is_locked": false,
  "invoices": [
    {
      "id": 1,
      "invoice_number": "INV-2026-001",
      "invoice_date": "2026-03-23",
      "supplier_code": "V001",
      "supplier_name": "ABC Suppliers",
      "sap_doc_entry": 1001,
      "total_amount": "50000.00",
      "is_verified": true,
      "verified_by": 5,
      "verified_by_name": "Khush Patel",
      "verified_at": "2026-03-23T10:30:00Z",
      "remarks": "",
      "created_at": "2026-03-23T09:45:00Z"
    }
  ],
  "photos": [
    {
      "id": 1,
      "image": "/media/docking/photos/2026/03/23/dock_a1b2c3d4.jpeg",
      "photo_type": "VEHICLE",
      "caption": "",
      "created_at": "2026-03-23T09:20:00Z"
    }
  ],
  "created_by_name": "Naresh Kumar",
  "created_at": "2026-03-23T09:10:00Z",
  "updated_at": "2026-03-23T10:30:00Z"
}
```

### 1.4 Dock In

```
POST /api/v1/docking/entries/{id}/dock-in/
```

**Request:** Empty body `{}`

**Response:** `200 OK` — Full entry detail with `dock_in_time` set and `status: "DOCKED"`

**Errors:**
- `400` — "Entry is locked."
- `400` — "Already docked in."

### 1.5 Complete Docking (Dock Out)

```
POST /api/v1/docking/entries/{id}/complete/
```

**Request:** Empty body `{}`

**Response:** `200 OK` — Full entry detail with `status: "COMPLETED"`, `is_locked: true`

**Errors:**
- `400` — `{"errors": ["Dock-in time not recorded.", "At least one invoice is required."]}`

### 1.6 Add Invoice

```
POST /api/v1/docking/entries/{id}/invoices/
```

**Request:**

```json
{
  "invoice_number": "INV-2026-001",
  "invoice_date": "2026-03-23",
  "supplier_code": "V001",
  "supplier_name": "ABC Suppliers",
  "total_amount": 50000.00,
  "remarks": ""
}
```

**Response:** `201 Created` — Invoice object

### 1.7 Verify Invoice (Against SAP)

```
POST /api/v1/docking/entries/{id}/invoices/{invoice_id}/verify/
```

**Request:** Empty body `{}`

**Response:** `200 OK` — Invoice with `is_verified: true`, SAP data populated

**Errors:**
- `404` — "Invoice not found in SAP."

### 1.8 Upload Photo

```
POST /api/v1/docking/entries/{id}/photos/
```

**Option A — Base64 (JSON):**

```json
{
  "image_base64": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
  "photo_type": "VEHICLE",
  "caption": "Front view"
}
```

**Option B — Multipart:**

```
Content-Type: multipart/form-data

image: <file>
photo_type: VEHICLE
caption: Front view
```

**Response:** `201 Created`

```json
{
  "id": 1,
  "image": "/media/docking/photos/2026/03/23/dock_a1b2c3d4.jpeg",
  "photo_type": "VEHICLE",
  "caption": "Front view",
  "created_at": "2026-03-23T09:20:00Z"
}
```

### 1.9 SAP Invoice Lookup

```
GET /api/v1/docking/sap/invoice-lookup/?invoice_number=INV-2026-001
```

**Response:** `200 OK`

```json
{
  "doc_entry": 1001,
  "supplier_code": "V001",
  "supplier_name": "ABC Suppliers",
  "total_amount": 50000.00,
  "doc_date": "2026-03-23",
  "invoice_number": "INV-2026-001"
}
```

---

## 2. Dynamic Forms APIs

**Base:** `/api/v1/dynamic-forms/`

### 2.1 List Accessible Forms

```
GET /api/v1/dynamic-forms/forms/
```

**Response:** `200 OK`

```json
[
  {
    "id": 10,
    "title": "Material Request",
    "description": "Request materials from warehouse",
    "is_published": true,
    "requires_approval": true,
    "field_count": 4,
    "submission_count": 25,
    "created_at": "2026-01-15T08:00:00Z"
  }
]
```

### 2.2 Get Form with Fields

```
GET /api/v1/dynamic-forms/forms/{id}/
```

**Response:** `200 OK`

```json
{
  "id": 10,
  "title": "Material Request",
  "description": "Request materials from warehouse",
  "is_published": true,
  "requires_approval": true,
  "fields": [
    {
      "id": 1,
      "label": "Item Name",
      "field_type": "TEXT",
      "is_required": true,
      "order": 0,
      "options": [],
      "validation_rules": {"max_length": 200}
    },
    {
      "id": 2,
      "label": "Quantity",
      "field_type": "INTEGER",
      "is_required": true,
      "order": 1,
      "options": [],
      "validation_rules": {"min": 1, "max": 999}
    },
    {
      "id": 3,
      "label": "Photo",
      "field_type": "IMAGE",
      "is_required": false,
      "order": 2,
      "options": [],
      "validation_rules": {}
    },
    {
      "id": 4,
      "label": "Priority",
      "field_type": "SELECT",
      "is_required": true,
      "order": 3,
      "options": ["Low", "Medium", "High"],
      "validation_rules": {}
    }
  ],
  "created_at": "2026-01-15T08:00:00Z",
  "updated_at": "2026-01-15T08:00:00Z"
}
```

### 2.3 Submit Form

```
POST /api/v1/dynamic-forms/forms/{id}/submit/
```

**Request:**

```json
{
  "responses": [
    {"field_id": 1, "value": "Steel Pipe"},
    {"field_id": 2, "value": "50"},
    {"field_id": 3, "image_base64": "data:image/jpeg;base64,..."},
    {"field_id": 4, "value": "High"}
  ]
}
```

**Response:** `201 Created`

```json
{
  "id": 55,
  "form": 10,
  "form_title": "Material Request",
  "submitted_by": 3,
  "submitted_by_name": "Naresh Kumar",
  "status": "PENDING",
  "approved_by": null,
  "approved_by_name": "",
  "approved_at": null,
  "rejection_reason": "",
  "is_closed": false,
  "closed_at": null,
  "responses": [
    {
      "id": 101,
      "field": 1,
      "field_label": "Item Name",
      "field_type": "TEXT",
      "value": "Steel Pipe",
      "image": null,
      "file": null
    },
    {
      "id": 102,
      "field": 2,
      "field_label": "Quantity",
      "field_type": "INTEGER",
      "value": "50",
      "image": null,
      "file": null
    },
    {
      "id": 103,
      "field": 3,
      "field_label": "Photo",
      "field_type": "IMAGE",
      "value": "",
      "image": "/media/forms/responses/2026/03/23/form_a1b2c3.jpeg",
      "file": null
    },
    {
      "id": 104,
      "field": 4,
      "field_label": "Priority",
      "field_type": "SELECT",
      "value": "High",
      "image": null,
      "file": null
    }
  ],
  "created_at": "2026-03-23T11:00:00Z",
  "updated_at": "2026-03-23T11:00:00Z"
}
```

### 2.4 List Submissions

```
GET /api/v1/dynamic-forms/submissions/?status=PENDING&form={form_id}
```

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `status` | string | PENDING, APPROVED, REJECTED, CLOSED |
| `form` | int | Filter by form ID |
| `date_from` | date | YYYY-MM-DD |
| `date_to` | date | YYYY-MM-DD |

**Response:** `200 OK` — Array of submission list objects

### 2.5 Approve Submission

```
POST /api/v1/dynamic-forms/submissions/{id}/approve/
```

**Request:** Empty body `{}`

**Response:** `200 OK` — Full submission detail with `status: "APPROVED"`

### 2.6 Reject Submission

```
POST /api/v1/dynamic-forms/submissions/{id}/reject/
```

**Request:**

```json
{
  "rejection_reason": "Quantity exceeds budget allocation"
}
```

**Response:** `200 OK` — Full submission detail with `status: "REJECTED"`

### 2.7 Close Submission

```
POST /api/v1/dynamic-forms/submissions/{id}/close/
```

**Request:** Empty body `{}`

**Response:** `200 OK` — Submission with `is_closed: true`, `status: "CLOSED"`

### 2.8 Pending Approvals (Dashboard)

```
GET /api/v1/dynamic-forms/pending-approvals/
```

Returns only submissions for forms where the current user has `can_approve=true`.

**Response:** `200 OK` — Array of pending submission list objects

---

## 3. Material Tracking APIs

**Base:** `/api/v1/material-tracking/`

### 3.1 Create Material Movement

```
POST /api/v1/material-tracking/movements/
```

**Request:**

```json
{
  "movement_type": "OUTWARD",
  "from_location": "Factory A - Store",
  "to_location": "Warehouse B",
  "vehicle_number": "UP32AT1234",
  "driver_name": "Ramesh",
  "driver_contact": "9876543210",
  "expected_return_date": "2026-04-05",
  "remarks": "Monthly supply",
  "items": [
    {
      "material_name": "Steel Rod",
      "material_code": "SR001",
      "quantity": 100,
      "uom": "KG",
      "remarks": ""
    },
    {
      "material_name": "Copper Wire",
      "material_code": "CW002",
      "quantity": 50,
      "uom": "MTR",
      "remarks": ""
    }
  ]
}
```

**Response:** `201 Created`

```json
{
  "id": 42,
  "movement_number": "MOV-JIVO_OIL-20260323-001",
  "movement_type": "OUTWARD",
  "status": "DISPATCHED",
  "from_location": "Factory A - Store",
  "to_location": "Warehouse B",
  "vehicle_number": "UP32AT1234",
  "driver_name": "Ramesh",
  "driver_contact": "9876543210",
  "dispatched_by": 3,
  "dispatched_by_name": "Naresh Kumar",
  "dispatched_at": "2026-03-23T12:00:00Z",
  "expected_return_date": "2026-04-05",
  "received_back": false,
  "received_by": null,
  "received_at": null,
  "remarks": "Monthly supply",
  "is_locked": false,
  "items": [
    {
      "id": 1,
      "material_name": "Steel Rod",
      "material_code": "SR001",
      "quantity": "100.000",
      "uom": "KG",
      "received_quantity": "0.000",
      "remarks": ""
    },
    {
      "id": 2,
      "material_name": "Copper Wire",
      "material_code": "CW002",
      "quantity": "50.000",
      "uom": "MTR",
      "received_quantity": "0.000",
      "remarks": ""
    }
  ],
  "created_at": "2026-03-23T12:00:00Z"
}
```

### 3.2 List Movements

```
GET /api/v1/material-tracking/movements/?status=DISPATCHED&movement_type=OUTWARD
```

**Query Parameters:**

| Param | Type | Description |
|---|---|---|
| `status` | string | DISPATCHED, IN_TRANSIT, RECEIVED, PARTIALLY_RECEIVED, OVERDUE, CLOSED |
| `movement_type` | string | OUTWARD, RETURN, TRANSFER |
| `date_from` | date | YYYY-MM-DD |
| `date_to` | date | YYYY-MM-DD |

### 3.3 Receive Materials

```
POST /api/v1/material-tracking/movements/{id}/receive/
```

**Request:**

```json
{
  "items": [
    {"item_id": 1, "received_quantity": 95},
    {"item_id": 2, "received_quantity": 50}
  ]
}
```

**Response:** `200 OK` — Movement detail with updated quantities and status

**Status Logic:**
- All items fully received → `RECEIVED`
- Some items partially received → `PARTIALLY_RECEIVED`

### 3.4 Close Movement

```
POST /api/v1/material-tracking/movements/{id}/close/
```

**Response:** `200 OK` — Movement with `status: "CLOSED"`, `is_locked: true`

### 3.5 Overdue Movements

```
GET /api/v1/material-tracking/movements/overdue/
```

Returns movements where `expected_return_date < today` and not yet closed.

### 3.6 Create Gate Pass

```
POST /api/v1/material-tracking/gate-passes/
```

**Request:**

```json
{
  "pass_type": "RETURNABLE",
  "party_name": "ABC Ltd",
  "party_contact": "9876543210",
  "vehicle_number": "UP32AT1234",
  "movement_id": 42,
  "remarks": "",
  "items": [
    {
      "material_name": "Steel Rod",
      "material_code": "SR001",
      "quantity": 100,
      "uom": "KG",
      "value": 25000
    }
  ]
}
```

**Response:** `201 Created` — Gate pass with `status: "PENDING_APPROVAL"`

### 3.7 Approve Gate Pass

```
POST /api/v1/material-tracking/gate-passes/{id}/approve/
```

**Response:** `200 OK` — Gate pass with `status: "APPROVED"`

### 3.8 Reject Gate Pass

```
POST /api/v1/material-tracking/gate-passes/{id}/reject/
```

**Request:**

```json
{
  "rejection_reason": "Items not approved for transfer"
}
```

### 3.9 Print Gate Pass

```
GET /api/v1/material-tracking/gate-passes/{id}/print/
```

**Response:** `200 OK` — HTML or PDF content for printing

```
Content-Type: text/html (or application/pdf)
```

---

## 4. Reporting APIs

**Base:** `/api/v1/reporting/`

### 4.1 List Available Reports

```
GET /api/v1/reporting/reports/
```

**Response:** `200 OK`

```json
[
  {
    "id": 5,
    "name": "Daily Material Out",
    "description": "Summary of materials dispatched",
    "category": "MATERIAL",
    "parameters": [
      {"name": "start_date", "type": "date", "required": true, "label": "Start Date"},
      {"name": "end_date", "type": "date", "required": true, "label": "End Date"}
    ],
    "is_published": true,
    "created_at": "2026-01-10T08:00:00Z"
  }
]
```

### 4.2 Execute Report

```
POST /api/v1/reporting/reports/{id}/execute/
```

**Request:**

```json
{
  "start_date": "2026-03-01",
  "end_date": "2026-03-23"
}
```

**Response:** `200 OK`

```json
{
  "columns": [
    {"key": "date", "label": "Date"},
    {"key": "material_name", "label": "Material"},
    {"key": "quantity", "label": "Qty"},
    {"key": "uom", "label": "UOM"},
    {"key": "destination", "label": "Destination"}
  ],
  "rows": [
    {
      "date": "2026-03-23",
      "material_name": "Steel Rod",
      "quantity": 100,
      "uom": "KG",
      "destination": "Warehouse B"
    }
  ],
  "total_count": 150,
  "execution_time_ms": 45
}
```

### 4.3 Export Report (Excel)

```
POST /api/v1/reporting/reports/{id}/export/
```

**Request:** Same as execute

**Response:** `200 OK` — File download

```
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="Daily_Material_Out_20260323.xlsx"
```

### 4.4 List Scheduled Reports

```
GET /api/v1/reporting/schedules/
```

**Response:** `200 OK`

```json
[
  {
    "id": 1,
    "report": 5,
    "report_name": "Daily Material Out",
    "schedule_type": "HALF_MONTHLY",
    "parameters": {"start_date": "auto", "end_date": "auto"},
    "is_active": true,
    "last_run_at": "2026-03-16T08:00:00Z",
    "next_run_at": "2026-04-01T08:00:00Z",
    "recipient_count": 3
  }
]
```

### 4.5 Create Schedule

```
POST /api/v1/reporting/schedules/
```

**Request:**

```json
{
  "report_id": 5,
  "schedule_type": "HALF_MONTHLY",
  "parameters": {},
  "recipient_ids": [1, 3, 7],
  "is_active": true
}
```

---

## Enum Reference

### Docking

| Enum | Values |
|---|---|
| DockingEntryType | `INWARD`, `OUTWARD`, `EMPTY_TRUCK` |
| DockingStatus | `DRAFT`, `DOCKED`, `INVOICE_VERIFIED`, `COMPLETED`, `CANCELLED` |
| DockingPhotoType | `VEHICLE`, `INVOICE`, `MATERIAL`, `SEAL`, `OTHER` |

### Dynamic Forms

| Enum | Values |
|---|---|
| FieldType | `TEXT`, `INTEGER`, `DECIMAL`, `DATE`, `DATETIME`, `SELECT`, `RADIO`, `CHECKBOX`, `IMAGE`, `FILE`, `TEXTAREA` |
| SubmissionStatus | `PENDING`, `APPROVED`, `REJECTED`, `CLOSED` |

### Material Tracking

| Enum | Values |
|---|---|
| MovementType | `OUTWARD`, `RETURN`, `TRANSFER` |
| MovementStatus | `DISPATCHED`, `IN_TRANSIT`, `RECEIVED`, `PARTIALLY_RECEIVED`, `OVERDUE`, `CLOSED` |
| GatePassType | `RETURNABLE`, `NON_RETURNABLE` |
| GatePassStatus | `DRAFT`, `PENDING_APPROVAL`, `APPROVED`, `REJECTED`, `CLOSED` |

### Reporting

| Enum | Values |
|---|---|
| ReportCategory | `PRODUCTION`, `MATERIAL`, `GATE`, `QUALITY`, `DOCKING`, `GENERAL` |
| ScheduleType | `DAILY`, `WEEKLY`, `HALF_MONTHLY`, `MONTHLY` |
