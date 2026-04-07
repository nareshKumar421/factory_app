# Warehouse Module — Frontend Integration Guide

> Base URL: `/api/v1/warehouse/`
> Auth: `Bearer <JWT>` header + `Company-Code` header on all requests.

---

## Complete Workflow Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        PRODUCTION SIDE                                   │
│                                                                          │
│  1. Create Production Run (with required_qty)                           │
│     POST /api/v1/production-execution/runs/                             │
│                                                                          │
│  2. Submit BOM Request to Warehouse                                     │
│     POST /api/v1/warehouse/bom-requests/create/                         │
│     → BOM auto-fetched from SAP, scaled by required_qty                 │
│     → run.warehouse_approval_status = "PENDING"                         │
│                                                                          │
│  3. Wait for warehouse approval...                                       │
│     (Start Production button DISABLED until approved)                   │
│                                                                          │
│  4. Start Production (only if warehouse_approval_status = APPROVED)     │
│     POST /api/v1/production-execution/runs/{id}/start-production/       │
│                                                                          │
│  5. Complete Production                                                  │
│     POST /api/v1/production-execution/runs/{id}/complete/               │
│     → Notifies warehouse (FG receipt auto-created or manual)            │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                        WAREHOUSE SIDE                                    │
│                                                                          │
│  A. View pending BOM requests                                            │
│     GET /api/v1/warehouse/bom-requests/?status=PENDING                  │
│                                                                          │
│  B. Review BOM request detail (with live stock from SAP)                │
│     GET /api/v1/warehouse/bom-requests/{id}/                            │
│                                                                          │
│  C. Approve or Reject                                                    │
│     POST /api/v1/warehouse/bom-requests/{id}/approve/                   │
│     POST /api/v1/warehouse/bom-requests/{id}/reject/                    │
│                                                                          │
│  D. Issue materials to SAP (after approval)                              │
│     POST /api/v1/warehouse/bom-requests/{id}/issue/                     │
│                                                                          │
│  E. Receive finished goods (after production completes)                  │
│     POST /api/v1/warehouse/fg-receipts/create/                          │
│     POST /api/v1/warehouse/fg-receipts/{id}/receive/                    │
│     POST /api/v1/warehouse/fg-receipts/{id}/post-to-sap/               │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Create Production Run (Updated)

**`POST /api/v1/production-execution/runs/`**

The `required_qty` field is new — it determines how many units to produce and scales the BOM accordingly.

### Request
```json
{
    "sap_doc_entry": 1234,
    "line_id": 1,
    "date": "2026-04-06",
    "product": "FG-OIL-1L",
    "required_qty": 500.00,
    "rated_speed": 100.00,
    "machine_ids": [1, 2],
    "labour_count": 5,
    "supervisor": "Mr. Singh",
    "operators": "Op1, Op2"
}
```

### Response — `201 Created`
```json
{
    "id": 42,
    "sap_doc_entry": 1234,
    "run_number": 1,
    "date": "2026-04-06",
    "line": 1,
    "line_name": "Line A",
    "product": "FG-OIL-1L",
    "required_qty": "500.00",
    "rated_speed": "100.00",
    "warehouse_approval_status": "NOT_REQUESTED",
    "status": "DRAFT",
    ...
}
```

### New Fields in ProductionRun Response

| Field | Type | Description |
|-------|------|-------------|
| `required_qty` | decimal | Required production quantity (units) |
| `warehouse_approval_status` | string | `NOT_REQUESTED` / `PENDING` / `APPROVED` / `PARTIALLY_APPROVED` / `REJECTED` |

---

## 2. Submit BOM Request to Warehouse

**`POST /api/v1/warehouse/bom-requests/create/`**

This fetches BOM from SAP, scales quantities by `required_qty`, and sends to warehouse for approval.

### Request
```json
{
    "production_run_id": 42,
    "required_qty": 500.00,
    "remarks": "Urgent — morning shift production"
}
```

