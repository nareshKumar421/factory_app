# Frontend Changes Required — GRPO Attachment Fix (v1)

## Why This Change?

SAP throws error `200039` ("Select OK in Approve Column After Adding Attachment") when we PATCH an already-posted GRPO to link an attachment. The fix: send attachments **during** GRPO posting, not after.

---

## What Changed in Backend API

The `POST /api/v1/grpo/post/` endpoint now accepts **two formats**:

| Format | When to use |
|--------|-------------|
| `application/json` | No attachments (works exactly as before) |
| `multipart/form-data` | When user selects files to attach |

### New Response Field

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

---

## Frontend Changes (Step by Step)

### 1. Add File Picker to GRPO Posting Form

Add a file input **before** the "Post GRPO" button. Allow multiple files.

```html
<!-- Add this section in the GRPO posting form, before submit button -->
<div class="attachment-section">
  <label>Attachments (optional)</label>
  <input
    type="file"
    multiple
    id="grpo-attachments"
    accept=".pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx"
  />
  <!-- Show selected files list -->
  <div id="selected-files-list"></div>
</div>
```

### 2. Show Selected Files with Remove Option

```javascript
const fileInput = document.getElementById('grpo-attachments');
let selectedFiles = [];

fileInput.addEventListener('change', (e) => {
  selectedFiles = [...e.target.files];
  renderFileList();
});

function renderFileList() {
  const container = document.getElementById('selected-files-list');
  container.innerHTML = selectedFiles.map((file, index) => `
    <div class="file-item">
      <span>${file.name} (${(file.size / 1024).toFixed(1)} KB)</span>
      <button onclick="removeFile(${index})">Remove</button>
    </div>
  `).join('');
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileList();
}
```

### 3. Update the API Call

This is the main change. When files are selected, use `FormData` instead of JSON.

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

**After (with attachment support):**
```javascript
async function postGRPO(payload, files = []) {
  let response;

  if (files.length > 0) {
    // --- MULTIPART/FORM-DATA (with files) ---
    const formData = new FormData();

    // Put all JSON fields inside a "data" key as a JSON string
    formData.append('data', JSON.stringify(payload));

    // Append each file under the "attachments" key
    files.forEach(file => {
      formData.append('attachments', file);
    });

    response = await fetch('/api/v1/grpo/post/', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        // !! DO NOT set Content-Type here !!
        // Browser auto-sets it to multipart/form-data with correct boundary
      },
      body: formData
    });

  } else {
    // --- JSON (no files, same as before) ---
    response = await fetch('/api/v1/grpo/post/', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });
  }

  return response;
}
```

> **CRITICAL:** When using `FormData`, do NOT set `Content-Type` header manually.
> The browser sets it to `multipart/form-data; boundary=...` automatically.
> Setting it manually will break the request.

### 4. Handle the Response (check attachment statuses)

```javascript
const response = await postGRPO(payload, selectedFiles);

if (response.status === 201) {
  const result = await response.json();

  // Show success
  showSuccess(`GRPO Posted! SAP Doc: ${result.sap_doc_num}`);

  // Check if any attachments failed
  if (result.attachments && result.attachments.length > 0) {
    const failed = result.attachments.filter(
      a => a.sap_attachment_status === 'FAILED'
    );
    const linked = result.attachments.filter(
      a => a.sap_attachment_status === 'LINKED'
    );

    if (linked.length > 0) {
      showInfo(`${linked.length} attachment(s) uploaded successfully.`);
    }
    if (failed.length > 0) {
      showWarning(
        `${failed.length} attachment(s) failed to upload to SAP. ` +
        `Files are saved locally. You can retry from GRPO details page.`
      );
    }
  }
} else {
  const error = await response.json();
  showError(error.detail || 'Failed to post GRPO');
}
```

### 5. Update Form State Management

```javascript
// Add to your existing GRPO form state
const grpoFormState = {
  // ... existing fields (vehicleEntryId, poReceiptId, items, branchId, etc.)

  // NEW: track selected files
  attachmentFiles: [],   // Array of File objects from file input
};
```

---

## Attachment Status Values (for display)

| Status | Meaning | Show to User |
|--------|---------|--------------|
| `LINKED` | Attached to GRPO in SAP | Green check — "Uploaded" |
| `FAILED` | SAP upload failed, file saved locally | Red warning — "Failed, retry available" |
| `UPLOADED` | Uploaded but not linked (transient) | Should not normally appear |
| `PENDING` | Not yet uploaded (transient) | Should not normally appear |

---

## What NOT to Change

- **Separate attachment upload** (`POST /api/grpo/<id>/attachments/`) still exists but will hit the same SAP approval error if used after posting. Keep it in the UI only for retrying failed attachments.
- **Attachment retry** (`POST /api/grpo/<id>/attachments/<id>/retry/`) — no changes needed.
- **Attachment list/delete** — no changes needed.
- **JSON-only posting** (no attachments) — fully backward compatible, no changes needed.

---

## Summary of UI Changes

| Screen | Change |
|--------|--------|
| GRPO Posting Form | Add file picker section before submit button |
| GRPO Posting Form | Update API call to use `FormData` when files are selected |
| GRPO Success Dialog | Show attachment upload statuses (linked/failed count) |
| GRPO Detail Page | Show attachment list with status badges |

---

## Quick Test Checklist

- [ ] Post GRPO **without** attachments — should work exactly as before (JSON)
- [ ] Post GRPO **with** 1 attachment — should use FormData, attachment shows as LINKED
- [ ] Post GRPO **with** multiple attachments — all should show in response
- [ ] Remove a file from the picker before posting — removed file should not be sent
- [ ] Post GRPO with an invalid/corrupt file — GRPO still posts, attachment shows FAILED
- [ ] Check response includes `attachments` array even when no files sent (empty array)
