# GRPO Frontend Developer Guide

This guide provides everything frontend developers need to implement the GRPO (Goods Receipt Purchase Order) posting feature.

## Overview

The GRPO module allows users to post goods receipts to SAP after gate entry completion. The typical user flow is:

1. View list of pending entries
2. Select an entry to see PO details
3. Enter accepted quantities for each item
4. Submit GRPO to SAP
5. View posting history/confirmation

---

## Authentication

All API calls require JWT authentication:

```javascript
const headers = {
  'Authorization': `Bearer ${accessToken}`,
  'Content-Type': 'application/json'
};
```

---

## Complete User Flow

### Flow Diagram

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Pending List   │────▶│  Preview/Edit   │────▶│  Confirm Post   │────▶│    Success      │
│    Screen       │     │    Screen       │     │    Dialog       │     │    Screen       │
└─────────────────┘     └─────────────────┘     └─────────────────┘     └─────────────────┘
       │                        │                       │                       │
       ▼                        ▼                       ▼                       ▼
  GET /pending/           GET /preview/{id}/      POST /post/           GET /history/
```

---

## API Endpoints

### 1. Get Pending Entries

**Purpose:** Show list of gate entries that need GRPO posting.

**Request:**
```javascript
GET /api/v1/grpo/pending/
```

**Response:**
```json
[
  {
    "vehicle_entry_id": 3,
    "entry_no": "VE-2024-003",
    "status": "COMPLETED",
    "entry_time": "2024-01-15T10:30:00Z",
    "total_po_count": 2,
    "posted_po_count": 0,
    "pending_po_count": 2,
    "is_fully_posted": false
  }
]
```

**Frontend Implementation:**
```javascript
// Fetch pending entries
async function fetchPendingEntries() {
  const response = await fetch('/api/v1/grpo/pending/', { headers });
  const entries = await response.json();

  // Display as a list/table
  entries.forEach(entry => {
    // Show entry_no, entry_time, pending_po_count
    // Add click handler to navigate to preview
  });
}
```

**UI Suggestions:**
- Show as a card list or table
- Display badge with `pending_po_count`
- Show empty state if no pending entries
- Add pull-to-refresh functionality

---

### 2. Get Preview Data

**Purpose:** Show details of all POs and items for a specific entry before posting.

**Request:**
```javascript
GET /api/v1/grpo/preview/{vehicle_entry_id}/
```

**Response:**
```json
[
  {
    "vehicle_entry_id": 3,
    "entry_no": "VE-2024-003",
    "entry_status": "COMPLETED",
    "is_ready_for_grpo": true,
    "po_receipt_id": 2,
    "po_number": "PO-001",
    "supplier_code": "SUP001",
    "supplier_name": "ABC Suppliers Ltd",
    "invoice_no": "INV-12345",
    "invoice_date": "2024-01-15",
    "challan_no": "CH-789",
    "items": [
      {
        "po_item_receipt_id": 3,
        "item_code": "ITEM001",
        "item_name": "Raw Material A",
        "ordered_qty": 100.000,
        "received_qty": 100.000,
        "accepted_qty": 0.000,
        "rejected_qty": 0.000,
        "uom": "KG",
        "qc_status": "ACCEPTED"
      },
      {
        "po_item_receipt_id": 4,
        "item_code": "ITEM002",
        "item_name": "Raw Material B",
        "ordered_qty": 50.000,
        "received_qty": 50.000,
        "accepted_qty": 0.000,
        "rejected_qty": 0.000,
        "uom": "KG",
        "qc_status": "ACCEPTED"
      }
    ],
    "grpo_status": null,
    "sap_doc_num": null
  }
]
```

**Important Fields for Frontend:**
- `po_receipt_id` - Use this when posting
- `po_item_receipt_id` - Use this for each item when posting
- `received_qty` - Suggested default for `accepted_qty` (no longer enforced as max)
- `grpo_status` - If "POSTED", this PO is already done
- `is_ready_for_grpo` - Must be `true` to allow posting

**Frontend Implementation:**
```javascript
async function fetchPreviewData(vehicleEntryId) {
  const response = await fetch(`/api/v1/grpo/preview/${vehicleEntryId}/`, { headers });
  const poReceipts = await response.json();

  poReceipts.forEach(po => {
    // Check if already posted
    if (po.grpo_status === 'POSTED') {
      // Show as disabled/completed
      return;
    }

    // Create form for each PO
    po.items.forEach(item => {
      // Create input field for accepted_qty
      // Pre-fill with item.received_qty as default (user may enter any value >= 0)
    });
  });
}
```

**UI Suggestions:**
- Group items by PO
- Show PO header (supplier, invoice, challan)
- For each item:
  - Show item code, name, received_qty
  - Input field for accepted_qty (number input)
  - Auto-calculate rejected_qty = max(received_qty - accepted_qty, 0)
- Disable/gray out POs that are already posted
- Show validation errors inline

---

### 3. Post GRPO

**Purpose:** Submit GRPO to SAP with accepted quantities and optional attachments.

> **IMPORTANT:** Attachments MUST be sent during GRPO posting (not after).
> SAP has an approval process on PurchaseDeliveryNotes — adding attachments
> via PATCH after document creation triggers error 200039. To avoid this,
> attachments are uploaded to SAP first and included in the GRPO document creation.

**Request Format:** The endpoint supports two formats:

#### Option A: JSON only (no attachments) — backward compatible
```javascript
POST /api/v1/grpo/post/
Content-Type: application/json