### Response — `201 Created`
```json
{
    "id": 1,
    "production_run": 42,
    "run_number": 1,
    "run_date": "2026-04-06",
    "line_name": "Line A",
    "product": "FG-OIL-1L",
    "sap_doc_entry": 1234,
    "required_qty": "500.00",
    "status": "PENDING",
    "material_issue_status": "NOT_ISSUED",
    "sap_issue_doc_entries": [],
    "remarks": "Urgent — morning shift production",
    "rejection_reason": "",
    "requested_by": 1,
    "requested_by_name": "Naresh Kumar",
    "reviewed_by": null,
    "reviewed_by_name": "",
    "reviewed_at": null,
    "lines": [
        {
            "id": 1,
            "item_code": "RM-CANOLA-OIL",
            "item_name": "Canola Oil Crude",
            "per_unit_qty": "1.0500",
            "required_qty": "525.000",
            "available_stock": "0.000",
            "approved_qty": "0.000",
            "issued_qty": "0.000",
            "warehouse": "RM-WH",
            "uom": "LTR",
            "base_line": 0,
            "status": "PENDING",
            "remarks": ""
        },
        {
            "id": 2,
            "item_code": "PM-BOTTLE-1L",
            "item_name": "PET Bottle 1 Litre",
            "per_unit_qty": "1.0000",
            "required_qty": "500.000",
            "available_stock": "0.000",
            "approved_qty": "0.000",
            "issued_qty": "0.000",
            "warehouse": "PM-WH",
            "uom": "PCS",
            "base_line": 1,
            "status": "PENDING",
            "remarks": ""
        },
        {
            "id": 3,
            "item_code": "PM-CAP-38MM",
            "item_name": "Cap 38mm",
            "per_unit_qty": "1.0000",
            "required_qty": "500.000",
            "available_stock": "0.000",
            "approved_qty": "0.000",
            "issued_qty": "0.000",
            "warehouse": "PM-WH",
            "uom": "PCS",
            "base_line": 2,
            "status": "PENDING",
            "remarks": ""
        }
    ],
    "created_at": "2026-04-06T09:00:00+05:30",
    "updated_at": "2026-04-06T09:00:00+05:30"
}
```

### Error — `400 Bad Request`
```json
{
    "error": "An active BOM request already exists for this run."
}
```

---

## 3. List BOM Requests (Warehouse Dashboard)

**`GET /api/v1/warehouse/bom-requests/`**

### Query Parameters

| Param | Type | Description |
|-------|------|-------------|
| `status` | string | Filter: `PENDING`, `APPROVED`, `PARTIALLY_APPROVED`, `REJECTED` |
| `production_run_id` | integer | Filter by specific run |

### Example — Pending requests
```
GET /api/v1/warehouse/bom-requests/?status=PENDING
```

### Response — `200 OK`
```json
[
    {
        "id": 1,
        "production_run": 42,
        "run_number": 1,
        "run_date": "2026-04-06",
        "line_name": "Line A",
        "product": "FG-OIL-1L",
        "sap_doc_entry": 1234,
        "required_qty": "500.00",
        "status": "PENDING",
        "material_issue_status": "NOT_ISSUED",
        "remarks": "Urgent — morning shift production",
        "rejection_reason": "",
        "requested_by": 1,
        "requested_by_name": "Naresh Kumar",
        "reviewed_by": null,
        "reviewed_by_name": "",
        "reviewed_at": null,
        "lines_count": 3,
        "created_at": "2026-04-06T09:00:00+05:30",
        "updated_at": "2026-04-06T09:00:00+05:30"
    }
]
```

---

## 4. BOM Request Detail (with Live Stock)

**`GET /api/v1/warehouse/bom-requests/{request_id}/`**

Returns BOM request with all lines enriched with **live SAP stock data**.

