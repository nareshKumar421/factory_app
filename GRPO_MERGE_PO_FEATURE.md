# Feature: Merge Multiple POs into Single GRPO (Same Party)

## Problem Statement

Currently, GRPO posting works **one PO at a time** — each PO receipt creates a separate GRPO in SAP. When a party (e.g., Zomato) has multiple POs (say 5), and the user wants to receive goods against 2 or more POs in a single GRPO, there is no way to do that.

## Goal

Allow users to **select multiple POs from the same party/supplier** and post them as a **single GRPO document** to SAP.

**Example:** Party "Zomato" has 5 open POs. User selects PO-001 and PO-003 → system creates one GRPO with lines from both POs.

---

## Current Flow (Single PO → Single GRPO)

```
1. Vehicle Entry created
2. User selects 1 PO → POReceipt created (linked to vehicle entry)
3. QC inspection happens on PO items
4. User previews GRPO for that 1 PO
5. User posts GRPO → 1 SAP GRPO document created
```

**Current Constraints:**
- `GRPOPostRequestSerializer` accepts a single `po_receipt_id`
- `GRPOPosting` model has unique constraint: `("vehicle_entry", "po_receipt")`
- `GRPOPosting.po_receipt` is a single ForeignKey (not ManyToMany)

---

## Proposed Flow (Multiple POs → Single Merged GRPO)

```
1. Vehicle Entry created
2. User receives multiple POs for the same supplier → multiple POReceipts created
3. QC inspection happens on all PO items
4. User selects multiple POs (same party) for merged GRPO
5. System validates: all selected POs belong to the same supplier_code
6. User previews merged GRPO (items from all selected POs combined)
7. User posts merged GRPO → 1 SAP GRPO document with lines referencing different PO BaseEntries
```

---

## What Changes Are Needed

### 1. Model Changes (`grpo/models.py`)

#### Option A: New ManyToMany Relationship (Recommended)

- Add `po_receipts` ManyToManyField on `GRPOPosting` to link multiple POs
- Keep existing `po_receipt` ForeignKey for backward compatibility (nullable) OR migrate to M2M only
- Remove or update the unique constraint `("vehicle_entry", "po_receipt")`

```
GRPOPosting
  ├── vehicle_entry (FK) — stays as is
  ├── po_receipt (FK) — make nullable, keep for single-PO backward compat
  ├── po_receipts (M2M → POReceipt) — NEW: all POs included in this GRPO
  └── ... rest stays same
```

#### Option B: Through Table

- Create `GRPOMergedPO` model linking GRPOPosting to multiple POReceipts
- This gives more control (e.g., tracking per-PO status within a merged GRPO)

### 2. Serializer Changes

#### `GRPOPostRequestSerializer` (grpo/serializers.py)

**Current:**
```python
po_receipt_id = serializers.IntegerField(required=True)
```

**Proposed:**
```python
po_receipt_ids = serializers.ListField(
    child=serializers.IntegerField(),
    required=True,
    min_length=1
)
```

- Accept a list of `po_receipt_ids` instead of a single ID
- Items list will include items from all selected POs
- Each item still references its `po_item_receipt_id` (which links back to its parent PO)

#### `GRPOItemInputSerializer`

- No change needed — each item already references `po_item_receipt_id` which carries its own PO context (sap_doc_entry, sap_line_num)

### 3. Validation Changes (grpo/services.py)

Add validation in `GRPOService.post_grpo()`:

- **Same Supplier Check:** All selected PO receipts must have the same `supplier_code`
- **Same Vehicle Entry Check:** All selected PO receipts must belong to the same `vehicle_entry`
- **Status Check:** All selected POs must have completed QC
- **Duplicate Check:** None of the selected POs should already have a POSTED GRPO
- **Branch ID Check:** All POs should have the same `branch_id` (SAP requirement — single business place per GRPO)

### 4. SAP Payload Changes (grpo/services.py → `_build_grpo_payload`)

