# GRPO API — Version 3 (fv3) Changes

## 1. Overview

This update adds support for three new date fields and a round-off adjustment field when
creating a GRPO (Goods Receipt PO / Purchase Delivery Note) in SAP Business One. It also
fixes an Internal Server Error (HTTP 500) that was returned even when the GRPO was
successfully created in SAP.

---

## 2. Backend Changes

### 2.1 Bug Fix — Internal Server Error on Successful GRPO Post

**File:** `grpo/views.py` — `PostGRPOAPI.post()`

**Root Cause:**
After a successful SAP GRPO creation, the view built a `response_data` dict that contained
already-serialized attachment data (`.data` from `GRPOAttachmentSerializer`). This
pre-serialized data (a list of `OrderedDict`) was then fed into `GRPOPostResponseSerializer`,
which contains a nested `GRPOAttachmentSerializer` (a `ModelSerializer`). DRF's `FileField`
inside that serializer tried to call `.url` on a plain string instead of a Django `FieldFile`
object, raising `AttributeError: 'str' object has no attribute 'url'` — surfacing as HTTP 500.

**Fix:**
Pass the raw queryset (`grpo_posting.attachments.all()`) directly in `response_data` so that
`GRPOPostResponseSerializer` receives model instances and can serialize them correctly. Also,
the `request` context is now forwarded to the serializer so that file URLs are returned as
absolute URIs.

```python
# Before (broken)
"attachments": GRPOAttachmentSerializer(
    grpo_posting.attachments.all(), many=True
).data,
...
GRPOPostResponseSerializer(response_data).data

# After (fixed)
"attachments": grpo_posting.attachments.all(),
...
GRPOPostResponseSerializer(response_data, context={"request": request}).data
```

---

### 2.2 New Fields Added

#### `grpo/serializers.py` — `GRPOPostRequestSerializer`

Four new optional fields added to the request serializer:

| Field        | Type          | SAP Field  | Description                                      |
|--------------|---------------|------------|--------------------------------------------------|
| `doc_date`   | `DateField`   | `DocDate`  | Posting Date. Defaults to today in SAP if omitted. |
| `doc_due_date` | `DateField` | `DocDueDate` | Due Date of the document.                      |
| `tax_date`   | `DateField`   | `TaxDate`  | Document Date (used for tax reporting).          |
| `should_roundoff` | `BooleanField` | `RoundDif` (auto-calculated) | If `true`, backend calculates `RoundDif` automatically from the line items subtotal. |

#### `grpo/services.py` — `GRPOService.post_grpo()`

Four new optional parameters added:

```python
def post_grpo(
    self,
    ...
    doc_date: Optional[str] = None,
    doc_due_date: Optional[str] = None,
    tax_date: Optional[str] = None,
    should_roundoff: bool = False,
) -> GRPOPosting:
```

Date fields are included in the SAP payload when provided. When `should_roundoff=True`,
the backend calculates `RoundDif` automatically:

```python
# Compute subtotal from document lines, then round to nearest integer
subtotal = sum(Decimal(line["Quantity"]) * Decimal(line["UnitPrice"]) for line in document_lines)
rounded  = subtotal.quantize(Decimal('1'), rounding='ROUND_HALF_UP')
round_dif = float(rounded - subtotal)
if round_dif != 0:
    grpo_payload["RoundDif"] = round_dif
```

Note: The subtotal is calculated from lines that have `UnitPrice` set. Lines without a
price contribute `0` to the subtotal (their price is managed by SAP from the base PO).

#### `sap_client/dtos.py` — `GRPORequestDTO`

Two new optional fields added to the DTO for completeness:

```python
tax_date: Optional[str] = None
round_dif: Optional[float] = None
```

---

## 3. API Request Format

### Endpoint

```
POST /api/grpo/post/
Content-Type: application/json
```

### Updated Request Payload

