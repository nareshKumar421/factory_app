# GRPO Attachment Fix — Changelog

## Problem

When posting a GRPO and then adding attachments, SAP throws error:

```
(200039) Select "OK" in Approve Column After Adding Attachment
```

**Root cause:** The old flow PATCHed an already-posted GRPO document to link the attachment. SAP's approval process treated this as a document modification and required re-approval, which the API cannot do.

## Solution Applied

**Solution 1: Attach during GRPO creation** — attachments are now uploaded to SAP *before* the GRPO document is created, and the `AttachmentEntry` is included in the initial `POST` payload. This avoids modifying the document after creation.

---

## Files Changed

### 1. `grpo/views.py` — PostGRPOAPI

**What changed:**
- Added `MultiPartParser`, `FormParser` to `parser_classes` (alongside existing `JSONParser`)
- The endpoint now supports two request formats:
  - **JSON** (`application/json`) — backward compatible, no attachments
  - **multipart/form-data** — JSON fields in a `data` part + files in `attachments` parts
- Files are extracted from `request.FILES.getlist("attachments")` and passed to the service
- Response now includes `attachments` array showing status of each uploaded attachment

**Why:**
The frontend needs to send files together with JSON data in a single request. `multipart/form-data` is the standard way to do this.

---

### 2. `grpo/services.py` — post_grpo()

**What changed:**
- New parameter: `attachments: Optional[list] = None`
- **Before** creating the GRPO in SAP, the service now:
  1. Saves each attachment file locally (creates `GRPOAttachment` record with status `PENDING`)
  2. Uploads each file to SAP `Attachments2` endpoint (status becomes `UPLOADED`)
  3. Takes the `AbsoluteEntry` from the last successful upload
  4. Adds `"AttachmentEntry": <absolute_entry>` to the GRPO payload
- **After** GRPO creation succeeds, marks all uploaded attachments as `LINKED`
- If an attachment upload fails, it's saved locally with `FAILED` status but the GRPO still posts (without that attachment)

**Why:**
This is the core fix. By including `AttachmentEntry` in the original `POST /b1s/v2/PurchaseDeliveryNotes` request, SAP creates the document with the attachment in one step. No PATCH = no approval re-trigger.

---

### 3. `grpo/serializers.py` — GRPOPostResponseSerializer

**What changed:**
- Added `attachments` field (`GRPOAttachmentSerializer`, many=True, read_only=True)

**Why:**
The response now includes the attachment statuses so the frontend can show the user which attachments succeeded/failed.

---

### 4. `grpo/docs/frontend_guide.md`

**What changed:**
- Updated POST `/api/v1/grpo/post/` documentation with both request formats
- Added `multipart/form-data` JavaScript example using `FormData`
- Added attachment file picker to the UI mockup
- Added note about checking attachment statuses in the response

---

## Frontend Changes Required

### API Call Changes

**Before (JSON only):**
```javascript
const response = await fetch('/api/v1/grpo/post/', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify(payload)
});
```

**After (with attachments):**
```javascript
const formData = new FormData();

// JSON data goes in the "data" field as a JSON string
formData.append('data', JSON.stringify({
  vehicle_entry_id: 3,
  po_receipt_id: 2,
  items: [
    { po_item_receipt_id: 3, accepted_qty: 95 },
    { po_item_receipt_id: 4, accepted_qty: 50 }
  ],
  branch_id: 4,
  warehouse_code: "BH-FG",
  comments: "Gate entry completed"
}));

// Append each file
files.forEach(file => {
  formData.append('attachments', file);
});

const response = await fetch('/api/v1/grpo/post/', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    // DO NOT set Content-Type — browser sets it automatically with boundary
  },
  body: formData
});
```

> **Important:** When using `FormData`, do NOT set `Content-Type` header manually. The browser sets it to `multipart/form-data` with the correct boundary automatically.

### UI Changes

1. **Add file picker** to the GRPO posting form (before the submit button)
   - Allow multiple file selection
   - Show selected files with remove option
   - Optional — not required to post GRPO

2. **Handle response attachments** — check the `attachments` array in the success response
   - Show warning if any attachment has `sap_attachment_status: "FAILED"`
   - User can retry failed attachments later via the existing retry endpoint

3. **Backward compatible** — if no files are selected, send JSON as before (no changes needed)

### Response Changes

The success response now includes an `attachments` array:

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

### Attachment Status Values

| Status | Meaning | Frontend Action |
|--------|---------|-----------------|
| `LINKED` | Successfully attached to GRPO in SAP | Show green checkmark |
| `FAILED` | Upload to SAP failed (file saved locally) | Show warning + retry button |
| `UPLOADED` | Uploaded to SAP but not yet linked | Should not appear (transient state) |
| `PENDING` | Not yet uploaded | Should not appear (transient state) |

---

## What Still Works (No Changes)

- **POST-creation attachment upload** (`POST /api/grpo/<id>/attachments/`) — still works for adding files after posting, but will fail if SAP approval is configured (same error as before)
- **Attachment retry** (`POST /api/grpo/<id>/attachments/<id>/retry/`) — still works for retrying failed attachments
- **Attachment list/delete** — no changes
- **JSON-only GRPO posting** — fully backward compatible, no attachments = no change in behavior

---

## SAP Flow Comparison

### Before (broken):
```
1. POST /b1s/v2/PurchaseDeliveryNotes     → Creates GRPO (succeeds)
2. POST /b1s/v2/Attachments2              → Uploads file (succeeds)
3. PATCH /b1s/v2/PurchaseDeliveryNotes(X) → Links attachment (FAILS: error 200039)
```

### After (fixed):
```
1. POST /b1s/v2/Attachments2              → Uploads file (succeeds, returns AbsoluteEntry)
2. POST /b1s/v2/PurchaseDeliveryNotes     → Creates GRPO WITH AttachmentEntry (succeeds)
   payload includes: { ..., "AttachmentEntry": 789 }
```

No PATCH step = no approval re-trigger = no error.
