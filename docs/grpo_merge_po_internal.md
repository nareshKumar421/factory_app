# GRPO Merge PO — Internal Technical Documentation

## Feature Summary

Allows merging multiple Purchase Orders (POs) from the **same supplier** into a **single GRPO document** in SAP. This reduces document clutter in SAP and matches real-world workflows where a single truck delivers goods against multiple POs.

---

## Architecture Changes

### Model: `GRPOPosting` (grpo/models.py)

**Changes:**
1. **Added `po_receipts` M2M field** — Links a GRPO to multiple `POReceipt` records
   - Related name: `merged_grpo_postings`
   - Blank allowed (populated after creation)

2. **Made `po_receipt` FK nullable** — Legacy field kept for backward compatibility
   - Set to first PO in the list during creation
   - New code should read from `po_receipts` M2M

3. **Removed `unique_together` constraint** — Previously `("vehicle_entry", "po_receipt")`.
   Removed because a merged GRPO links to multiple POs via M2M instead.

**Data Migration:** `0011_populate_po_receipts_m2m` copies existing `po_receipt` FK values into the M2M table for all historical records.

### Service: `GRPOService.post_grpo()` (grpo/services.py)

**Signature change:**
```python
# Old
def post_grpo(self, vehicle_entry_id, po_receipt_id, ...)

# New
def post_grpo(self, vehicle_entry_id, po_receipt_ids: List[int], ...)
```

**Validation added:**
- All PO receipts must have the same `supplier_code`
- All PO receipts must have the same `branch_id` (SAP requires single business place per GRPO)
- No selected PO can already have a POSTED GRPO (checks both M2M and legacy FK)
- All `po_item_receipt_id` values in `items` must belong to one of the selected POs
- Gate entry must be COMPLETED or QC_COMPLETED

**SAP Payload:**
- `CardCode` taken from any PO (all same supplier)
- `DocumentLines` include items from ALL selected POs
- Each line's `BaseEntry`/`BaseLine` references its own PO's SAP doc entry
- Comments include "Merged: N POs" when multiple POs are merged

### Service: `_build_structured_comments()` (grpo/services.py)

**Signature change:**
```python
# Old
def _build_structured_comments(self, user, po_receipt: POReceipt, ...)

# New
def _build_structured_comments(self, user, po_receipts: List[POReceipt], ...)
```

Now accepts a list of PO receipts. Comments include all PO numbers and a "Merged" indicator.

### Service: `get_grpo_preview_data()` (grpo/services.py)

**New parameter:** `po_receipt_ids: Optional[List[int]]`

When provided, filters the preview to only the specified PO receipts. Used by the frontend to show a merged preview before posting.

### Serializer: `GRPOPostRequestSerializer` (grpo/serializers.py)

**New fields:**
- `po_receipt_ids` — `ListField(child=IntegerField)`, preferred
- `po_receipt_id` — `IntegerField`, deprecated but still works

**Validation:** At least one of `po_receipt_ids` or `po_receipt_id` must be provided. If only `po_receipt_id` is given, it's auto-normalized to `po_receipt_ids: [<id>]`.

### Serializer: `GRPOPostingSerializer` (grpo/serializers.py)

**New response fields:**
- `po_numbers` — List of PO numbers (e.g., `["PO-001", "PO-002"]`)
- `is_merged` — Boolean, true when GRPO has >1 PO
- `merged_po_receipts` — Array of `{id, po_number, supplier_code, supplier_name}`
- `po_number` — Now returns comma-separated string for merged GRPOs

### View: `PendingGRPOListAPI` (grpo/views.py)

**New response structure:** Each entry now includes a `suppliers` array that groups pending POs by supplier. Each supplier group has:
- `supplier_code`, `supplier_name`
- `po_count` — Number of pending POs for this supplier
- `can_merge` — True if >1 PO (mergeable)
- `po_receipts` — List of PO details with `po_receipt_id`, `po_number`, `branch_id`, `item_count`

### View: `GRPOPreviewAPI` (grpo/views.py)

**New query parameter:** `?po_receipt_ids=1,2,3` (comma-separated integers)

When provided, only returns preview data for the specified POs.

### View: `PostGRPOAPI` (grpo/views.py)

