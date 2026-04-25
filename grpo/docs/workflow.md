# GRPO Posting Workflow

This document describes the complete workflow for GRPO posting.

## Process Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        GATE ENTRY PROCESS                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   1. Vehicle Arrival                                                 │
│          ↓                                                           │
│   2. Security Check                                                  │
│          ↓                                                           │
│   3. PO Selection & Item Receipt                                     │
│          ↓                                                           │
│   4. Weighment                                                       │
│          ↓                                                           │
│   5. Material Arrival Slip (submitted to QA)                         │
│          ↓                                                           │
│   6. QC Inspection (Accept/Reject quantities)                        │
│          ↓                                                           │
│   7. Gate Entry Completion                                           │
│          ↓                                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │                    GRPO POSTING                              │   │
│   │                                                              │   │
│   │   8. Get Pending Entries (GET /api/v1/grpo/pending/)         │   │
│   │          ↓                                                   │   │
│   │   9. Preview GRPO Data (GET /api/v1/grpo/preview/{id}/)      │   │
│   │          ↓                                                   │   │
│   │   10. User enters accepted quantities for each item          │   │
│   │          ↓                                                   │   │
│   │   11. Post GRPO to SAP (POST /api/v1/grpo/post/)             │   │
│   │       - Validates accepted_qty <= received_qty               │   │
│   │       - Saves accepted/rejected qty to POItemReceipt         │   │
│   │       - Posts to SAP                                         │   │
│   │          ↓                                                   │   │
│   │   12. SAP Creates GRPO Document                              │   │
│   │          ↓                                                   │   │
│   │   13. Record Posting in Database                             │   │
│   │                                                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites for GRPO Posting

Before GRPO can be posted, the following conditions must be met:

### 1. Gate Entry Status
- Entry status must be `COMPLETED` or `QC_COMPLETED`
- Entry must not be locked

### 2. Security Check
- Security check must exist and be submitted

### 3. Weighment
- Weighment must be completed

### 4. PO Items
- At least one PO item must exist
- Items must have `accepted_qty > 0`

### 5. QC Inspection
- All items must have completed QC
- Items can be ACCEPTED or REJECTED
- Only ACCEPTED quantities are posted to GRPO

---

## Detailed Workflow Steps

### Step 1: Identify Pending Entries

The store/warehouse manager retrieves list of completed gate entries pending GRPO.

```http
GET /api/v1/grpo/pending/
Authorization: Bearer <token>
```

**Response shows:**
- Entry number and time
- Number of POs in the entry
- How many POs are already posted
- How many are pending

### Step 2: Preview GRPO Data

Before posting, review the data that will be sent to SAP.

```http
GET /api/v1/grpo/preview/123/
Authorization: Bearer <token>
```

**Review checklist:**
- [ ] Correct supplier code and name
- [ ] Invoice number and date
- [ ] Item codes and names
- [ ] Note `po_item_receipt_id` for each item (needed for POST)
- [ ] Note `received_qty` for each item (suggested default for accepted_qty)
- [ ] Entry is ready for GRPO (`is_ready_for_grpo: true`)

### Step 3: Post GRPO to SAP

When ready, post the GRPO for each PO receipt. **You must specify the accepted quantity for each item.**

```http
POST /api/v1/grpo/post/
Authorization: Bearer <token>
Content-Type: application/json

{
  "vehicle_entry_id": 123,
  "po_receipt_id": 456,
  "items": [
    {"po_item_receipt_id": 789, "accepted_qty": 950.000},
    {"po_item_receipt_id": 790, "accepted_qty": 500.000}
  ],
  "branch_id": 1,
  "warehouse_code": "WH01",
  "comments": "Goods received per gate entry VE-2024-001"
}
```

**Important Notes:**
- Get `po_item_receipt_id` values from the preview response
- `accepted_qty` may exceed the `received_qty` from preview (no upper-bound validation)
- `branch_id` is required - this is the SAP Branch/Business Place ID (BPLId)
- System automatically calculates `rejected_qty = max(received_qty - accepted_qty, 0)`
- Items with `accepted_qty = 0` are skipped (not posted to SAP)

