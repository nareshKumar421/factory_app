# GRPO Error Codes and Troubleshooting

This document lists all error codes and messages that can be returned by the GRPO API.

## HTTP Status Codes

| Status | Meaning |
|--------|---------|
| 200 | Success - Request completed |
| 201 | Created - GRPO posted successfully |
| 400 | Bad Request - Invalid input or business rule violation |
| 401 | Unauthorized - Missing or invalid authentication |
| 404 | Not Found - Resource doesn't exist |
| 502 | Bad Gateway - SAP error |
| 503 | Service Unavailable - SAP system down |

---

## Error Categories

### 1. Authentication Errors (401)

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "Authentication credentials were not provided" | No token in header | Add `Authorization: Bearer <token>` |
| "Given token not valid for any token type" | Token expired/invalid | Get a new token from login API |
| "User is not authenticated" | Authentication failed | Re-authenticate |

**Example Response:**
```json
{
  "detail": "Authentication credentials were not provided."
}
```

---

### 2. Validation Errors (400)

#### Missing Required Fields

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "vehicle_entry_id is required" | Missing field | Add vehicle_entry_id to request |
| "po_receipt_id is required" | Missing field | Add po_receipt_id to request |
| "items is required" | Missing items array | Add items array with accepted quantities |
| "branch_id is required" | Missing branch ID | Add branch_id (SAP BPLId) to request |

**Example Response:**
```json
{
  "detail": "Invalid request data",
  "errors": {
    "po_receipt_id": ["This field is required."]
  }
}
```

#### Items Validation Errors

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "At least one item with accepted quantity is required" | Empty items array | Add at least one item with accepted_qty |
| "po_item_receipt_id is required" | Missing ID in item | Include po_item_receipt_id for each item |
| "accepted_qty is required" | Missing quantity in item | Include accepted_qty for each item |
| "Ensure this value is greater than or equal to 0" | Negative accepted_qty | Use 0 or positive value |

**Example Response:**
```json
{
  "detail": "Invalid request data",
  "errors": {
    "items": ["At least one item with accepted quantity is required"]
  }
}
```

#### Business Rule Violations

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "Vehicle entry {id} not found" | Invalid vehicle_entry_id | Verify the ID exists |
| "PO receipt {id} not found for this vehicle entry" | Invalid po_receipt_id | Verify PO belongs to the entry |
| "Invalid PO item receipt IDs: {ids}" | Item IDs don't belong to PO | Use po_item_receipt_id from preview API |
| "Gate entry is not completed. Current status: {status}" | Entry not ready | Complete gate entry first |
| "GRPO already posted for PO {number}. SAP Doc Num: {num}" | Duplicate posting | No action needed - already done |
| "No accepted quantities to post for this PO" | All items have accepted_qty = 0 | Provide at least one item with qty > 0 |

**Example Responses:**
```json
{
  "detail": "Gate entry is not completed. Current status: IN_PROGRESS"
}
```

```json
{
  "detail": "Invalid PO item receipt IDs: {101, 102}"
}
```

---

### 3. SAP Errors

#### SAP Validation Errors (400)

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "SAP validation error: {details}" | SAP rejected the data | Check SAP validation rules |
| "Specify an active branch [OPDN.BPLId]" | Missing or invalid branch ID | Provide valid branch_id in request |
| "Item {code} is not active in SAP" | Item deactivated | Activate item in SAP |
| "Supplier {code} is not authorized" | Supplier issue | Check supplier status in SAP |
| "Quantity exceeds open PO quantity" | Over-receipt | Verify quantities |

**Example Response:**
```json
{
  "detail": "SAP validation error: Item ITEM001 is not active in SAP"
}
```

#### SAP Connection Errors (503)

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "SAP system is currently unavailable. Please try again later." | SAP Service Layer down | Wait and retry |

**Example Response:**
```json
{
  "detail": "SAP system is currently unavailable. Please try again later."
}
```

#### SAP Data Errors (502)

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "SAP error: {details}" | SAP internal error | Check SAP logs |
| "Failed to create GRPO in SAP" | General SAP failure | Check SAP connection and logs |

**Example Response:**
```json
{
  "detail": "SAP error: Failed to create document"
}
```

---

### 4. Not Found Errors (404)

| Error Message | Cause | Solution |
|---------------|-------|----------|
| "GRPO posting not found" | Invalid posting ID | Verify the posting ID |
| "Vehicle entry not found" | Invalid entry ID | Verify the entry ID |

**Example Response:**
```json
{
  "detail": "GRPO posting not found"
}
```

---

## Troubleshooting Guide

### Problem: "Gate entry is not completed"

**Symptoms:**
- GRPO posting fails with status 400
- Error mentions entry status

**Diagnosis:**
1. Check entry status: `GET /api/v1/grpo/preview/{id}/`
2. Look at `entry_status` field

**Resolution:**
1. Complete security check if pending
2. Complete weighment if pending
3. Submit arrival slips for all items
4. Complete QC inspections
5. Call gate completion API

---

### Problem: "No accepted quantities"

**Symptoms:**
- GRPO posting fails
- All items in request have accepted_qty = 0

**Diagnosis:**
1. Check the `items` array in your request
2. Verify accepted_qty values are > 0 for at least one item

**Resolution:**
1. Provide accepted_qty > 0 for items you want to post
2. If all goods should be rejected, no GRPO is needed
3. Handle rejected goods through returns process

---

### Problem: "Invalid PO item receipt IDs"

**Symptoms:**
- GRPO posting fails with 400 error
- Error lists invalid IDs

**Diagnosis:**
1. Verify po_item_receipt_id values in your request
2. Check they belong to the correct po_receipt_id

**Resolution:**
1. Use the preview API to get correct po_item_receipt_id values
2. Ensure items match the specified PO receipt

---

### Problem: "SAP system unavailable"

**Symptoms:**
- 503 error
- Cannot post any GRPO

**Diagnosis:**
1. Check SAP Service Layer status
2. Check network connectivity
3. Verify SAP credentials

**Resolution:**
1. Wait for SAP to come online
2. Contact SAP administrator
3. Retry posting after SAP is available

---

### Problem: "GRPO already posted"

**Symptoms:**
- 400 error when posting
- Message shows existing SAP Doc Num

**Diagnosis:**
- GRPO was already successfully posted

**Resolution:**
1. No action needed
2. Check posting history for details
3. Use provided SAP Doc Num for reference

---

## Error Logging

All errors are logged with:
- Timestamp
- User ID
- Request details
- Full error message
- Stack trace (for server errors)

**Log location:** Check Django logs at configured location

**Log format:**
```
2024-01-15 14:30:00 ERROR grpo.services: SAP validation error posting GRPO: Item ITEM001 is not active
```

---

## Support Escalation

If issue persists after troubleshooting:

1. **Collect Information:**
   - Error message and status code
   - Request payload
   - Vehicle entry ID and PO number
   - Timestamp of error

2. **Check Logs:**
   - Application logs
   - SAP Service Layer logs
   - Network logs (if connectivity issue)

3. **Escalate To:**
   - Level 1: Application support team
   - Level 2: SAP integration team
   - Level 3: SAP administrator