Updated to pass `po_receipt_ids` (list) to service instead of single `po_receipt_id`.

### Admin: `GRPOPostingAdmin` (grpo/admin.py)

- Added `filter_horizontal` for M2M `po_receipts` field
- Added `get_po_numbers` display (comma-separated)
- Added `get_is_merged` boolean display
- Added `get_merged_po_list` readonly field showing all linked POs
- Search includes `po_receipts__po_number`

---

## Database Migrations

| Migration | Description |
|-----------|-------------|
| `0010_alter_grpoposting_unique_together_and_more` | Remove unique_together, add M2M field, make FK nullable |
| `0011_populate_po_receipts_m2m` | Data migration: copy FK → M2M for existing records |

---

## SAP Integration Details

### How SAP Handles Merged GRPOs

A single SAP GRPO document (`PurchaseDeliveryNotes`) can reference lines from **multiple Purchase Orders** by setting different `BaseEntry` values per line:

```json
{
  "CardCode": "ZOMATO01",
  "BPL_IDAssignedToInvoice": 1,
  "DocumentLines": [
    {
      "ItemCode": "ITEM-A",
      "Quantity": 180,
      "BaseEntry": 5001,   // ← PO #1
      "BaseLine": 0,
      "BaseType": 22
    },
    {
      "ItemCode": "ITEM-B",
      "Quantity": 90,
      "BaseEntry": 5002,   // ← PO #2
      "BaseLine": 0,
      "BaseType": 22
    }
  ]
}
```

### SAP Requirements for Merging
- **Same CardCode** — All lines must reference POs from the same vendor
- **Same BPL_IDAssignedToInvoice** — Single business place per document
- **BaseType: 22** — Always 22 for Purchase Order
- Each line independently references its own PO via `BaseEntry` + `BaseLine`

---

## Test Coverage

### Test File: `grpo/tests.py`

**New test class: `MergedGRPOServiceTests`** (8 tests)
- `test_merged_grpo_success` — Two POs merged, SAP payload verified
- `test_merged_grpo_different_suppliers_rejected` — Validation error
- `test_merged_grpo_different_branch_rejected` — Validation error
- `test_merged_grpo_already_posted_po_rejected` — Cannot re-merge posted PO
- `test_merged_grpo_invalid_po_ids` — Non-existent IDs rejected
- `test_single_po_in_list_works` — Backward compat: single PO in list
- `test_preview_with_po_receipt_ids_filter` — Filtered preview
- `test_preview_all_pos` — Unfiltered preview returns all

**New test class: `MergedGRPOSerializerTests`** (4 tests)
- `test_serializer_accepts_po_receipt_ids_list` — New field works
- `test_serializer_legacy_po_receipt_id_converted_to_list` — Auto-conversion
- `test_serializer_rejects_missing_both_ids` — Validation error
- `test_posting_serializer_shows_merged_info` — Response includes merged fields

**Updated existing tests:**
- All `po_receipt_id=` calls updated to `po_receipt_ids=[...]`
- `_build_structured_comments` calls updated to pass list
- `test_grpo_unique_constraint` replaced with `test_grpo_m2m_po_receipts`
- Serializer tests updated for new validation logic

---

## Backward Compatibility

| Component | Backward Compatible? | Notes |
|-----------|---------------------|-------|
| API: `po_receipt_id` (single) | Yes | Auto-converted to list |
| API: Response format | Yes | New fields added, old fields preserved |
| Model: `po_receipt` FK | Yes | Kept as nullable, still populated |
| Admin | Yes | Shows both legacy and M2M data |
| Existing GRPO records | Yes | Data migration populates M2M |

---

## Files Modified

| File | Changes |
|------|---------|
| `grpo/models.py` | Added M2M, made FK nullable, removed unique_together |
| `grpo/services.py` | Multi-PO post_grpo, preview filter, list comments |
| `grpo/serializers.py` | po_receipt_ids list, merged response fields |
| `grpo/views.py` | Supplier grouping, preview filter, merged post |
| `grpo/admin.py` | M2M display, merged indicators |
| `grpo/tests.py` | 12 new tests, existing tests updated |
| `grpo/migrations/0010_*` | Schema migration |
| `grpo/migrations/0011_*` | Data migration |