### Response — `200 OK`
```json
{
    "id": 1,
    "production_run": 42,
    "run_number": 1,
    "run_date": "2026-04-06",
    "line_name": "Line A",
    "product": "FG-OIL-1L",
    "sap_doc_entry": 1234,
    "required_qty": "500.00",
    "status": "PENDING",
    "material_issue_status": "NOT_ISSUED",
    "sap_issue_doc_entries": [],
    "remarks": "",
    "rejection_reason": "",
    "requested_by": 1,
    "requested_by_name": "Naresh Kumar",
    "reviewed_by": null,
    "reviewed_by_name": "",
    "reviewed_at": null,
    "lines": [
        {
            "id": 1,
            "item_code": "RM-CANOLA-OIL",
            "item_name": "Canola Oil Crude",
            "per_unit_qty": "1.0500",
            "required_qty": "525.000",
            "available_stock": 1200.0,
            "available_qty": 950.0,
            "approved_qty": "0.000",
            "issued_qty": "0.000",
            "warehouse": "RM-WH",
            "uom": "LTR",
            "base_line": 0,
            "status": "PENDING",
            "remarks": "",
            "stock_warehouses": [
                {"WhsCode": "RM-WH", "OnHand": 800.0, "Available": 650.0},
                {"WhsCode": "RM-WH2", "OnHand": 400.0, "Available": 300.0}
            ]
        },
        {
            "id": 2,
            "item_code": "PM-BOTTLE-1L",
            "item_name": "PET Bottle 1 Litre",
            "per_unit_qty": "1.0000",
            "required_qty": "500.000",
            "available_stock": 200.0,
            "available_qty": 150.0,
            "approved_qty": "0.000",
            "issued_qty": "0.000",
            "warehouse": "PM-WH",
            "uom": "PCS",
            "base_line": 1,
            "status": "PENDING",
            "remarks": "",
            "stock_warehouses": [
                {"WhsCode": "PM-WH", "OnHand": 200.0, "Available": 150.0}
            ]
        }
    ],
    "created_at": "2026-04-06T09:00:00+05:30",
    "updated_at": "2026-04-06T09:00:00+05:30"
}
```

### Extra Fields on Lines (from live SAP stock)

| Field | Type | Description |
|-------|------|-------------|
| `available_stock` | number | Total OnHand stock across all warehouses |
| `available_qty` | number | Available stock (OnHand - Committed) |
| `stock_warehouses` | array | Per-warehouse stock breakdown |

### Error — `404 Not Found`
```json
{
    "error": "BOM request 999 not found."
}
```

---

## 5. Approve BOM Request (Warehouse Action)

**`POST /api/v1/warehouse/bom-requests/{request_id}/approve/`**

Warehouse approves or partially approves with line-level decisions.

### Request — Full Approval
```json
{
    "lines": [
        {"line_id": 1, "approved_qty": 525.0, "status": "APPROVED"},
        {"line_id": 2, "approved_qty": 500.0, "status": "APPROVED"},
        {"line_id": 3, "approved_qty": 500.0, "status": "APPROVED"}
    ]
}
```

### Request — Partial Approval (some lines rejected or reduced qty)
```json
{
    "lines": [
        {"line_id": 1, "approved_qty": 525.0, "status": "APPROVED"},
        {"line_id": 2, "approved_qty": 200.0, "status": "APPROVED", "remarks": "Only 200 in stock"},
        {"line_id": 3, "status": "REJECTED", "remarks": "Out of stock"}
    ]
}
```

### Response — `200 OK`
```json
{
    "id": 1,
    "status": "PARTIALLY_APPROVED",
    "lines": [
        {
            "id": 1,
            "item_code": "RM-CANOLA-OIL",
            "status": "APPROVED",
            "approved_qty": "525.000",
            "remarks": ""
        },
        {
            "id": 2,
            "item_code": "PM-BOTTLE-1L",
            "status": "APPROVED",
            "approved_qty": "200.000",
            "remarks": "Only 200 in stock"
        },
        {
            "id": 3,
            "item_code": "PM-CAP-38MM",
            "status": "REJECTED",
            "approved_qty": "0.000",
            "remarks": "Out of stock"
        }
    ],
    "reviewed_by": 2,
    "reviewed_by_name": "Warehouse Manager",
    "reviewed_at": "2026-04-06T10:30:00+05:30",
    ...
}
```

### Status Logic

| Scenario | BOM Request Status | Production Can Start? |
|----------|-------------------|----------------------|
| All lines APPROVED | `APPROVED` | YES |
| Some lines APPROVED, some REJECTED | `PARTIALLY_APPROVED` | YES |
| All lines REJECTED | `REJECTED` | NO |

