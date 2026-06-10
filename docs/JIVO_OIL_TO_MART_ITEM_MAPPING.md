# Jivo Oil to Jivo Mart Item Mapping

## Branch

Created branch:

```text
fix/jivo-oil-mart-item-mapping
```

## What Changed

- Added Jivo Oil to Jivo Mart item-code mapping for Barcode Intercompany Transfer.
- Applied the mapping only when:
  - Source Company is `JIVO_OIL`
  - Destination Company is `JIVO_MART`
- Added a Jivo Mart OITM lookup by Oil item code:

```sql
SELECT "ItemCode"
FROM "OITM"
WHERE "U_Oil_ItemCode" = :OilItemCode;
```

- Preserved the original Oil ItemCode on the transfer line for source-side traceability and reversal.
- Updated destination-owned boxes and pallets to use the mapped Jivo Mart ItemCode after transfer.
- Restored the original Oil ItemCode when an Oil to Mart transfer is reversed.
- Kept barcode scan validation unchanged; the mapping is resolved during transfer confirmation before destination ownership is applied.
- Kept all other company transfer combinations on the existing logic.

## Error Handling

- If no Jivo Mart item is mapped:

```text
Item mapping not found in Jivo Mart for Oil ItemCode: <OilItemCode>. Please maintain U_Oil_ItemCode in Jivo Mart OITM table.
```

- If multiple Jivo Mart items are mapped:

```text
Duplicate item mapping found in Jivo Mart for Oil ItemCode: <OilItemCode>. Please correct duplicate U_Oil_ItemCode values in Jivo Mart OITM table.
```

## Verification

Latest focused Intercompany/OITM tests:

```powershell
$env:DEBUG='False'; .\.venv\Scripts\python.exe manage.py test barcode.tests.BarcodeWorkflowTests.test_oitm_item_service_looks_up_jivo_mart_item_by_oil_item_code barcode.tests.BarcodeWorkflowTests.test_intercompany_oil_to_mart_maps_destination_item_code barcode.tests.BarcodeWorkflowTests.test_intercompany_oil_to_mart_rejects_missing_item_mapping barcode.tests.BarcodeWorkflowTests.test_intercompany_oil_to_mart_rejects_duplicate_item_mapping barcode.tests.BarcodeWorkflowTests.test_intercompany_oil_to_mart_rejects_blank_or_null_mapping barcode.tests.BarcodeWorkflowTests.test_intercompany_item_mapping_is_skipped_for_other_company_pairs barcode.tests.BarcodeWorkflowTests.test_intercompany_oil_to_mart_scan_does_not_resolve_destination_mapping barcode.tests.BarcodeWorkflowTests.test_intercompany_scan_accepts_legacy_qr_payload_and_one_dimensional_value barcode.tests.BarcodeWorkflowTests.test_intercompany_transfer_api_end_to_end --keepdb
```

Result:

```text
Ran 9 tests in 11.466s
OK
```

Broader Barcode workflow tests:

```powershell
$env:DEBUG='False'; .\.venv\Scripts\python.exe manage.py test barcode.tests.BarcodeWorkflowTests --keepdb
```

Result:

```text
Ran 35 tests in 43.795s
OK
```

Runtime smoke checks:

```text
Frontend http://127.0.0.1:5173/ returned 200 OK
Backend http://127.0.0.1:8000/admin/ returned 200 OK
Django system check identified no issues
```

## Test Scope Result

| Scope | Result |
| --- | --- |
| Successful Jivo Oil to Jivo Mart transfer | Passed. Transfer line keeps Oil ItemCode; destination box uses mapped Mart ItemCode. |
| Missing mapping | Passed. Transfer stops with the required missing mapping error. |
| Duplicate mapping | Passed. Transfer stops with the required duplicate mapping error. |
| Blank or null mapping | Passed. Blank/null behaves as no valid mapping and transfer does not proceed. |
| Other company combinations | Passed. OITM mapping service is not called and existing transfer behavior remains unchanged. |
| Barcode scanning flow | Passed. Scan returns the Oil-side barcode/item data and does not resolve destination mapping during scan. |
| Quantity, warehouse, batch, and stock fields | Passed for app-level transfer records. Quantity, UOM, batch, and warehouse remain unchanged while only the destination ItemCode changes. |
| Transaction failure handling | Passed. Missing/duplicate/blank mapping failures do not create transfer records and do not move the source box. |

## Full Barcode Suite Note

The full `barcode` app test suite currently has unrelated failures in `BarcodeDispatchWorkflowTests` around dispatch SAP-sync expectations, and one run also hit a remote PostgreSQL connection timeout. These failures are outside the Intercompany Transfer item-code mapping change and occur in code paths not modified by this branch.