Currently builds lines from a single PO. Change to:

- Iterate over **all selected PO receipts**
- Each GRPO line gets its own `BaseEntry` (from its parent POReceipt.sap_doc_entry) and `BaseLine` (from POItemReceipt.sap_line_num)
- SAP supports this — a single GRPO can reference lines from multiple POs as long as:
  - Same CardCode (supplier)
  - Same BPL_IDAssignedToInvoice (branch/business place)

**Payload structure (merged):**
```json
{
  "CardCode": "V001",
  "BPL_IDAssignedToInvoice": 1,
  "DocumentLines": [
    {
      "ItemCode": "ITEM-A",
      "Quantity": 100,
      "BaseEntry": 1001,
      "BaseLine": 0,
      "BaseType": 22
    },
    {
      "ItemCode": "ITEM-B",
      "Quantity": 50,
      "BaseEntry": 1002,
      "BaseLine": 0,
      "BaseType": 22
    }
  ]
}
```
> Here `BaseEntry: 1001` and `BaseEntry: 1002` are from two different POs.

### 5. Preview API Changes (grpo/views.py → `GRPOPreviewAPI`)

**Current:** `GET /api/grpo/preview/<vehicle_entry_id>/` — previews all POs for a vehicle entry individually.

**Proposed:** Add support for merged preview:
- `GET /api/grpo/preview/<vehicle_entry_id>/?po_receipt_ids=1,2,3`
- Returns a combined preview with items from all selected POs grouped together
- Shows total quantities and amounts across all POs

### 6. Pending GRPO List Changes (grpo/views.py → `PendingGRPOListAPI`)

- Group pending PO receipts by `supplier_code` in the response
- Frontend can then show: "Zomato — 5 POs pending" with checkboxes to select which to merge

### 7. API Endpoint Changes

| Endpoint | Change |
|----------|--------|
| `POST /api/grpo/post/` | Accept `po_receipt_ids` (list) instead of `po_receipt_id` |
| `GET /api/grpo/preview/<id>/` | Add `?po_receipt_ids=` query param for merged preview |
| `GET /api/grpo/pending/` | Group by supplier in response |
| `GET /api/grpo/<posting_id>/` | Show all linked POs in detail |

### 8. GRPOLinePosting Model

- No change needed — each line already stores `base_entry` and `base_line` independently
- Lines from different POs will naturally have different `base_entry` values

---

## Migration Plan

1. **Database Migration:**
   - Add M2M field `po_receipts` to `GRPOPosting`
   - Make `po_receipt` FK nullable
   - Remove unique constraint `("vehicle_entry", "po_receipt")`
   - Data migration: copy existing `po_receipt` FK values into the new M2M relationship

2. **Backend Changes:**
   - Update serializers to accept list of PO receipt IDs
   - Update validation logic in GRPOService
   - Update SAP payload builder to handle multiple POs
   - Update preview API

3. **Frontend Changes:**
   - Add multi-select UI for POs (grouped by party)
   - Update GRPO preview to show merged view
   - Update GRPO posting to send list of PO IDs

---

## Edge Cases to Handle

| Scenario | How to Handle |
|----------|---------------|
| User selects POs from different suppliers | Reject with validation error |
| User selects POs with different branch_id | Reject — SAP requires single business place per GRPO |
| One PO fails QC, others pass | Only allow QC-completed POs to be selected |
| Partial merge (2 of 5 POs) | Allowed — remaining POs stay pending for future GRPO |
| Mixed: some POs already posted | Exclude already-posted POs from selection |
| Single PO selected in new flow | Works as before — list with 1 item |
| Extra charges in merged GRPO | Apply at document level (shared across all PO lines) |
| Attachments | Shared across the merged GRPO document |

---

## Backward Compatibility

- Single PO posting still works (list with 1 element)
- Existing GRPO records remain valid via data migration
- API can support both `po_receipt_id` (deprecated) and `po_receipt_ids` during transition