### Error — `400 Bad Request`
```json
{
    "error": "Only PENDING requests can be approved."
}
```

---

## 6. Reject BOM Request (Warehouse Action)

**`POST /api/v1/warehouse/bom-requests/{request_id}/reject/`**

Rejects the entire BOM request.

### Request
```json
{
    "reason": "Insufficient raw material stock for this batch size. Please reduce required_qty to 200."
}
```

### Response — `200 OK`
```json
{
    "id": 1,
    "status": "REJECTED",
    "rejection_reason": "Insufficient raw material stock for this batch size. Please reduce required_qty to 200.",
    "reviewed_by": 2,
    "reviewed_by_name": "Warehouse Manager",
    "reviewed_at": "2026-04-06T10:30:00+05:30",
    "lines": [
        {"id": 1, "status": "REJECTED", ...},
        {"id": 2, "status": "REJECTED", ...}
    ],
    ...
}
```

---

## 7. Issue Materials to SAP (After Approval)

**`POST /api/v1/warehouse/bom-requests/{request_id}/issue/`**

Issues approved materials from warehouse to production. Creates SAP `InventoryGenExits` document.

### Request — Issue all remaining approved materials
```json
{}
```

### Request — Issue specific lines/quantities (partial issue)
```json
{
    "posting_date": "2026-04-06",
    "lines": [
        {"line_id": 1, "quantity": 300.0},
        {"line_id": 2, "quantity": 200.0}
    ]
}
```

### Response — `200 OK`
```json
{
    "id": 1,
    "status": "APPROVED",
    "material_issue_status": "PARTIALLY_ISSUED",
    "sap_issue_doc_entries": [
        {
            "doc_entry": 789,
            "doc_num": 1001,
            "date": "2026-04-06",
            "lines_count": 2
        }
    ],
    "lines": [
        {
            "id": 1,
            "item_code": "RM-CANOLA-OIL",
            "approved_qty": "525.000",
            "issued_qty": "300.000",
            ...
        },
        {
            "id": 2,
            "item_code": "PM-BOTTLE-1L",
            "approved_qty": "200.000",
            "issued_qty": "200.000",
            ...
        }
    ],
    ...
}
```

### `material_issue_status` Values

| Status | Description |
|--------|-------------|
| `NOT_ISSUED` | No materials issued yet |
| `PARTIALLY_ISSUED` | Some materials issued, remaining to be issued |
| `FULLY_ISSUED` | All approved materials have been issued |

### Error — `400 Bad Request`
```json
{
    "error": "Issue qty 600.0 exceeds remaining approved qty 525.0 for RM-CANOLA-OIL"
}
```

---

## 8. Stock Check

**`POST /api/v1/warehouse/stock/check/`**

Check available stock for multiple items. Useful for pre-validation before approval.

### Request
```json
{
    "item_codes": ["RM-CANOLA-OIL", "PM-BOTTLE-1L", "PM-CAP-38MM"]
}
```

### Response — `200 OK`
```json
{
    "RM-CANOLA-OIL": {
        "ItemCode": "RM-CANOLA-OIL",
        "ItemName": "Canola Oil Crude",
        "total_on_hand": 1200.0,
        "total_available": 950.0,
        "warehouses": [
            {"WhsCode": "RM-WH", "OnHand": 800.0, "Available": 650.0},
            {"WhsCode": "RM-WH2", "OnHand": 400.0, "Available": 300.0}
        ]
    },
    "PM-BOTTLE-1L": {
        "ItemCode": "PM-BOTTLE-1L",
        "ItemName": "PET Bottle 1 Litre",
        "total_on_hand": 200.0,
        "total_available": 150.0,
        "warehouses": [
            {"WhsCode": "PM-WH", "OnHand": 200.0, "Available": 150.0}
        ]
    },
    "PM-CAP-38MM": {
        "ItemCode": "PM-CAP-38MM",
        "ItemName": "Cap 38mm",
        "total_on_hand": 0,
        "total_available": 0,
        "warehouses": []
    }
}
```

---

## 9. Create Finished Goods Receipt (Post-Production)

