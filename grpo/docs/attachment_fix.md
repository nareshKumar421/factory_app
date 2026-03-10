# GRPO Attachment Fix — SAP Error (200019)

## Problem

When posting a GRPO to SAP, the following error was returned:

```json
{
  "error": {
    "code": "-1116",
    "message": "(200019) Please Attach its Receiving"
  }
}
```

SAP has a Transaction Notification (stored procedure) that validates GRPO documents
must have an `AttachmentEntry` linked at creation time. The previous implementation
uploaded attachments **after** GRPO creation, which SAP rejected.

## Root Cause

The original flow was:

1. Create GRPO in SAP (without attachments)
2. Upload attachments to SAP Attachments2 endpoint
3. Link attachments to GRPO via PATCH

SAP's custom validation requires `AttachmentEntry` to be present **in the GRPO
payload at creation time**, not attached afterward.

## Solution

Changed the flow to:

1. Upload attachment(s) to SAP Attachments2 endpoint **first** → get `AbsoluteEntry`
2. Include `AttachmentEntry: <AbsoluteEntry>` in the GRPO creation payload
3. SAP creates the GRPO with the attachment already linked

### Files Changed

| File | Change |
|------|--------|
| `grpo/views.py` | `PostGRPOAPI` now accepts `multipart/form-data` with files in `attachments` field and JSON data in `data` field. Also supports plain `application/json` (backward compatible). |
| `grpo/services.py` | `post_grpo()` accepts optional `attachments` parameter (list of Django `UploadedFile`). Uploads them to SAP before creating the GRPO, then includes `AttachmentEntry` in the payload. |
| `sap_client/service_layer/attachment_writer.py` | Added fallback: if multipart file upload fails with error -43 (SAP attachment folder path issue), falls back to JSON metadata entry creation. Added `_create_attachment_entry()` and `_get_attachment_source_path()` methods. Also added MIME type detection for file uploads. |

### SAP Attachment Upload — Fallback Mechanism

The SAP Attachments2 endpoint supports two upload modes:

1. **Multipart file upload** (preferred) — sends the actual file binary
2. **JSON metadata entry** (fallback) — creates an attachment entry record with
   `SourcePath`, `FileName`, and `FileExtension` fields

The fallback is triggered when multipart upload fails with SAP error `-43`
(typically caused by the SAP server's attachment folder path not being
writable from the Service Layer process).

## API Usage

### With Attachments (multipart/form-data)

```
POST /api/v1/grpo/post/
Content-Type: multipart/form-data

data: {
  "vehicle_entry_id": 94,
  "po_receipt_id": 55,
  "branch_id": 2,
  "items": [
    {
      "po_item_receipt_id": 97,
      "accepted_qty": 200,
      "unit_price": 0.22,
      "tax_code": "CG+SG@18",
      "gl_account": "1103005"
    }
  ]
}
attachments: <file1>
attachments: <file2>  (optional, multiple files supported)
```

### Without Attachments (JSON — backward compatible)

```
POST /api/v1/grpo/post/
Content-Type: application/json

{
  "vehicle_entry_id": 94,
  "po_receipt_id": 55,
  "branch_id": 2,
  "items": [...]
}
```

### curl Example (multipart with attachment)

```bash
curl -X POST "http://localhost:8000/api/v1/grpo/post/" \
  -H "Authorization: Bearer <token>" \
  -H "Company-Code: JIVO_OIL" \
  -F 'data={"vehicle_entry_id":94,"po_receipt_id":55,"branch_id":2,"items":[{"po_item_receipt_id":97,"accepted_qty":200}]}' \
  -F "attachments=@/path/to/receiving_doc.pdf"
```

## SAP Payload (with attachment)

The GRPO payload sent to SAP now includes the `AttachmentEntry` field:

```json
{
  "CardCode": "VENDA000948",
  "BPL_IDAssignedToInvoice": 2,
  "Comments": "App: FactoryApp v2 | User: ...",
  "AttachmentEntry": 116689,
  "DocumentLines": [...]
}
```

## Testing Verification

- Original error `(200019) Please Attach its Receiving` — **resolved**
- Multipart form data with attachments — **working**
- JSON-only requests (no attachments) — **backward compatible**
- SAP Attachments2 fallback (JSON metadata) — **working** when multipart upload returns -43
