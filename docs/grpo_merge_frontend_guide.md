# GRPO Merge PO — Frontend Integration Guide

## Overview

The backend now supports **merging multiple POs from the same supplier** into a single GRPO document. This guide explains the API changes and how to integrate them in the frontend.

---

## API Changes Summary

| Endpoint | What Changed |
|----------|-------------|
| `GET /api/v1/grpo/pending/` | Now includes `suppliers` grouping with `can_merge` flag |
| `GET /api/v1/grpo/preview/<id>/` | New `?po_receipt_ids=1,2,3` query param for merged preview |
| `POST /api/v1/grpo/post/` | Accepts `po_receipt_ids` (list) instead of `po_receipt_id` |
| `GET /api/v1/grpo/<id>/` | Response now includes `is_merged`, `po_numbers`, `merged_po_receipts` |

---

## 1. Pending GRPO List — Supplier Grouping

### Endpoint
```
GET /api/v1/grpo/pending/
```

### New Response Structure
```json
[
  {
    "vehicle_entry_id": 1,
    "entry_no": "VE-2024-001",
    "status": "COMPLETED",
    "entry_time": "2026-03-27T10:00:00Z",
    "total_po_count": 5,
    "posted_po_count": 1,
    "pending_po_count": 4,
    "is_fully_posted": false,
    "suppliers": [
      {
        "supplier_code": "ZOMATO01",
        "supplier_name": "Zomato",
        "po_count": 3,
        "can_merge": true,
        "po_receipts": [
          {
            "po_receipt_id": 10,
            "po_number": "PO-Z-001",
            "supplier_code": "ZOMATO01",
            "supplier_name": "Zomato",
            "branch_id": 1,
            "item_count": 2
          },
          {
            "po_receipt_id": 11,
            "po_number": "PO-Z-002",
            "supplier_code": "ZOMATO01",
            "supplier_name": "Zomato",
            "branch_id": 1,
            "item_count": 3
          },
          {
            "po_receipt_id": 12,
            "po_number": "PO-Z-003",
            "supplier_code": "ZOMATO01",
            "supplier_name": "Zomato",
            "branch_id": 1,
            "item_count": 1
          }
        ]
      },
      {
        "supplier_code": "SWIGGY01",
        "supplier_name": "Swiggy",
        "po_count": 1,
        "can_merge": false,
        "po_receipts": [
          {
            "po_receipt_id": 13,
            "po_number": "PO-S-001",
            "supplier_code": "SWIGGY01",
            "supplier_name": "Swiggy",
            "branch_id": 2,
            "item_count": 1
          }
        ]
      }
    ]
  }
]
```

### Frontend UI Suggestion
- Show each vehicle entry as a card/row
- Within each entry, group POs by supplier
- If `can_merge` is `true`, show checkboxes next to each PO for multi-select
- Add a "Merge & Post GRPO" button that becomes active when 2+ POs are selected
- Single PO can still be posted individually (backward compatible)

---

## 2. Merged GRPO Preview

### Endpoint
```
GET /api/v1/grpo/preview/<vehicle_entry_id>/?po_receipt_ids=10,11
```

### Query Parameters
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `po_receipt_ids` | comma-separated ints | No | Filter preview to specific POs. Omit for all POs. |

### Response
Returns an array of PO receipts with their items. Same structure as before, but filtered to only the selected POs.

### Frontend UI Suggestion
- When user selects POs to merge, call preview with those IDs
- Show a combined view: all items from all selected POs in one table
- Group items by PO number for clarity
- Show totals across all POs

---

## 3. Post Merged GRPO

### Endpoint
```
POST /api/v1/grpo/post/
```

### Request Body (New)
```json
{
  "vehicle_entry_id": 1,
  "po_receipt_ids": [10, 11],
  "items": [
    {
      "po_item_receipt_id": 100,
      "accepted_qty": "180.000",
      "variety": "TMT-500D",
      "unit_price": "50.00",
      "tax_code": "GST18"
    },
    {
      "po_item_receipt_id": 200,
      "accepted_qty": "90.000",
      "variety": "Grade-A",
      "unit_price": "120.00",
      "tax_code": "GST18"
    }
  ],
  "branch_id": 1,
  "warehouse_code": "WH-01",
  "comments": "Merged GRPO for Zomato",
  "vendor_ref": "VINV-2026-001"
}
```

### Key Changes
| Field | Old | New |
|-------|-----|-----|
| `po_receipt_id` | Required (single int) | **Deprecated** — still works for backward compat |
| `po_receipt_ids` | N/A | **New** — list of ints (preferred) |
| `items` | Items from single PO | Items from **all selected POs** |