**`POST /api/v1/warehouse/fg-receipts/create/`**

Called after production is completed to notify warehouse of finished goods.

### Request
```json
{
    "production_run_id": 42,
    "posting_date": "2026-04-06"
}
```

> `item_code`, `item_name`, `warehouse` are auto-fetched from SAP if the run has `sap_doc_entry`.

### Response — `201 Created`
```json
{
    "id": 1,
    "production_run": 42,
    "run_number": 1,
    "run_date": "2026-04-06",
    "sap_doc_entry": 1234,
    "item_code": "FG-OIL-1L",
    "item_name": "Jivo Canola Oil 1L",
    "produced_qty": "500.00",
    "good_qty": "490.00",
    "rejected_qty": "10.00",
    "warehouse": "FG-WH",
    "uom": "",
    "posting_date": "2026-04-06",
    "status": "PENDING",
    "sap_receipt_doc_entry": null,
    "sap_error": "",
    "received_by": null,
    "received_by_name": "",
    "received_at": null,
    "created_at": "2026-04-06T18:00:00+05:30",
    "updated_at": "2026-04-06T18:00:00+05:30"
}
```

### Error — `400 Bad Request`
```json
{
    "error": "Can only create FG receipt for completed runs."
}
```

---

## 10. List Finished Goods Receipts

**`GET /api/v1/warehouse/fg-receipts/`**

### Query Parameters

| Param | Type | Description |
|-------|------|-------------|
| `status` | string | Filter: `PENDING`, `RECEIVED`, `SAP_POSTED`, `FAILED` |
| `production_run_id` | integer | Filter by specific run |

### Response — `200 OK`
```json
[
    {
        "id": 1,
        "production_run": 42,
        "run_number": 1,
        "run_date": "2026-04-06",
        "sap_doc_entry": 1234,
        "item_code": "FG-OIL-1L",
        "item_name": "Jivo Canola Oil 1L",
        "produced_qty": "500.00",
        "good_qty": "490.00",
        "rejected_qty": "10.00",
        "warehouse": "FG-WH",
        "posting_date": "2026-04-06",
        "status": "PENDING",
        "sap_receipt_doc_entry": null,
        "sap_error": "",
        "received_by": null,
        "received_by_name": "",
        "received_at": null,
        "created_at": "2026-04-06T18:00:00+05:30"
    }
]
```

---

## 11. FG Receipt Detail

**`GET /api/v1/warehouse/fg-receipts/{receipt_id}/`**

### Response — `200 OK`
Same structure as list item.

### Error — `404 Not Found`
```json
{
    "error": "FG receipt 999 not found."
}
```

---

## 12. Receive Finished Goods (Warehouse Confirmation)

**`POST /api/v1/warehouse/fg-receipts/{receipt_id}/receive/`**

Warehouse user confirms they have physically received the finished goods.

### Request
No body required.

### Response — `200 OK`
```json
{
    "id": 1,
    "status": "RECEIVED",
    "received_by": 2,
    "received_by_name": "Warehouse Manager",
    "received_at": "2026-04-06T18:30:00+05:30",
    ...
}
```

### Error — `400 Bad Request`
```json
{
    "error": "Receipt is already processed."
}
```

---

## 13. Post FG Receipt to SAP

**`POST /api/v1/warehouse/fg-receipts/{receipt_id}/post-to-sap/`**

Posts the received finished goods to SAP `InventoryGenEntries` (Goods Receipt linked to Production Order).

### Request
No body required.

### Response — `200 OK`
```json
{
    "id": 1,
    "status": "SAP_POSTED",
    "sap_receipt_doc_entry": 5678,
    "sap_error": "",
    "received_by": 2,
    "received_by_name": "Warehouse Manager",
    "received_at": "2026-04-06T18:30:00+05:30",
    ...
}
```

### Error — `400 Bad Request`
```json
{
    "error": "SAP posting failed: Insufficient inventory for item FG-OIL-1L in warehouse FG-WH"
}
```

---

## Status Code Reference

