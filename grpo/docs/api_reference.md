# GRPO API Reference

This document provides complete API documentation for the GRPO app.

## Authentication

All endpoints require:
- **JWT Authentication**: Bearer token in Authorization header
- **Company Context**: User must have an associated company

```
Authorization: Bearer <your_jwt_token>
Content-Type: application/json
```

---

## Endpoints

### 1. List Pending GRPO Entries

Returns list of completed gate entries that have POs pending GRPO posting. Entries where all POs have been posted are automatically excluded.

**Endpoint:** `GET /api/v1/grpo/pending/`

**Response (200 OK):**
```json
[
  {
    "vehicle_entry_id": 123,
    "entry_no": "VE-2024-001",
    "status": "COMPLETED",
    "entry_time": "2024-01-15T10:30:00Z",
    "total_po_count": 2,
    "posted_po_count": 1,
    "pending_po_count": 1,
    "is_fully_posted": false
  }
]
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| vehicle_entry_id | integer | Unique ID of the vehicle entry |
| entry_no | string | Gate entry number |
| status | string | Current status (COMPLETED, QC_COMPLETED) |
| entry_time | datetime | Time of gate entry |
| total_po_count | integer | Total number of POs in this entry |
| posted_po_count | integer | Number of POs already posted to SAP |
| pending_po_count | integer | Number of POs pending posting (always > 0) |
| is_fully_posted | boolean | Always false (fully posted entries are excluded) |

**Note:** This endpoint only returns entries with `pending_po_count > 0`. Once all POs in an entry are posted, the entry will no longer appear in this list.

---

### 2. Preview GRPO Data

Returns all data required for GRPO posting for a specific gate entry.

**Endpoint:** `GET /api/v1/grpo/preview/<vehicle_entry_id>/`

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| vehicle_entry_id | integer | Yes | The vehicle entry ID |

**Response (200 OK):**
```json
[
  {
    "vehicle_entry_id": 123,
    "entry_no": "VE-2024-001",
    "entry_status": "COMPLETED",
    "is_ready_for_grpo": true,
    "po_receipt_id": 456,
    "po_number": "PO-001",
    "supplier_code": "SUP001",
    "supplier_name": "ABC Suppliers Ltd",
    "invoice_no": "INV-12345",
    "invoice_date": "2024-01-15",
    "challan_no": "CH-789",
    "items": [
      {
        "po_item_receipt_id": 789,
        "item_code": "ITEM001",
        "item_name": "Raw Material A",
        "ordered_qty": 1000.000,
        "received_qty": 1000.000,
        "accepted_qty": 950.000,
        "rejected_qty": 50.000,
        "uom": "KG",
        "qc_status": "ACCEPTED"
      }
    ],
    "grpo_status": null,
    "sap_doc_num": null
  }
]
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| vehicle_entry_id | integer | Vehicle entry ID |
| entry_no | string | Gate entry number |
| entry_status | string | Gate entry status |
| is_ready_for_grpo | boolean | True if entry is ready for GRPO posting |
| po_receipt_id | integer | PO receipt ID (use this for posting) |
| po_number | string | Purchase order number |
| supplier_code | string | Supplier code in SAP |
| supplier_name | string | Supplier name |
| invoice_no | string | Invoice number |
| invoice_date | date | Invoice date |
| challan_no | string | Challan/delivery note number |
| items | array | List of PO items |
| grpo_status | string | GRPO posting status (null if not posted) |
| sap_doc_num | integer | SAP document number (null if not posted) |

**Item Fields:**

| Field | Type | Description |
|-------|------|-------------|
| po_item_receipt_id | integer | PO item receipt ID |
| item_code | string | Item code |
| item_name | string | Item description |
| ordered_qty | decimal | Ordered quantity |
| received_qty | decimal | Received quantity |
| accepted_qty | decimal | QC accepted quantity (posted to GRPO) |
| rejected_qty | decimal | QC rejected quantity |
| uom | string | Unit of measure |
| qc_status | string | QC status (ACCEPTED, REJECTED, PENDING, etc.) |

**Error Responses:**

| Status | Description |
|--------|-------------|
| 401 | Unauthorized - Invalid or missing token |
| 404 | Vehicle entry not found |

---

### 3. Post GRPO to SAP

Posts GRPO to SAP for a specific PO receipt. Users must provide accepted quantities for each item, which are stored in POItemReceipt before posting to SAP.

**Endpoint:** `POST /api/v1/grpo/post/`

**Request Payload:**
```json
{
  "vehicle_entry_id": 123,
  "po_receipt_id": 456,
  "items": [
    {"po_item_receipt_id": 789, "accepted_qty": 950.000},
    {"po_item_receipt_id": 790, "accepted_qty": 500.000}
  ],
  "branch_id": 1,
  "warehouse_code": "WH01",
  "comments": "Gate entry completed - goods received"
}
```

