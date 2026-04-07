"""
Test SAP Production Receipt Integration — End-to-end validation
Run: python test_sap_integration.py
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Setup Django
sys.path.insert(0, os.path.dirname(__file__))
django.setup()

from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError
from production_execution.services.sap_writer import GoodsReceiptWriter, SAPWriteError

COMPANY_CODE = "JIVO_OIL"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

results = []


def log_result(test_name, status, detail=""):
    results.append((test_name, status, detail))
    symbol = {"PASS": "[+]", "FAIL": "[-]", "SKIP": "[~]"}[status]
    print(f"  {symbol} {test_name}: {status}" + (f" — {detail}" if detail else ""))


def test_hana_connection():
    """Test 1: Verify HANA connection works"""
    print("\n=== Test 1: HANA Connection ===")
    try:
        reader = ProductionOrderReader(COMPANY_CODE)
        log_result("Initialize ProductionOrderReader", PASS)
        return reader
    except SAPReadError as e:
        log_result("Initialize ProductionOrderReader", FAIL, str(e))
        return None


def test_fetch_released_orders(reader):
    """Test 2: Fetch released production orders from SAP"""
    print("\n=== Test 2: Fetch Released Production Orders ===")
    if not reader:
        log_result("Fetch released orders", SKIP, "Reader not initialized")
        return []

    try:
        orders = reader.get_released_production_orders()
        log_result("Fetch released orders", PASS, f"Found {len(orders)} orders")
        if orders:
            sample = orders[0]
            print(f"      Sample order: DocEntry={sample.get('DocEntry')}, "
                  f"DocNum={sample.get('DocNum')}, "
                  f"ItemCode={sample.get('ItemCode')}, "
                  f"ProdName={sample.get('ProdName')}, "
                  f"PlannedQty={sample.get('PlannedQty')}, "
                  f"Warehouse={sample.get('Warehouse')}")
        return orders
    except SAPReadError as e:
        log_result("Fetch released orders", FAIL, str(e))
        return []


def test_order_detail(reader, doc_entry):
    """Test 3: Fetch production order detail with components"""
    print(f"\n=== Test 3: Fetch Order Detail (DocEntry={doc_entry}) ===")
    if not reader:
        log_result("Fetch order detail", SKIP, "Reader not initialized")
        return None

    try:
        detail = reader.get_production_order_detail(doc_entry)
        header = detail.get('header', {})
        components = detail.get('components', [])
        log_result("Fetch order detail", PASS,
                   f"ItemCode={header.get('ItemCode')}, "
                   f"Warehouse={header.get('Warehouse')}, "
                   f"Components={len(components)}")

        item_code = header.get('ItemCode')
        warehouse = header.get('Warehouse')

        if item_code and warehouse:
            log_result("ItemCode present", PASS, item_code)
            log_result("Warehouse present", PASS, warehouse)
        else:
            if not item_code:
                log_result("ItemCode present", FAIL, "Missing!")
            if not warehouse:
                log_result("Warehouse present", FAIL, "Missing!")

        return detail
    except SAPReadError as e:
        log_result("Fetch order detail", FAIL, str(e))
        return None


def test_service_layer_login():
    """Test 4: Verify SAP Service Layer authentication"""
    print("\n=== Test 4: SAP Service Layer Authentication ===")
    try:
        writer = GoodsReceiptWriter(COMPANY_CODE)
        log_result("Initialize GoodsReceiptWriter", PASS)

        # Test login manually
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        sl_config = writer.client.context.service_layer
        base_url = sl_config['base_url']

        session = requests.Session()
        login_resp = session.post(
            f"{base_url}/b1s/v2/Login",
            json={
                "CompanyDB": sl_config['company_db'],
                "UserName": sl_config['username'],
                "Password": sl_config['password'],
            },
            timeout=10,
            verify=False,
        )

        if login_resp.ok:
            log_result("Service Layer Login", PASS,
                       f"URL={base_url}, CompanyDB={sl_config['company_db']}")
        else:
            log_result("Service Layer Login", FAIL, login_resp.text[:200])

        # Logout to clean up session
        session.post(f"{base_url}/b1s/v2/Logout", verify=False, timeout=5)
        return writer
    except Exception as e:
        log_result("Service Layer Login", FAIL, str(e))
        return None


def test_goods_receipt_payload(detail):
    """Test 5: Validate the goods receipt payload structure"""
    print("\n=== Test 5: Validate Goods Receipt Payload ===")
    if not detail:
        log_result("Validate payload", SKIP, "No order detail available")
        return

    header = detail.get('header', {})
    item_code = header.get('ItemCode')
    warehouse = header.get('Warehouse')
    doc_entry = header.get('DocEntry')

    payload = {
        "DocDate": "2026-03-31",
        "Comments": f"Production Execution — DocEntry {doc_entry}",
        "DocumentLines": [{
            "ItemCode": item_code,
            "Quantity": 1.0,
            "WarehouseCode": warehouse,
            "BaseType": 202,
            "BaseEntry": doc_entry,
            "BaseLine": 0,
        }],
    }

    # Validate all fields are present and non-null
    errors = []
    line = payload["DocumentLines"][0]
    for field in ["ItemCode", "Quantity", "WarehouseCode", "BaseType", "BaseEntry"]:
        if line.get(field) is None:
            errors.append(f"{field} is None")

    if errors:
        log_result("Validate payload", FAIL, "; ".join(errors))
    else:
        log_result("Validate payload", PASS)
        print(f"      Payload preview: {payload}")


def test_complete_run_integration():
    """Test 6: Test the complete_run flow with a real (dry) run"""
    print("\n=== Test 6: Test complete_run SAP Integration (model fields) ===")
    from production_execution.models import ProductionRun

    # Check new fields exist on the model
    field_names = [f.name for f in ProductionRun._meta.get_fields()]
    new_fields = ['sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error']

    for field in new_fields:
        if field in field_names:
            log_result(f"Model field '{field}' exists", PASS)
        else:
            log_result(f"Model field '{field}' exists", FAIL, "Field not found on model")

    # Check default values
    run = ProductionRun()
    log_result("Default sap_sync_status", PASS if run.sap_sync_status == 'NOT_APPLICABLE' else FAIL,
               f"Got: {run.sap_sync_status}")
    log_result("Default sap_receipt_doc_entry", PASS if run.sap_receipt_doc_entry is None else FAIL,
               f"Got: {run.sap_receipt_doc_entry}")
    log_result("Default sap_sync_error", PASS if run.sap_sync_error == '' else FAIL,
               f"Got: '{run.sap_sync_error}'")


def test_serializer_fields():
    """Test 7: Verify serializers include new fields"""
    print("\n=== Test 7: Verify Serializer Fields ===")
    from production_execution.serializers import (
        ProductionRunListSerializer, ProductionRunDetailSerializer
    )

    list_fields = ProductionRunListSerializer.Meta.fields
    detail_fields = ProductionRunDetailSerializer.Meta.fields

    new_fields = ['sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error']
    for field in new_fields:
        if field in list_fields:
            log_result(f"ListSerializer has '{field}'", PASS)
        else:
            log_result(f"ListSerializer has '{field}'", FAIL)

        if field in detail_fields:
            log_result(f"DetailSerializer has '{field}'", PASS)
        else:
            log_result(f"DetailSerializer has '{field}'", FAIL)


def test_retry_endpoint_exists():
    """Test 8: Verify retry URL is registered"""
    print("\n=== Test 8: Verify URL Configuration ===")
    from django.urls import reverse, NoReverseMatch

    try:
        url = reverse('pe-run-retry-sap', kwargs={'run_id': 1})
        log_result("Retry SAP URL registered", PASS, url)
    except NoReverseMatch as e:
        log_result("Retry SAP URL registered", FAIL, str(e))

    try:
        url = reverse('pe-run-complete', kwargs={'run_id': 1})
        log_result("Complete run URL registered", PASS, url)
    except NoReverseMatch as e:
        log_result("Complete run URL registered", FAIL, str(e))


def test_service_methods():
    """Test 9: Verify service methods exist"""
    print("\n=== Test 9: Verify Service Methods ===")
    from production_execution.services import ProductionExecutionService

    service = ProductionExecutionService.__new__(ProductionExecutionService)

    if hasattr(service, 'complete_run'):
        log_result("complete_run method exists", PASS)
    else:
        log_result("complete_run method exists", FAIL)

    if hasattr(service, '_post_goods_receipt_to_sap'):
        log_result("_post_goods_receipt_to_sap method exists", PASS)
    else:
        log_result("_post_goods_receipt_to_sap method exists", FAIL)

    if hasattr(service, 'retry_sap_goods_receipt'):
        log_result("retry_sap_goods_receipt method exists", PASS)
    else:
        log_result("retry_sap_goods_receipt method exists", FAIL)


# ===== RUN ALL TESTS =====
if __name__ == '__main__':
    print("=" * 60)
    print("SAP Production Receipt Integration — Test Suite")
    print(f"Company: {COMPANY_CODE}")
    print("=" * 60)

    # Django/code validation tests
    test_complete_run_integration()
    test_serializer_fields()
    test_retry_endpoint_exists()
    test_service_methods()

    # SAP connectivity tests
    reader = test_hana_connection()
    orders = test_fetch_released_orders(reader)

    detail = None
    if orders:
        doc_entry = orders[0]['DocEntry']
        detail = test_order_detail(reader, doc_entry)

    test_service_layer_login()
    test_goods_receipt_payload(detail)

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    total = len(results)

    print(f"  Total:   {total}")
    print(f"  Passed:  {passed}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {skipped}")
    print("=" * 60)

    if failed > 0:
        print("\nFailed tests:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  [-] {name}: {detail}")

    sys.exit(1 if failed > 0 else 0)
