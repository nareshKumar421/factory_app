# GRPO Attachment Error Analysis

## Error

```json
{
    "detail": "SAP validation error: {
        \"error\": {
            \"code\": \"-1116\",
            \"details\": [{ \"code\": \"\", \"message\": \"\" }],
            \"message\": \"(200039) Select \\\"OK\\\" in Approve Column After Adding Attachment\"
        }
    }"
}
```

## Root Cause

This is a **SAP Business One approval process error**. It occurs during the **PATCH step** when linking an attachment to an already-posted GRPO document.

### What happens step-by-step:

1. **GRPO is created** via `POST /b1s/v2/PurchaseDeliveryNotes` — this succeeds (status: POSTED)
2. **Attachment file is uploaded** via `POST /b1s/v2/Attachments2` — this succeeds (returns `AbsoluteEntry`)
3. **Attachment is linked to GRPO** via `PATCH /b1s/v2/PurchaseDeliveryNotes({doc_entry})` with payload `{"AttachmentEntry": <absolute_entry>}` — **THIS FAILS**

### Why it fails:

- SAP Business One has an **approval process** configured for PurchaseDeliveryNotes (Goods Receipt PO).
- When you **PATCH** an existing document, SAP treats it as a **document modification**.
- SAP requires that any modification to an approved document must go through the approval workflow again.
- The error message `"Select OK in Approve Column After Adding Attachment"` means SAP expects the document to be explicitly re-approved after the attachment is added.
- The Service Layer API cannot "click OK" like a user would in the SAP GUI — so it throws error code `-1116` / `200039`.

## Where in our code

| Step | File | Method | Line |
|------|------|--------|------|
| Upload attachment | `sap_client/service_layer/attachment_writer.py` | `upload()` | 32-90 |
| **Link to GRPO (FAILS HERE)** | `sap_client/service_layer/attachment_writer.py` | `link_to_document()` | 92-159 |
| Orchestration | `grpo/services.py` | `upload_grpo_attachment()` | 464-468 |

The failing call is in `attachment_writer.py:119`:
```python
response = requests.patch(
    url,  # /b1s/v2/PurchaseDeliveryNotes({doc_entry})
    json={"AttachmentEntry": absolute_entry},
    ...
)
```

## Possible Solutions

### Solution 1: Attach during GRPO creation (Recommended)

Instead of the current 3-step process (create GRPO → upload attachment → PATCH to link), we should:

1. Upload the attachment to `Attachments2` **first**
2. Include `"AttachmentEntry": <absolute_entry>` in the **initial GRPO POST payload**

This avoids the PATCH entirely. The attachment is part of the document creation, so no re-approval is needed.

**Trade-off:** Requires the user to provide attachments at the time of GRPO posting, not after. The current flow allows adding attachments to already-posted GRPOs.

### Solution 2: Two-step with approval handling

Keep the current PATCH approach but add the approval-related fields to the PATCH payload:

```json
{
    "AttachmentEntry": <absolute_entry>,
    "DocumentApprovalRequests": [
        {
            "ApprovalTemplatesID": <template_id>,
            "Status": "Y"
        }
    ]
}
```

**Trade-off:** Requires knowing the approval template ID configured in SAP. May also require specific SAP user permissions.

### Solution 3: Disable approval for attachment-only changes (SAP Admin)

Ask the SAP admin to configure the approval process so that attachment-only changes to PurchaseDeliveryNotes do not trigger re-approval.

**Trade-off:** SAP admin configuration change, not a code change. May not be possible depending on business requirements.

### Solution 4: Hybrid approach (Recommended for best UX)

- **At GRPO posting time:** If attachments are provided, upload them first and include `AttachmentEntry` in the POST payload.
- **After GRPO posting:** For adding attachments later, use Solution 2 or inform users that attachments must be added at posting time.

## Recommendation

**Solution 1 (attach during creation)** is the cleanest fix. It avoids the approval workflow entirely by including the attachment in the original document creation. The code change would be:

1. Accept file uploads in the GRPO POST endpoint
2. Upload files to `Attachments2` before creating the GRPO
3. Add `"AttachmentEntry": absolute_entry` to the `grpo_payload` dict in `services.py:312`
4. Skip the separate PATCH step for these attachments

For attachments added after posting, we'd need to investigate Solution 2 (approval handling in PATCH) or accept that post-creation attachments require SAP admin intervention.