| Status Code | Meaning | When |
|-------------|---------|------|
| `200 OK` | Success | GET, PATCH, approval/reject/receive/issue actions |
| `201 Created` | Resource created | POST create endpoints |
| `400 Bad Request` | Validation error or business rule violation | Invalid data, wrong status transitions |
| `401 Unauthorized` | Missing/invalid JWT | No Bearer token |
| `403 Forbidden` | No company access | Invalid Company-Code header |
| `404 Not Found` | Resource not found | Invalid ID |

---

## Frontend UI Components Guide

### Production Page — Changes Needed

#### 1. Required Quantity Field (New)
```
┌─────────────────────────────────────────────────┐
│ Create Production Run                            │
│                                                  │
│ SAP Order: [1234 — FG-OIL-1L        ] ▼        │
│ Line:      [Line A                   ] ▼        │
│ Date:      [2026-04-06              ]           │
│                                                  │
│ ┌─────────────────────────────────────────────┐ │
│ │ Required Quantity: [  500  ] units          │ │
│ │ (BOM will scale based on this quantity)     │ │
│ └─────────────────────────────────────────────┘ │
│                                                  │
│ [Create Run]                                     │
└─────────────────────────────────────────────────┘
```

#### 2. Submit to Warehouse Button
After run is created (status = DRAFT):
```
┌─────────────────────────────────────────────────┐
│ Production Run #1 — 2026-04-06                   │
│ Product: FG-OIL-1L | Qty: 500 | Status: DRAFT  │
│                                                  │
│ Warehouse: ⚪ Not Requested                      │
│                                                  │
│ [Submit BOM to Warehouse]  [Start Production]    │
│                            (disabled - grey)     │
└─────────────────────────────────────────────────┘
```

#### 3. Approval Status Badge
```
warehouse_approval_status → Badge color:
  NOT_REQUESTED  →  ⚪ Grey
  PENDING        →  🟡 Yellow/Orange
  APPROVED       →  🟢 Green
  PARTIALLY_APPROVED → 🔵 Blue
  REJECTED       →  🔴 Red
```

#### 4. Start Production Gate
```javascript
// Only enable Start Production button when:
const canStart = ['APPROVED', 'PARTIALLY_APPROVED'].includes(
    run.warehouse_approval_status
);
// OR when NOT_REQUESTED (no warehouse workflow needed)
const canStartNoWorkflow = run.warehouse_approval_status === 'NOT_REQUESTED';
```

---

### Warehouse App — New Pages

#### Page 1: BOM Requests Dashboard
```
┌─────────────────────────────────────────────────────────────┐
│ Warehouse — BOM Requests                    [Filter: All ▼] │
├─────┬──────┬───────────┬────────┬──────────┬───────────────┤
│ ID  │ Run  │ Product   │ Qty    │ Status   │ Requested     │
├─────┼──────┼───────────┼────────┼──────────┼───────────────┤
│ #1  │ R-1  │ FG-OIL-1L │ 500    │ 🟡 PEND │ Naresh 9:00am │
│ #2  │ R-2  │ FG-OIL-5L │ 200    │ 🟢 APPR │ Amit 8:30am   │
│ #3  │ R-3  │ FG-JAM-1K │ 100    │ 🔴 REJ  │ Naresh 7:00am │
└─────┴──────┴───────────┴────────┴──────────┴───────────────┘
```

#### Page 2: BOM Request Review (Detail)
```
┌─────────────────────────────────────────────────────────────┐
│ BOM Request #1 — Run #1 (2026-04-06)                        │
│ Product: FG-OIL-1L | Required Qty: 500 | Status: PENDING   │
│ Requested by: Naresh Kumar at 9:00 AM                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│ ┌──────────┬──────────┬──────────┬──────────┬─────────────┐ │
│ │ Material │ Required │ In Stock │ Approve  │ Action      │ │
│ ├──────────┼──────────┼──────────┼──────────┼─────────────┤ │
│ │ Canola   │ 525 LTR  │ 1200 LTR│ [525   ] │ ✅ Approve  │ │
│ │ Bottle   │ 500 PCS  │ 200 PCS │ [200   ] │ ✅ Approve  │ │
│ │ Cap      │ 500 PCS  │ 0 PCS   │ [  0   ] │ ❌ Reject   │ │
│ └──────────┴──────────┴──────────┴──────────┴─────────────┘ │
│                                                              │
│ Stock color coding:                                          │
│   🟢 Stock >= Required (sufficient)                          │
│   🟡 0 < Stock < Required (partial)                          │
│   🔴 Stock = 0 (out of stock)                                │
│                                                              │
│ [Approve Selected]          [Reject All]                     │
└─────────────────────────────────────────────────────────────┘
```

