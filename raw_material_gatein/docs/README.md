# Raw Material Gate-In Module

## Overview

The **Raw Material Gate-In Module** handles Purchase Order (PO) receipt processing for raw materials. It integrates with SAP to fetch PO data and validates received quantities against ordered quantities.

---

## Models

### POReceipt

| Field | Type | Description |
|-------|------|-------------|
| `vehicle_entry` | ForeignKey | Link to VehicleEntry (CASCADE) |
| `po_number` | CharField(30) | SAP Purchase Order number |
| `supplier_code` | CharField(30) | SAP Supplier code |
| `supplier_name` | CharField(150) | Supplier name |
| `invoice_no` | CharField(50) | Invoice number |
| `invoice_date` | DateField | Invoice date |
| `challan_no` | CharField(50) | Delivery challan number |
| `created_at` | DateTimeField | Auto-generated |
| `created_by` | ForeignKey | User who created |

**Constraints:** Unique together (vehicle_entry, po_number)

### POItemReceipt

| Field | Type | Description |
|-------|------|-------------|
| `po_receipt` | ForeignKey | Link to POReceipt (CASCADE) |
| `po_item_code` | CharField(50) | SAP PO item code |
| `item_name` | CharField(200) | Item description |
| `ordered_qty` | DecimalField | Quantity ordered |
| `received_qty` | DecimalField | Quantity received |
| `accepted_qty` | DecimalField | Quantity accepted (after QC) |
| `rejected_qty` | DecimalField | Quantity rejected (after QC) |
| `short_qty` | DecimalField | Auto-calculated (ordered - received) |
| `uom` | CharField(20) | Unit of measure |
| `created_at` | DateTimeField | Auto-generated |
| `created_by` | ForeignKey | User who created |

**Constraints:** Unique together (po_receipt, po_item_code)

---

## API Documentation

### Base URL
```
/api/v1/raw-material-gatein/
```

### Headers Required
```
Authorization: Bearer <access_token>
Company-Code: <company_code>
```

---

### 1. Receive PO Items

```
POST /api/v1/raw-material-gatein/gate-entries/{gate_entry_id}/po-receipts/
```

**Request Body:**
```json
{
    "po_number": "PO-2026-001",
    "supplier_code": "SUP001",
    "supplier_name": "ABC Suppliers Pvt Ltd",
    "items": [
        {
            "po_item_code": "ITEM001",
            "item_name": "Raw Material A",
            "ordered_qty": 1000.000,
            "received_qty": 950.000,
            "uom": "KG"
        },
        {
            "po_item_code": "ITEM002",
            "item_name": "Raw Material B",
            "ordered_qty": 500.000,
            "received_qty": 500.000,
            "uom": "LTR"
        }
    ]
}
```

**Response (201 Created):**
```json
{
    "message": "PO items received successfully"
}
```

**Error Responses:**

| Status | Message |
|--------|---------|
| 400 | `Invalid PO item {item_code}` |
| 400 | `Received quantity exceeds remaining quantity` |
| 502 | `Failed to retrieve PO data from SAP` |
| 503 | `SAP system is currently unavailable` |

**Note:** This endpoint:
1. Validates items against SAP remaining quantities
2. Creates POReceipt and POItemReceipt records
3. Changes gate entry status from `IN_PROGRESS` to `QC_PENDING`

---

### 2. List PO Receipts for Gate Entry

```
GET /api/v1/raw-material-gatein/gate-entries/{gate_entry_id}/po-receipts/view/
```

**Response (200 OK):**
```json
[
    {
        "id": 1,
        "po_number": "PO-2026-001",
        "supplier_code": "SUP001",
        "supplier_name": "ABC Suppliers Pvt Ltd",
        "items": [
            {
                "id": 1,
                "po_item_code": "ITEM001",
                "item_name": "Raw Material A",
                "ordered_qty": "1000.000",
                "received_qty": "950.000",
                "short_qty": "50.000",
                "uom": "KG"
            },
            {
                "id": 2,
                "po_item_code": "ITEM002",
                "item_name": "Raw Material B",
                "ordered_qty": "500.000",
                "received_qty": "500.000",
                "short_qty": "0.000",
                "uom": "LTR"
            }
        ]
    }
]
```

---

### 3. Complete Gate Entry

```
POST /api/v1/raw-material-gatein/gate-entries/{gate_entry_id}/complete/
```

**Request Body:** None required

**Response (200 OK):**
```json
{
    "message": "Gate entry completed successfully"
}
```

**Validation Rules:**
- Entry type must be `RAW_MATERIAL`
- Security check must be completed and submitted
- PO receipts must exist
- All QC inspections must be completed (PASSED or FAILED)
- Weighment must be recorded

---