{
  "vehicle_entry_id": 3,
  "po_receipt_id": 2,
  "items": [
    {"po_item_receipt_id": 3, "accepted_qty": 95},
    {"po_item_receipt_id": 4, "accepted_qty": 50}
  ],
  "branch_id": 4,
  "warehouse_code": "BH-FG",
  "comments": "Gate entry completed"
}
```

#### Option B: multipart/form-data (with attachments) — RECOMMENDED
```javascript
POST /api/v1/grpo/post/
Content-Type: multipart/form-data

// "data" field: JSON string with all GRPO fields
// "attachments" field(s): one or more files
```

**Required Fields:**
| Field | Type | Description |
|-------|------|-------------|
| vehicle_entry_id | integer | From preview response |
| po_receipt_id | integer | From preview response |
| items | array | List of items with quantities |
| items[].po_item_receipt_id | integer | From preview response |
| items[].accepted_qty | decimal | User-entered quantity (>= 0) |
| branch_id | integer | SAP Branch ID (get from config/dropdown) |

**Optional Fields:**
| Field | Type | Description |
|-------|------|-------------|
| warehouse_code | string | SAP Warehouse code |
| comments | string | Remarks for the GRPO |
| attachments | File[] | One or more files to attach (multipart only) |

**Success Response (201):**
```json
{
  "success": true,
  "grpo_posting_id": 5,
  "sap_doc_entry": 12345,
  "sap_doc_num": 1001,
  "sap_doc_total": 47500.00,
  "message": "GRPO posted successfully. SAP Doc Num: 1001",
  "attachments": [
    {
      "id": 1,
      "original_filename": "invoice.pdf",
      "sap_attachment_status": "LINKED",
      "sap_absolute_entry": 789,
      "sap_error_message": null
    }
  ]
}
```

**Frontend Implementation (with attachments):**
```javascript
async function postGRPO(formData, files = []) {
  // Build the JSON payload
  const jsonPayload = {
    vehicle_entry_id: formData.vehicleEntryId,
    po_receipt_id: formData.poReceiptId,
    items: formData.items.map(item => ({
      po_item_receipt_id: item.id,
      accepted_qty: parseFloat(item.acceptedQty)
    })),
    branch_id: formData.branchId,
    warehouse_code: formData.warehouseCode,
    comments: formData.comments
  };

  let response;

  if (files.length > 0) {
    // Use multipart/form-data when attachments are present
    const formPayload = new FormData();
    formPayload.append('data', JSON.stringify(jsonPayload));
    files.forEach(file => {
      formPayload.append('attachments', file);
    });

    response = await fetch('/api/v1/grpo/post/', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${accessToken}`,
        // DO NOT set Content-Type — browser sets it with boundary
      },
      body: formPayload
    });
  } else {
    // Use JSON when no attachments
    response = await fetch('/api/v1/grpo/post/', {
      method: 'POST',
      headers,
      body: JSON.stringify(jsonPayload)
    });
  }

  if (response.status === 201) {
    const result = await response.json();
    showSuccess(`GRPO Posted! SAP Doc: ${result.sap_doc_num}`);

    // Check attachment statuses
    if (result.attachments) {
      const failed = result.attachments.filter(a => a.sap_attachment_status === 'FAILED');
      if (failed.length > 0) {
        showWarning(`${failed.length} attachment(s) failed to upload. You can retry later.`);
      }
    }
  } else {
    const error = await response.json();
    showError(error.detail || 'Failed to post GRPO');
  }
}
```

**Validation Rules (Frontend):**
```javascript
function validateForm(items) {
  const errors = [];

  items.forEach(item => {
    const accepted = parseFloat(item.acceptedQty);

    if (isNaN(accepted) || accepted < 0) {
      errors.push(`${item.name}: Invalid quantity`);
    }
  });

  // At least one item must have qty > 0
  const hasValidQty = items.some(i => parseFloat(i.acceptedQty) > 0);
  if (!hasValidQty) {
    errors.push('At least one item must have accepted quantity > 0');
  }

  return errors;
}
```

---

### 4. Get Posting History

**Purpose:** View history of all GRPO postings.

**Request:**
```javascript
GET /api/v1/grpo/history/
GET /api/v1/grpo/history/?vehicle_entry_id=3  // Filter by entry
```

**Response:**
```json
[
  {
    "id": 5,
    "vehicle_entry": 3,
    "entry_no": "VE-2024-003",
    "po_receipt": 2,
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
        "quantity_posted": "95.000",
        "base_entry": null,
        "base_line": null
      }
    ]
  }
]
```

**Status Values:**
| Status | Description | UI Color |
|--------|-------------|----------|
| PENDING | Initiated but not sent | Yellow |
| POSTED | Successfully posted to SAP | Green |
| FAILED | Posting failed | Red |
| PARTIALLY_POSTED | Some items posted | Orange |

---

### 5. Get Posting Detail

**Purpose:** View details of a specific GRPO posting.

**Request:**
```javascript
GET /api/v1/grpo/{posting_id}/
```

**Response:** Same structure as history item.

---

## Error Handling

### Error Response Format

```json
{
  "detail": "Error message here"
}
```

Or for validation errors:
```json
{
  "detail": "Invalid request data",
  "errors": {
    "items": ["At least one item with accepted quantity is required"],
    "branch_id": ["This field is required."]
  }
}
```

### Common Errors and UI Messages

| HTTP Status | Error | User-Friendly Message |
|-------------|-------|----------------------|
| 400 | "Gate entry is not completed" | "This entry is not ready for GRPO. Please complete the gate entry first." |
| 400 | "GRPO already posted" | "This PO has already been posted to SAP." |
| 400 | "No accepted quantities" | "Please enter accepted quantity for at least one item." |
| 401 | Unauthorized | "Session expired. Please login again." |
| 503 | "SAP unavailable" | "SAP system is currently unavailable. Please try again later." |

---

## Configuration

### Branch ID

The `branch_id` is required for posting. Options:
1. **Hardcode** - If single branch
2. **Dropdown** - Fetch from SAP branches API
3. **User Profile** - Store in user preferences

```javascript
// Example: Fetch branches from SAP
const branches = await fetch('/api/sap/branches/', { headers });
// Returns: [{id: 1, name: "Main Branch"}, {id: 4, name: "Warehouse Branch"}]
```

### Warehouse Code

Optional field. Can be:
1. **Hardcoded** per branch
2. **Dropdown** selection
3. **Default** from SAP configuration

---

## State Management

### Recommended State Structure

```javascript
const grpoState = {
  // Pending entries list
  pendingEntries: [],
  pendingLoading: false,

  // Preview data for selected entry
  selectedEntryId: null,
  previewData: [],
  previewLoading: false,

  // Form state for posting
  formData: {
    poReceiptId: null,
    items: [], // {po_item_receipt_id, accepted_qty}
    branchId: null,
    warehouseCode: '',
    comments: ''
  },

  // Posting state
  posting: false,
  postResult: null,
  postError: null,

  // History
  history: [],
  historyLoading: false
};
```

---

## UI Components

### 1. Pending List Component

```
┌────────────────────────────────────────────┐
│ Pending GRPO Entries                    🔄 │
├────────────────────────────────────────────┤
│ ┌────────────────────────────────────────┐ │
│ │ VE-2024-003                      [2]   │ │
│ │ 15 Jan 2024, 10:30 AM                  │ │
│ │ Status: COMPLETED                    ▶ │ │
│ └────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────┐ │
│ │ VE-2024-004                      [1]   │ │
│ │ 15 Jan 2024, 02:15 PM                  │ │
│ │ Status: QC_COMPLETED                 ▶ │ │
│ └────────────────────────────────────────┘ │
└────────────────────────────────────────────┘
```

### 2. Preview/Edit Component

```
┌────────────────────────────────────────────┐
│ ← VE-2024-003                              │
├────────────────────────────────────────────┤
│ PO: PO-001                                 │
│ Supplier: ABC Suppliers Ltd                │
│ Invoice: INV-12345 | Challan: CH-789       │
├────────────────────────────────────────────┤
│ Items                                      │
│ ┌────────────────────────────────────────┐ │
│ │ ITEM001 - Raw Material A               │ │
│ │ Received: 100 KG                       │ │
│ │ Accepted: [____95____] KG              │ │
│ │ Rejected: 5 KG (auto-calculated)       │ │
│ └────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────┐ │
│ │ ITEM002 - Raw Material B               │ │
│ │ Received: 50 KG                        │ │
│ │ Accepted: [____50____] KG              │ │
│ │ Rejected: 0 KG                         │ │
│ └────────────────────────────────────────┘ │
├────────────────────────────────────────────┤
│ Branch: [Dropdown v]                       │
│ Warehouse: [BH-FG      ]                   │
│ Comments: [___________________]            │
├────────────────────────────────────────────┤
│ Attachments (optional)                     │
│ ┌────────────────────────────────────────┐ │
│ │ + Add Files                            │ │
│ │   invoice.pdf              [x Remove]  │ │
│ │   weighbridge_slip.jpg     [x Remove]  │ │
│ └────────────────────────────────────────┘ │
├────────────────────────────────────────────┤
│         [ Cancel ]  [ Post GRPO ]          │
└────────────────────────────────────────────┘
```

### 3. Success Dialog

```
┌────────────────────────────────────────────┐
│              ✓ Success!                    │
│                                            │
│  GRPO posted successfully                  │
│                                            │
│  SAP Document Number: 1001                 │
│  Total Value: ₹47,500.00                   │
│                                            │
│              [ View History ]              │
│              [     Done     ]              │
└────────────────────────────────────────────┘
```

---

## Best Practices

1. **Pre-fill accepted_qty** with received_qty as default
2. **Validate on blur** - Ensure value is non-negative when user leaves input
3. **Show calculated rejected_qty** in real-time
4. **Disable submit** if validation fails
5. **Show loading state** during API calls
6. **Confirm before posting** - Show summary dialog
7. **Handle network errors** gracefully
8. **Cache branch/warehouse** options
9. **Auto-refresh pending list** after successful post

---

## Testing Checklist

- [ ] Pending list shows only entries with pending POs
- [ ] Preview shows correct item details
- [ ] Cannot enter negative accepted_qty
- [ ] Cannot post with all items having 0 quantity
- [ ] Success message shows SAP doc number
- [ ] Error messages are user-friendly
- [ ] Posted POs don't appear in pending list
- [ ] History shows all posted GRPOs
- [ ] Handles network errors gracefully
- [ ] Loading states are visible