### Step 4: Handle Response

**Success:**
- Store the `sap_doc_num` for reference
- GRPO record is marked as POSTED
- Line items are recorded

**Failure:**
- Check `error_message` for details
- GRPO record is marked as FAILED
- Can retry after fixing the issue

---

## What Gets Posted to SAP

The following data is sent to SAP Service Layer:

```json
{
  "CardCode": "SUP001",
  "BPL_IDAssignedToInvoice": 1,
  "Comments": "Gate entry completed",
  "DocumentLines": [
    {
      "ItemCode": "ITEM001",
      "Quantity": 950.000,
      "WarehouseCode": "WH01"
    }
  ]
}
```

### Key Points:
1. **CardCode**: Supplier code from PO receipt
2. **BPL_IDAssignedToInvoice**: SAP Branch/Business Place ID (required for multi-branch SAP setup)
3. **Quantity**: The `accepted_qty` provided by the user in the POST request
4. **WarehouseCode**: Optional, from request or SAP default
5. **Rejected quantities are NOT posted** (items with accepted_qty = 0 are skipped)

### Data Flow:
1. User submits GRPO with `items` array containing accepted quantities
2. System validates quantities (must be >= 0; no upper bound vs. received_qty)
3. System updates `POItemReceipt.accepted_qty` and calculates `rejected_qty`
4. System posts to SAP with the accepted quantities
5. On success, GRPO posting is recorded with line items

---

## Handling Multiple POs

A single gate entry can have multiple POs. Each PO is posted as a separate GRPO.

```
Gate Entry VE-2024-001
├── PO-001 (Supplier A) → GRPO 1
├── PO-002 (Supplier B) → GRPO 2
└── PO-003 (Supplier A) → GRPO 3
```

**Best Practice:**
- Post one PO at a time
- Wait for confirmation before posting the next
- Check posting history if unsure

---

## Error Handling

### Common Errors and Solutions

| Error | Cause | Solution |
|-------|-------|----------|
| "At least one item required" | Items array is empty | Add items with accepted quantities |
| "Invalid PO item receipt IDs" | Item IDs don't belong to the PO | Use IDs from preview response |
| "Gate entry is not completed" | Entry status is not COMPLETED/QC_COMPLETED | Complete the gate entry first |
| "GRPO already posted" | GRPO was already posted for this PO | Check history - no action needed |
| "No accepted quantities" | All items have accepted_qty = 0 | Provide at least one item with qty > 0 |
| "SAP system unavailable" | SAP Service Layer is down | Retry later |
| "Item not found in SAP" | Item code doesn't exist in SAP | Verify item code mapping |

### Retry Logic

If posting fails:
1. Check `error_message` in the GRPO record
2. Fix the underlying issue
3. Retry by calling POST `/api/v1/grpo/post/` again
4. System will update existing record (not create duplicate)

---

## Post-Posting Actions

After successful GRPO posting:

1. **Verify in SAP**
   - Login to SAP B1
   - Check Purchase > Goods Receipt PO
   - Find document by `sap_doc_num`

2. **Update Local Records**
   - GRPO posting status = POSTED
   - Lines recorded with quantities
   - Posted timestamp and user recorded

3. **Reporting**
   - Generate gate entry report
   - Include GRPO document numbers
   - Track for accounts/finance

---

## Business Rules

1. **One GRPO per PO per Gate Entry**
   - Cannot post same PO twice from same entry
   - Different entries can post to same PO

2. **Quantity Validation**
   - User must provide accepted_qty for each item during GRPO posting
   - accepted_qty must be >= 0 (no upper bound vs. received_qty)
   - rejected_qty is auto-calculated as max(received_qty - accepted_qty, 0)
   - Items with accepted_qty = 0 are skipped
   - All item IDs must belong to the specified PO receipt

3. **Audit Trail**
   - All postings are logged
   - Posted by user is recorded
   - Timestamp is recorded
   - Failed attempts are recorded with error

4. **No Deletion**
   - GRPO records cannot be deleted
   - SAP document cannot be cancelled from this app
   - Use SAP for any corrections