**Request Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| vehicle_entry_id | integer | Yes | Vehicle entry ID |
| po_receipt_id | integer | Yes | PO receipt ID |
| items | array | Yes | List of items with accepted quantities |
| items[].po_item_receipt_id | integer | Yes | PO item receipt ID (from preview) |
| items[].accepted_qty | decimal | Yes | Quantity to accept (min: 0) |
| branch_id | integer | Yes | SAP Branch/Business Place ID (BPLId) |
| warehouse_code | string | No | Target warehouse code in SAP |
| comments | string | No | Comments/remarks for the GRPO |

**Items Validation:**
- At least one item is required
- `accepted_qty` cannot be negative
- `rejected_qty` is automatically calculated as `max(received_qty - accepted_qty, 0)`
- All `po_item_receipt_id` values must belong to the specified PO receipt

**Success Response (201 Created):**
```json
{
  "success": true,
  "grpo_posting_id": 789,
  "sap_doc_entry": 12345,
  "sap_doc_num": 1001,
  "sap_doc_total": 47500.00,
  "message": "GRPO posted successfully. SAP Doc Num: 1001"
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| success | boolean | True if posting was successful |
| grpo_posting_id | integer | Internal GRPO posting record ID |
| sap_doc_entry | integer | SAP internal document entry ID |
| sap_doc_num | integer | SAP document number (visible in SAP UI) |
| sap_doc_total | decimal | Total document value |
| message | string | Success message |

**Error Responses:**

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Invalid request data | Missing required fields or invalid format |
| 400 | At least one item required | Items array is empty |
| 400 | Invalid PO item receipt IDs | Item IDs don't belong to the PO receipt |
| 400 | Gate entry is not completed | Entry status is not COMPLETED/QC_COMPLETED |
| 400 | GRPO already posted | GRPO was already posted for this PO |
| 400 | No accepted quantities | All items have accepted_qty = 0 |
| 400 | SAP validation error | SAP rejected the GRPO |
| 401 | Unauthorized | Invalid or missing token |
| 502 | SAP error | SAP data error |
| 503 | SAP unavailable | SAP system is not reachable |

**Error Response Examples:**
```json
{
  "detail": "Gate entry is not completed. Current status: IN_PROGRESS"
}
```

```json
{
  "detail": "Invalid request data",
  "errors": {
    "items": ["At least one item with accepted quantity is required"]
  }
}
```

---

### 4. GRPO Posting History

Returns GRPO posting history with optional filtering.

**Endpoint:** `GET /api/v1/grpo/history/`

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| vehicle_entry_id | integer | No | Filter by vehicle entry ID |

**Response (200 OK):**
```json
[
  {
    "id": 789,
    "vehicle_entry": 123,
    "entry_no": "VE-2024-001",
    "po_receipt": 456,
    "po_number": "PO-001",
    "sap_doc_entry": 12345,
    "sap_doc_num": 1001,
    "sap_doc_total": "47500.00",
    "status": "POSTED",
    "error_message": null,
    "posted_at": "2024-01-15T14:30:00Z",
    "posted_by": 1,
    "created_at": "2024-01-15T14:30:00Z",
    "lines": [
      {
        "id": 1,
        "item_code": "ITEM001",
        "item_name": "Raw Material A",
        "quantity_posted": "950.000",
        "base_entry": null,
        "base_line": null
      }
    ]
  }
]
```

---

### 5. GRPO Posting Detail

Returns details of a specific GRPO posting.

**Endpoint:** `GET /api/v1/grpo/<posting_id>/`

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| posting_id | integer | Yes | GRPO posting ID |

**Response (200 OK):**
```json
{
  "id": 789,
  "vehicle_entry": 123,
  "entry_no": "VE-2024-001",
  "po_receipt": 456,
  "po_number": "PO-001",
  "sap_doc_entry": 12345,
  "sap_doc_num": 1001,
  "sap_doc_total": "47500.00",
  "status": "POSTED",
  "error_message": null,
  "posted_at": "2024-01-15T14:30:00Z",
  "posted_by": 1,
  "created_at": "2024-01-15T14:30:00Z",
  "lines": [
    {
      "id": 1,
      "item_code": "ITEM001",
      "item_name": "Raw Material A",
      "quantity_posted": "950.000",
      "base_entry": null,
      "base_line": null
    }
  ]
}
```

**Error Responses:**

| Status | Description |
|--------|-------------|
| 401 | Unauthorized |
| 404 | GRPO posting not found |

---

## Status Values

### GRPO Status

| Status | Description |
|--------|-------------|
| PENDING | GRPO posting initiated but not yet sent to SAP |
| POSTED | Successfully posted to SAP |
| FAILED | Posting failed (check error_message) |
| PARTIALLY_POSTED | Some items posted, others failed |

### QC Status

| Status | Description |
|--------|-------------|
| PENDING | QC not started |
| ACCEPTED | QC passed - quantity can be posted |
| REJECTED | QC failed - quantity not posted |
| NO_ARRIVAL_SLIP | Arrival slip not created |
| ARRIVAL_SLIP_PENDING | Arrival slip not submitted |
| INSPECTION_PENDING | Inspection not created |