#### Page 3: Material Issue
```
┌─────────────────────────────────────────────────────────────┐
│ Issue Materials — BOM Request #1                             │
│ Status: APPROVED | Issue Status: NOT_ISSUED                  │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│ ┌──────────┬──────────┬──────────┬──────────┬─────────────┐ │
│ │ Material │ Approved │ Issued   │ Issue Now│ Warehouse   │ │
│ ├──────────┼──────────┼──────────┼──────────┼─────────────┤ │
│ │ Canola   │ 525 LTR  │ 0 LTR   │ [525   ] │ RM-WH      │ │
│ │ Bottle   │ 200 PCS  │ 0 PCS   │ [200   ] │ PM-WH      │ │
│ └──────────┴──────────┴──────────┴──────────┴─────────────┘ │
│                                                              │
│ Posting Date: [2026-04-06]                                   │
│                                                              │
│ [Issue to SAP]                                               │
└─────────────────────────────────────────────────────────────┘
```

#### Page 4: Finished Goods Receipts
```
┌─────────────────────────────────────────────────────────────┐
│ Warehouse — Finished Goods Receipts         [Filter: All ▼] │
├─────┬──────┬───────────┬────────┬──────────┬───────────────┤
│ ID  │ Run  │ Product   │ Good Q │ Status   │ Actions       │
├─────┼──────┼───────────┼────────┼──────────┼───────────────┤
│ #1  │ R-1  │ FG-OIL-1L │ 490    │ 🟡 PEND │ [Receive]     │
│ #2  │ R-2  │ FG-OIL-5L │ 195    │ 🟢 RECV │ [Post to SAP] │
│ #3  │ R-3  │ FG-JAM-1K │ 98     │ ✅ SAP  │ DocEntry:5678 │
└─────┴──────┴───────────┴────────┴──────────┴───────────────┘
```

---

## FG Receipt Status Flow

```
PENDING  ──[Receive]──►  RECEIVED  ──[Post to SAP]──►  SAP_POSTED
                                            │
                                            ▼ (on failure)
                                          FAILED  ──[Retry Post]──►  SAP_POSTED
```

---

## Complete API Endpoints Summary

| Method | Endpoint | Purpose | Who Uses |
|--------|----------|---------|----------|
| **POST** | `/warehouse/bom-requests/create/` | Submit BOM to warehouse | Production |
| **GET** | `/warehouse/bom-requests/` | List BOM requests | Warehouse |
| **GET** | `/warehouse/bom-requests/{id}/` | BOM detail + live stock | Warehouse |
| **POST** | `/warehouse/bom-requests/{id}/approve/` | Approve BOM (line-level) | Warehouse |
| **POST** | `/warehouse/bom-requests/{id}/reject/` | Reject BOM | Warehouse |
| **POST** | `/warehouse/bom-requests/{id}/issue/` | Issue materials to SAP | Warehouse |
| **POST** | `/warehouse/stock/check/` | Check stock for items | Warehouse |
| **POST** | `/warehouse/fg-receipts/create/` | Create FG receipt | Production/Auto |
| **GET** | `/warehouse/fg-receipts/` | List FG receipts | Warehouse |
| **GET** | `/warehouse/fg-receipts/{id}/` | FG receipt detail | Warehouse |
| **POST** | `/warehouse/fg-receipts/{id}/receive/` | Confirm receipt | Warehouse |
| **POST** | `/warehouse/fg-receipts/{id}/post-to-sap/` | Post to SAP | Warehouse |

---

## Headers Required (All Endpoints)

```
Authorization: Bearer <jwt_access_token>
Company-Code: JIVO_OIL
Content-Type: application/json
```