## Raw Material Gate-In Flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    RAW MATERIAL GATE-IN FLOW                              │
└──────────────────────────────────────────────────────────────────────────┘

    ┌──────────────┐
    │ Gate Entry   │
    │   Created    │ ──► entry_type = "RAW_MATERIAL"
    └──────┬───────┘     Status: DRAFT
           │
           ▼
    ┌──────────────┐
    │   Security   │
    │    Check     │ ──► Vehicle inspection, alcohol test
    └──────┬───────┘     Status: IN_PROGRESS
           │
           ▼
    ┌──────────────┐
    │  Weighment   │
    │   (Gross)    │ ──► First weighment - loaded vehicle
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │  PO Receipt  │
    │   (SAP)      │ ──► POST /po-receipts/
    └──────┬───────┘     Status: QC_PENDING
           │
           ▼
    ┌──────────────┐
    │     QC       │
    │ Inspection   │ ──► Quality check for each item
    └──────┬───────┘     Status: QC_COMPLETED (when all done)
           │
           ▼
    ┌──────────────┐
    │  Weighment   │
    │   (Tare)     │ ──► Second weighment - empty vehicle
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │   Complete   │
    │     Entry    │ ──► POST /complete/
    └──────────────┘     Status: COMPLETED, is_locked = True
```

---

## Quantity Validation

Implemented in `services/validations.py` and applied on every PO-receipt write:

1. `received_qty > 0` — always, in every company
2. `received_qty <= remaining_qty * 1.10` — 110% of what is still **open** on the
   SAP PO line (`POR1."OpenQty"`), not 110% of the quantity originally ordered.
   **Only in the companies SAP enforces it in** — see "Which companies" below.
3. `short_qty = ordered_qty - received_qty` (auto-calculated on save)

Both `ordered_qty` and `remaining_qty` are read from SAP; the request body's
`ordered_qty` is accepted but ignored, so a client cannot widen its own ceiling.

### Why open quantity, and why 110%

`SBO_SP_TransactionNotification` refuses the GRPO at posting time when

```sql
PDN1."Quantity" > PDN1."BaseOpnQty" * 1.10   -- error 200017 (posted GRPO)
DRF1."Quantity" > DRF1."BaseOpnQty" * 1.10   -- error 1120023 (draft)
```

`BaseOpnQty` is the PO line's open quantity. The procedure's message reads "GRPO
Quantity cannot be greater than the PO quantity + 10%", which is what led this
module to originally cap on the ordered quantity — a far larger number on a
mostly-consumed line. A 12,000 PCS receipt onto a line with 9,000 PCS open of
150,000 ordered passed a 165,000 ceiling here and was then rejected by SAP.

The rule is enforced twice: at gate-in, and again in `GRPOService.post_grpo`
against a freshly read `OpenQty`, because open quantity moves between the two
(another GRPO can consume the same line, and QC can raise the accepted quantity).

### Which companies

`settings.GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES`, default `JIVO_OIL` only.

SAP does not enforce this everywhere. As of 2026-09-08 the posted-GRPO rule (PDN1,
error 200017) is **commented out** in the Mart and Beverages copies of the
procedure; the rule that survives there guards GRPO **drafts** (DRF1, error
1120023), which the Service Layer never creates. Turning the gate on in those
companies would block receipts SAP accepts today, so it follows SAP company by
company. Widen the setting if the SAP side is re-enabled.

The screen is told per PO rather than hard-coding the list: `POSerializer` returns
`over_receipt_enforced`, and both gate pages skip the client-side cap when it is
false or absent — so a stale frontend never blocks a receipt the server would take.

### Exemptions

Within an enforced company, SAP waves some vendors through its own check, so the
gate must too or it would block receipts SAP would accept:

- BP group 101 (`BRANCH VENDOR`) — the intercompany/branch legs
- Named vendors, per company, in `settings.GRPO_OVER_RECEIPT_EXEMPT_VENDORS`

These are hand-maintained inside the stored procedure, so keep the setting in step
with it. A failed BP-group lookup does **not** grant an exemption.

---

## Module Structure

```
raw_material_gatein/
├── __init__.py
├── apps.py
├── models/
│   ├── __init__.py
│   ├── po_receipt.py       # POReceipt model
│   └── po_item_receipt.py  # POItemReceipt model
├── serializers.py          # Request/Response serializers
├── views.py                # API views
├── urls.py                 # URL routing
├── services/
│   ├── __init__.py
│   ├── validation.py       # Quantity validation
│   └── completion.py       # Gate entry completion
├── admin.py                # Admin configuration
└── migrations/
```

---

## Related Modules

| Module | Relationship |
|--------|-------------|
| `sap_client` | Fetches open POs and validates quantities |
| `quality_control` | QC inspection for each PO item |
| `weighment` | Gross and tare weight measurement |