### Backward Compatibility
- Sending `po_receipt_id: 42` still works — it's auto-converted to `po_receipt_ids: [42]`
- You can migrate the frontend gradually

### Validation Rules
The backend validates:
- All POs must have the **same `supplier_code`** (same party)
- All POs must have the **same `branch_id`** (SAP requirement)
- No selected PO should already have a POSTED GRPO
- All item IDs must belong to one of the selected POs
- `accepted_qty` must be >= 0 (no upper bound vs. `received_qty`)

### Error Responses
```json
// Different suppliers
{
  "detail": "Cannot merge POs from different suppliers. Found suppliers: {'ZOMATO01', 'SWIGGY01'}"
}

// Different branches
{
  "detail": "Cannot merge POs with different branch IDs. Found branch IDs: {1, 2}"
}

// Already posted
{
  "detail": "GRPO already posted for PO PO-Z-001. SAP Doc Num: 12345"
}
```

### Success Response
```json
{
  "success": true,
  "grpo_posting_id": 42,
  "sap_doc_entry": 9001,
  "sap_doc_num": 9100,
  "sap_doc_total": "22000.00",
  "message": "GRPO posted successfully. SAP Doc Num: 9100",
  "attachments": []
}
```

---

## 4. GRPO Detail — Merged Info

### Endpoint
```
GET /api/v1/grpo/<posting_id>/
```

### New Response Fields
```json
{
  "id": 42,
  "vehicle_entry": 1,
  "entry_no": "VE-2024-001",
  "po_receipt": 10,
  "po_number": "PO-Z-001, PO-Z-002",
  "po_numbers": ["PO-Z-001", "PO-Z-002"],
  "is_merged": true,
  "merged_po_receipts": [
    {
      "id": 10,
      "po_number": "PO-Z-001",
      "supplier_code": "ZOMATO01",
      "supplier_name": "Zomato"
    },
    {
      "id": 11,
      "po_number": "PO-Z-002",
      "supplier_code": "ZOMATO01",
      "supplier_name": "Zomato"
    }
  ],
  "sap_doc_entry": 9001,
  "sap_doc_num": 9100,
  "sap_doc_total": "22000.00",
  "total_amount": "22000.00",
  "status": "POSTED",
  "lines": [
    {
      "id": 1,
      "item_code": "ITEM-A",
      "item_name": "Steel Rod",
      "quantity_posted": "180.000",
      "base_entry": 5001,
      "base_line": 0
    },
    {
      "id": 2,
      "item_code": "ITEM-B",
      "item_name": "Copper Wire",
      "quantity_posted": "90.000",
      "base_entry": 5002,
      "base_line": 0
    }
  ],
  "attachments": []
}
```

### New Fields
| Field | Type | Description |
|-------|------|-------------|
| `is_merged` | boolean | `true` if GRPO contains lines from multiple POs |
| `po_numbers` | string[] | List of all PO numbers in this GRPO |
| `merged_po_receipts` | object[] | Details of each PO receipt included |
| `po_number` | string | Comma-separated PO numbers (for display) |

---

## 5. Multipart/Form-Data (with Attachments)

For merged GRPO with attachments, send as multipart:

```
POST /api/v1/grpo/post/
Content-Type: multipart/form-data

data: {"vehicle_entry_id": 1, "po_receipt_ids": [10, 11], "items": [...], "branch_id": 1}
attachments: <file1>
attachments: <file2>
```

The `data` field contains the JSON as a string. Files go in `attachments` fields.

---

## 6. Recommended Frontend Flow

### Step 1: Pending List
```
User sees: Vehicle Entry VE-2024-001
  └── Zomato (3 POs pending) [can merge]
      ☐ PO-Z-001 (2 items)
      ☐ PO-Z-002 (3 items)
      ☐ PO-Z-003 (1 item)
  └── Swiggy (1 PO pending)
      → PO-S-001 (1 item)  [post individually]
```

### Step 2: User Selects POs to Merge
```
User checks PO-Z-001 and PO-Z-002
→ "Merge & Post GRPO" button activates
```

### Step 3: Preview
```
GET /api/v1/grpo/preview/1/?po_receipt_ids=10,11

Show combined items:
  From PO-Z-001:
    - Steel Rod: 200 KG received, [180] accepted
  From PO-Z-002:
    - Copper Wire: 100 KG received, [90] accepted
```

### Step 4: User Fills Details & Posts
```
POST /api/v1/grpo/post/
{
  "vehicle_entry_id": 1,
  "po_receipt_ids": [10, 11],
  "items": [...all items with accepted_qty and variety...],
  "branch_id": 1
}
```

### Step 5: Success
```
Show: "GRPO posted successfully. SAP Doc Num: 9100"
Show merged badge on GRPO in history
```