```json
{
  "vehicle_entry_id": 123,
  "po_receipt_id": 456,
  "branch_id": 1,
  "warehouse_code": "WH-01",
  "vendor_ref": "INV-2026-001",
  "comments": "Received in good condition",

  "doc_date": "2026-03-13",
  "doc_due_date": "2026-03-20",
  "tax_date": "2026-03-13",
  "should_roundoff": true,

  "items": [
    {
      "po_item_receipt_id": 789,
      "accepted_qty": 100.000,
      "unit_price": 250.000000,
      "tax_code": "GST18",
      "gl_account": "1310000",
      "variety": "Grade-A"
    }
  ],

  "extra_charges": [
    {
      "expense_code": 1,
      "amount": 500.00,
      "remarks": "Freight",
      "tax_code": "GST18"
    }
  ]
}
```

### Multipart Form-Data (with attachments)

```
POST /api/grpo/post/
Content-Type: multipart/form-data

data: <JSON string of the payload above>
attachments: <binary file>
attachments: <binary file>   (multiple files allowed)
```

### SAP Payload (sent to Service Layer)

```json
{
  "CardCode": "V001",
  "BPL_IDAssignedToInvoice": 1,
  "NumAtCard": "INV-2026-001",
  "DocDate": "2026-03-13",
  "DocDueDate": "2026-03-20",
  "TaxDate": "2026-03-13",
  "RoundDif": -0.47,
  "Comments": "App: FactoryApp v2 | User: John Doe (john) | PO: PO-100 | Gate Entry: GE-001 | Received in good condition",
  "AttachmentEntry": 42,
  "DocumentLines": [
    {
      "ItemCode": "RM001",
      "Quantity": "100.000",
      "UnitPrice": 250.0,
      "TaxCode": "GST18",
      "AccountCode": "1310000",
      "BaseEntry": 12345,
      "BaseLine": 0,
      "BaseType": 22,
      "WarehouseCode": "WH-01",
      "U_Variety": "Grade-A",
      "U_SchemeAgst": "Grade-A"
    }
  ],
  "DocumentAdditionalExpenses": [
    {
      "ExpenseCode": 1,
      "LineTotal": 500.0,
      "Remarks": "Freight",
      "TaxCode": "GST18"
    }
  ]
}
```

### Success Response (HTTP 201)

```json
{
  "success": true,
  "grpo_posting_id": 99,
  "sap_doc_entry": 54321,
  "sap_doc_num": 67890,
  "sap_doc_total": "50000.00",
  "message": "GRPO posted successfully. SAP Doc Num: 67890",
  "attachments": []
}
```

---

## 4. Frontend Changes Required

### 4.1 New Fields to Send

Add the following **optional** fields to the GRPO post request payload:

| Field         | Type            | Format       | Required | Notes                                      |
|---------------|-----------------|--------------|----------|--------------------------------------------|
| `doc_date`    | string (date)   | `YYYY-MM-DD` | No       | Posting date. SAP defaults to today.       |
| `doc_due_date`| string (date)   | `YYYY-MM-DD` | No       | Due date of the GRPO document.             |
| `tax_date`    | string (date)   | `YYYY-MM-DD` | No       | Tax/document date for reporting purposes.  |
| `should_roundoff` | boolean | —        | No       | If `true`, backend auto-calculates `RoundDif` from line items subtotal. Default: `false`. |

### 4.2 Updated JSON Payload Structure

Add the new fields alongside the existing ones at the **root level** of the request body:

```json
{
  "vehicle_entry_id": 123,
  "po_receipt_id": 456,
  "branch_id": 1,

  "doc_date": "2026-03-13",
  "doc_due_date": "2026-03-20",
  "tax_date": "2026-03-13",
  "should_roundoff": true,

  "items": [ ... ],
  ...
}
```

All four fields are **optional**. If not sent, SAP will use its own defaults (e.g., today's
date for `DocDate`).

### 4.3 Frontend Validation

- **Date fields** (`doc_date`, `doc_due_date`, `tax_date`): Must be a valid date in
  `YYYY-MM-DD` format if provided. Use a date picker and format before sending.
- **`should_roundoff`**: Boolean (`true`/`false`). When `true`, the backend automatically
  computes the rounding difference from the line items subtotal and includes it in the
  SAP payload. No manual amount is needed from the frontend.

### 4.4 No Breaking Changes

All new fields are optional. Existing requests without these fields will continue to work
exactly as before.
