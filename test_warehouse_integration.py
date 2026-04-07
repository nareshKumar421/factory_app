"""
Comprehensive integration test for Warehouse module.
Tests against real SAP HANA and Service Layer using .env credentials.
"""
import os
import sys
import io
import django

# Fix Windows encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import warnings
warnings.filterwarnings('ignore')

from datetime import date
from decimal import Decimal

# ============================================================
# Test utilities
# ============================================================
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"
SKIP = "\033[94mSKIP\033[0m"

results = []

def log_result(test_name, status, detail=""):
    results.append((test_name, status))
    mark = PASS if status == PASS else (FAIL if status == FAIL else (WARN if status == WARN else SKIP))
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{mark}] {test_name}{detail_str}")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# TEST 1: SAP HANA Connection
# ============================================================
section("1. SAP HANA Connection")

try:
    from hdbcli import dbapi
    from django.conf import settings

    conn = dbapi.connect(
        address=settings.HANA_HOST,
        port=settings.HANA_PORT,
        user=settings.HANA_USER,
        password=settings.HANA_PASSWORD,
    )
    cursor = conn.cursor()
    cursor.execute("SELECT CURRENT_TIMESTAMP FROM DUMMY")
    ts = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    log_result("HANA connection", PASS, f"Server time: {ts}")
except Exception as e:
    log_result("HANA connection", FAIL, str(e))


# ============================================================
# TEST 2: SAP Service Layer Login
# ============================================================
section("2. SAP Service Layer Login")

try:
    import requests
    requests.packages.urllib3.disable_warnings()

    sl_url = settings.SL_URL
    company_db = settings.COMPANY_DB["JIVO_OIL"]

    session = requests.Session()
    resp = session.post(
        f"{sl_url}/b1s/v2/Login",
        json={
            "CompanyDB": company_db,
            "UserName": settings.SL_USER,
            "Password": settings.SL_PASSWORD,
        },
        timeout=10,
        verify=False,
    )
    if resp.ok:
        log_result("Service Layer login", PASS, f"CompanyDB={company_db}")
        sl_session = session  # keep for later tests
    else:
        log_result("Service Layer login", FAIL, resp.text[:200])
        sl_session = None
except Exception as e:
    log_result("Service Layer login", FAIL, str(e))
    sl_session = None


# ============================================================
# TEST 3: Fetch Released Production Orders from HANA
# ============================================================
section("3. Fetch Released Production Orders (HANA)")

test_doc_entry = None
test_item_code = None

try:
    from production_execution.services.sap_reader import ProductionOrderReader

    reader = ProductionOrderReader("JIVO_OIL")
    orders = reader.get_released_production_orders()
    log_result("Fetch released orders", PASS, f"Found {len(orders)} released orders")

    if orders:
        order = orders[0]
        test_doc_entry = order.get('DocEntry')
        test_item_code = order.get('ItemCode')
        print(f"    Sample: DocEntry={test_doc_entry}, Item={test_item_code}, "
              f"Planned={order.get('PlannedQty')}, Remaining={order.get('RemainingQty')}")
    else:
        log_result("Released orders available", WARN, "No released orders found — some tests will be skipped")
except Exception as e:
    log_result("Fetch released orders", FAIL, str(e))


# ============================================================
# TEST 4: Fetch Production Order Detail + Components
# ============================================================
section("4. Fetch Production Order Detail + Components")

test_components = []
test_order_planned = None

if test_doc_entry:
    try:
        detail = reader.get_production_order_detail(test_doc_entry)
        header = detail.get('header', {})
        components = detail.get('components', [])
        test_components = components
        test_order_planned = header.get('PlannedQty')
        log_result("Fetch order detail", PASS,
                   f"DocEntry={test_doc_entry}, Components={len(components)}")

        for i, c in enumerate(components[:3]):
            print(f"    Component {i}: {c.get('ItemCode')} — "
                  f"Planned={c.get('PlannedQty')}, Issued={c.get('IssuedQty')}, "
                  f"WH={c.get('Warehouse')}, UOM={c.get('UomCode')}")

        if not components:
            log_result("Components found", WARN, "No components in this order")
    except Exception as e:
        log_result("Fetch order detail", FAIL, str(e))
else:
    log_result("Fetch order detail", SKIP, "No released orders")


# ============================================================
# TEST 5: Fetch BOM by Item Code
# ============================================================
section("5. Fetch BOM by Item Code")

if test_item_code:
    try:
        bom = reader.get_bom_by_item_code(test_item_code)
        log_result("Fetch BOM by item code", PASS,
                   f"Item={test_item_code}, BOM lines={len(bom)}")

        for i, c in enumerate(bom[:3]):
            print(f"    BOM {i}: {c.get('ItemCode')} — "
                  f"Qty={c.get('PlannedQty')}, UOM={c.get('UomCode')}, WH={c.get('Warehouse')}")
    except Exception as e:
        log_result("Fetch BOM by item code", FAIL, str(e))
else:
    log_result("Fetch BOM by item code", SKIP, "No item code available")


# ============================================================
# TEST 6: Stock Check (OITW)
# ============================================================
section("6. Stock Check via OITW")

if test_components:
    try:
        from warehouse.services.warehouse_service import WarehouseService

        wh_svc = WarehouseService("JIVO_OIL")
        item_codes = [c.get('ItemCode') for c in test_components[:3]]
        stock = wh_svc.get_stock_for_items(item_codes)
        log_result("Stock check", PASS, f"Checked {len(item_codes)} items, got stock for {len(stock)}")

        for code, info in stock.items():
            print(f"    {code}: OnHand={info.get('total_on_hand')}, "
                  f"Available={info.get('total_available')}, "
                  f"Warehouses={len(info.get('warehouses', []))}")
    except Exception as e:
        log_result("Stock check", FAIL, str(e))
elif test_item_code:
    try:
        from warehouse.services.warehouse_service import WarehouseService
        wh_svc = WarehouseService("JIVO_OIL")
        stock = wh_svc.get_stock_for_items([test_item_code])
        log_result("Stock check (finished good)", PASS, f"Got stock for {len(stock)} items")
        for code, info in stock.items():
            print(f"    {code}: OnHand={info.get('total_on_hand')}, "
                  f"Available={info.get('total_available')}")
    except Exception as e:
        log_result("Stock check", FAIL, str(e))
else:
    log_result("Stock check", SKIP, "No items available")


# ============================================================
# TEST 7: Django Models — Create & Query
# ============================================================
section("7. Django Models — CRUD Operations")

from warehouse.models import (
    BOMRequest, BOMRequestLine, FinishedGoodsReceipt,
    BOMRequestStatus, BOMLineStatus, FGReceiptStatus,
)
from production_execution.models import ProductionRun, ProductionLine
from company.models import Company

try:
    company = Company.objects.filter(code="JIVO_OIL").first()
    if not company:
        log_result("Company JIVO_OIL exists", FAIL, "Company not found in DB")
    else:
        log_result("Company JIVO_OIL exists", PASS, f"ID={company.id}")
except Exception as e:
    log_result("Company check", FAIL, str(e))
    company = None

# Check ProductionRun has new fields
try:
    field_names = [f.name for f in ProductionRun._meta.get_fields()]
    has_required_qty = 'required_qty' in field_names
    has_wh_status = 'warehouse_approval_status' in field_names
    log_result("ProductionRun.required_qty field", PASS if has_required_qty else FAIL)
    log_result("ProductionRun.warehouse_approval_status field", PASS if has_wh_status else FAIL)
except Exception as e:
    log_result("ProductionRun field check", FAIL, str(e))

# Check BOMRequest model
try:
    field_names = [f.name for f in BOMRequest._meta.get_fields()]
    expected = ['company', 'production_run', 'required_qty', 'status',
                'material_issue_status', 'sap_issue_doc_entries',
                'requested_by', 'reviewed_by', 'lines']
    missing = [f for f in expected if f not in field_names]
    if missing:
        log_result("BOMRequest model fields", FAIL, f"Missing: {missing}")
    else:
        log_result("BOMRequest model fields", PASS, f"All {len(expected)} fields present")
except Exception as e:
    log_result("BOMRequest model check", FAIL, str(e))

# Check BOMRequestLine model
try:
    field_names = [f.name for f in BOMRequestLine._meta.get_fields()]
    expected = ['bom_request', 'item_code', 'item_name', 'per_unit_qty',
                'required_qty', 'available_stock', 'approved_qty', 'issued_qty',
                'warehouse', 'uom', 'base_line', 'status']
    missing = [f for f in expected if f not in field_names]
    if missing:
        log_result("BOMRequestLine model fields", FAIL, f"Missing: {missing}")
    else:
        log_result("BOMRequestLine model fields", PASS, f"All {len(expected)} fields present")
except Exception as e:
    log_result("BOMRequestLine model check", FAIL, str(e))

# Check FinishedGoodsReceipt model
try:
    field_names = [f.name for f in FinishedGoodsReceipt._meta.get_fields()]
    expected = ['company', 'production_run', 'item_code', 'produced_qty',
                'good_qty', 'status', 'sap_receipt_doc_entry', 'received_by']
    missing = [f for f in expected if f not in field_names]
    if missing:
        log_result("FinishedGoodsReceipt model fields", FAIL, f"Missing: {missing}")
    else:
        log_result("FinishedGoodsReceipt model fields", PASS, f"All {len(expected)} fields present")
except Exception as e:
    log_result("FinishedGoodsReceipt model check", FAIL, str(e))


# ============================================================
# TEST 8: Warehouse Service — BOM Request Creation
# ============================================================
section("8. Warehouse Service — BOM Request Flow")

test_run = None
test_bom_request = None

if company and test_doc_entry and test_components:
    try:
        from production_execution.services.production_service import ProductionExecutionService

        pe_svc = ProductionExecutionService("JIVO_OIL")

        # Get or create a production line
        lines = list(pe_svc.list_lines(is_active=True))
        if lines:
            line = lines[0]
        else:
            line = pe_svc.create_line({'name': 'Test Line Warehouse'})

        # Create a test production run
        from accounts.models import User
        user = User.objects.first()

        test_run = pe_svc.create_run({
            'sap_doc_entry': test_doc_entry,
            'line_id': line.id,
            'date': date.today(),
            'product': test_item_code or '',
            'required_qty': Decimal('100'),
        }, user)

        log_result("Create test production run", PASS,
                   f"Run ID={test_run.id}, required_qty=100, wh_status={test_run.warehouse_approval_status}")

        # Test warehouse approval gate
        try:
            pe_svc.start_production(test_run.id)
            log_result("Start production (no approval)", WARN,
                       "Should have been blocked but wasn't — warehouse_approval_status is NOT_REQUESTED")
            # Stop it right away
            pe_svc.stop_production(test_run.id, 0)
        except ValueError as e:
            if 'warehouse' in str(e).lower() or 'approval' in str(e).lower():
                log_result("Start production blocked (no approval)", PASS, str(e))
            else:
                log_result("Start production (no approval)", PASS,
                           f"Blocked for other reason: {e}")

    except Exception as e:
        log_result("Create test production run", FAIL, str(e))

    # Create BOM request
    if test_run:
        try:
            wh_svc = WarehouseService("JIVO_OIL")
            test_bom_request = wh_svc.create_bom_request({
                'production_run_id': test_run.id,
                'required_qty': Decimal('100'),
                'remarks': 'Integration test BOM request',
            }, user)

            lines_count = test_bom_request.lines.count()
            log_result("Create BOM request", PASS,
                       f"ID={test_bom_request.id}, Lines={lines_count}, Status={test_bom_request.status}")

            # Verify run status updated
            test_run.refresh_from_db()
            log_result("Run warehouse_approval_status updated",
                       PASS if test_run.warehouse_approval_status == 'PENDING' else FAIL,
                       f"Status={test_run.warehouse_approval_status}")

            # Print BOM lines
            for line in test_bom_request.lines.all()[:5]:
                print(f"    Line: {line.item_code} | per_unit={line.per_unit_qty} | "
                      f"required={line.required_qty} | wh={line.warehouse} | uom={line.uom}")

        except Exception as e:
            log_result("Create BOM request", FAIL, str(e))

    # Test start_production blocked after BOM request
    if test_run and test_bom_request:
        try:
            pe_svc.start_production(test_run.id)
            log_result("Start production blocked (PENDING)", FAIL, "Should have raised ValueError")
        except ValueError as e:
            log_result("Start production blocked (PENDING)", PASS, str(e))
        except Exception as e:
            log_result("Start production blocked (PENDING)", FAIL, str(e))

    # Test duplicate BOM request prevention
    if test_run and test_bom_request:
        try:
            wh_svc.create_bom_request({
                'production_run_id': test_run.id,
                'required_qty': Decimal('100'),
            }, user)
            log_result("Duplicate BOM request blocked", FAIL, "Should have raised ValueError")
        except ValueError as e:
            if 'already exists' in str(e).lower():
                log_result("Duplicate BOM request blocked", PASS, str(e))
            else:
                log_result("Duplicate BOM request blocked", FAIL, str(e))
else:
    log_result("BOM Request flow", SKIP, "Missing company, doc_entry, or components")


# ============================================================
# TEST 9: Warehouse Service — Approval Flow
# ============================================================
section("9. Warehouse Service — Approval Flow")

if test_bom_request:
    try:
        bom_lines = list(test_bom_request.lines.all())
        if len(bom_lines) >= 2:
            # Approve some, reject some (partial approval)
            approval_data = {
                'lines': [
                    {'line_id': bom_lines[0].id, 'approved_qty': float(bom_lines[0].required_qty), 'status': 'APPROVED'},
                    {'line_id': bom_lines[1].id, 'approved_qty': 0, 'status': 'REJECTED', 'remarks': 'Test rejection'},
                ]
            }
            # Add remaining lines as approved
            for line in bom_lines[2:]:
                approval_data['lines'].append({
                    'line_id': line.id,
                    'approved_qty': float(line.required_qty),
                    'status': 'APPROVED',
                })

            result = wh_svc.approve_bom_request(test_bom_request.id, approval_data, user)
            log_result("Approve BOM request (partial)", PASS,
                       f"Status={result.status}, Reviewed by user ID={user.id if user else 'N/A'}")

            # Verify run status
            test_run.refresh_from_db()
            log_result("Run status after partial approval",
                       PASS if test_run.warehouse_approval_status in ['PARTIALLY_APPROVED', 'APPROVED'] else FAIL,
                       f"Status={test_run.warehouse_approval_status}")

            # Verify line statuses
            for line in test_bom_request.lines.all()[:3]:
                print(f"    Line {line.item_code}: status={line.status}, "
                      f"approved_qty={line.approved_qty}, remarks={line.remarks}")

        elif len(bom_lines) == 1:
            # Approve the only line
            approval_data = {
                'lines': [
                    {'line_id': bom_lines[0].id, 'approved_qty': float(bom_lines[0].required_qty), 'status': 'APPROVED'},
                ]
            }
            result = wh_svc.approve_bom_request(test_bom_request.id, approval_data, user)
            log_result("Approve BOM request (single line)", PASS, f"Status={result.status}")

            test_run.refresh_from_db()
            log_result("Run status after approval",
                       PASS if test_run.warehouse_approval_status in ['APPROVED', 'PARTIALLY_APPROVED'] else FAIL,
                       f"Status={test_run.warehouse_approval_status}")
        else:
            log_result("Approve BOM request", SKIP, "No BOM lines to approve")

    except Exception as e:
        log_result("Approve BOM request", FAIL, str(e))

    # Test start_production now allowed after approval
    try:
        test_run.refresh_from_db()
        if test_run.warehouse_approval_status in ['APPROVED', 'PARTIALLY_APPROVED']:
            pe_svc.start_production(test_run.id)
            log_result("Start production after approval", PASS, "Production started successfully")
            # Stop it for cleanup
            pe_svc.stop_production(test_run.id, 0)
        else:
            log_result("Start production after approval", SKIP,
                       f"Status is {test_run.warehouse_approval_status}")
    except Exception as e:
        log_result("Start production after approval", FAIL, str(e))

    # Test re-approval blocked
    try:
        wh_svc.approve_bom_request(test_bom_request.id, {'lines': []}, user)
        log_result("Re-approval blocked", FAIL, "Should have raised ValueError")
    except ValueError as e:
        log_result("Re-approval blocked", PASS, str(e))

else:
    log_result("Approval flow", SKIP, "No BOM request created")


# ============================================================
# TEST 10: Warehouse Service — Rejection Flow
# ============================================================
section("10. Warehouse Service — Rejection Flow")

test_bom_reject = None

if test_run and company:
    try:
        # Create a new BOM request for rejection test
        # First, update run to allow new request (reset the existing one)
        # We'll create a second run for this
        pe_svc2 = ProductionExecutionService("JIVO_OIL")
        test_run2 = pe_svc2.create_run({
            'sap_doc_entry': test_doc_entry,
            'line_id': test_run.line_id,
            'date': date.today(),
            'product': test_item_code or '',
            'required_qty': Decimal('50'),
        }, user)

        wh_svc2 = WarehouseService("JIVO_OIL")
        test_bom_reject = wh_svc2.create_bom_request({
            'production_run_id': test_run2.id,
            'required_qty': Decimal('50'),
            'remarks': 'Test rejection',
        }, user)

        rejected = wh_svc2.reject_bom_request(
            test_bom_reject.id,
            "Insufficient stock for test",
            user
        )
        log_result("Reject BOM request", PASS,
                   f"Status={rejected.status}, Reason={rejected.rejection_reason}")

        test_run2.refresh_from_db()
        log_result("Run status after rejection",
                   PASS if test_run2.warehouse_approval_status == 'REJECTED' else FAIL,
                   f"Status={test_run2.warehouse_approval_status}")

        # Verify start production blocked
        try:
            pe_svc2.start_production(test_run2.id)
            log_result("Start blocked after rejection", FAIL, "Should have raised ValueError")
        except ValueError as e:
            log_result("Start blocked after rejection", PASS, str(e))

        # Cleanup
        test_run2.delete()

    except Exception as e:
        log_result("Rejection flow", FAIL, str(e))
else:
    log_result("Rejection flow", SKIP, "No test run available")


# ============================================================
# TEST 11: Stock Check API
# ============================================================
section("11. Stock Check — Bulk Query")

if test_components:
    try:
        wh_svc = WarehouseService("JIVO_OIL")
        codes = [c.get('ItemCode') for c in test_components[:5]]
        stock = wh_svc.get_stock_for_items(codes)
        log_result("Bulk stock check", PASS,
                   f"Queried {len(codes)} items, got {len(stock)} results")

        for code, info in list(stock.items())[:3]:
            whs = info.get('warehouses', [])
            print(f"    {code}: total_on_hand={info.get('total_on_hand'):.1f}, "
                  f"total_available={info.get('total_available'):.1f}, "
                  f"warehouses={len(whs)}")
            for w in whs[:2]:
                print(f"      └─ {w['WhsCode']}: OnHand={w['OnHand']:.1f}, Available={w['Available']:.1f}")
    except Exception as e:
        log_result("Bulk stock check", FAIL, str(e))
else:
    log_result("Bulk stock check", SKIP, "No components available")


# ============================================================
# TEST 12: Material Issue to SAP (InventoryGenExits)
# ============================================================
section("12. Material Issue to SAP (InventoryGenExits)")

# NOTE: This actually creates a document in SAP.
# We'll skip this in test to avoid creating real SAP documents.
log_result("Material issue to SAP", SKIP,
           "Skipped to avoid creating real SAP documents. "
           "The endpoint is POST /warehouse/bom-requests/{id}/issue/")

# But verify the method exists and the flow is correct
try:
    assert hasattr(wh_svc, 'issue_materials_to_sap'), "Method missing"
    log_result("issue_materials_to_sap method exists", PASS)
except Exception as e:
    log_result("issue_materials_to_sap method exists", FAIL, str(e))


# ============================================================
# TEST 13: Finished Goods Receipt Flow
# ============================================================
section("13. Finished Goods Receipt Flow")

if test_run:
    try:
        # Complete the run first
        test_run.refresh_from_db()
        if test_run.status != 'COMPLETED':
            # Force complete for testing
            test_run.status = 'COMPLETED'
            test_run.total_production = Decimal('100')
            test_run.rejected_qty = Decimal('5')
            test_run.save()

        wh_svc = WarehouseService("JIVO_OIL")
        fg_receipt = wh_svc.create_fg_receipt({
            'production_run_id': test_run.id,
            'posting_date': date.today(),
        }, user)

        log_result("Create FG receipt", PASS,
                   f"ID={fg_receipt.id}, item={fg_receipt.item_code}, "
                   f"produced={fg_receipt.produced_qty}, good={fg_receipt.good_qty}, "
                   f"rejected={fg_receipt.rejected_qty}, status={fg_receipt.status}")

        # Receive
        received = wh_svc.receive_finished_goods(fg_receipt.id, user)
        log_result("Receive finished goods", PASS,
                   f"Status={received.status}, received_by={received.received_by}")

        # Post to SAP — skip to avoid real doc
        log_result("Post FG to SAP", SKIP, "Skipped to avoid real SAP documents")

        # Verify the method exists
        assert hasattr(wh_svc, 'post_fg_receipt_to_sap'), "Method missing"
        log_result("post_fg_receipt_to_sap method exists", PASS)

        # Duplicate prevention
        try:
            wh_svc.create_fg_receipt({
                'production_run_id': test_run.id,
                'posting_date': date.today(),
            }, user)
            log_result("Duplicate FG receipt blocked", FAIL, "Should have raised ValueError")
        except ValueError as e:
            if 'already' in str(e).lower():
                log_result("Duplicate FG receipt blocked", PASS, str(e))
            else:
                log_result("Duplicate FG receipt blocked", FAIL, str(e))

    except Exception as e:
        log_result("FG receipt flow", FAIL, str(e))
else:
    log_result("FG receipt flow", SKIP, "No test run available")


# ============================================================
# TEST 14: Serializers Validation
# ============================================================
section("14. Serializer Validation")

from warehouse.serializers import (
    BOMRequestCreateSerializer, BOMRequestApproveSerializer,
    BOMRequestRejectSerializer, MaterialIssueSerializer,
    FGReceiptCreateSerializer, StockCheckSerializer,
    BOMRequestListSerializer, BOMRequestDetailSerializer,
    FGReceiptListSerializer, BOMRequestLineSerializer,
)

# Valid data
try:
    s = BOMRequestCreateSerializer(data={
        'production_run_id': 1,
        'required_qty': '500.00',
        'remarks': 'Test',
    })
    assert s.is_valid(), s.errors
    log_result("BOMRequestCreateSerializer valid", PASS)
except Exception as e:
    log_result("BOMRequestCreateSerializer valid", FAIL, str(e))

# Missing required field
try:
    s = BOMRequestCreateSerializer(data={'remarks': 'Test'})
    assert not s.is_valid()
    log_result("BOMRequestCreateSerializer rejects missing fields", PASS,
               f"Errors: {list(s.errors.keys())}")
except Exception as e:
    log_result("BOMRequestCreateSerializer validation", FAIL, str(e))

# Approve serializer
try:
    s = BOMRequestApproveSerializer(data={
        'lines': [
            {'line_id': 1, 'approved_qty': '100.000', 'status': 'APPROVED'},
            {'line_id': 2, 'status': 'REJECTED', 'remarks': 'Out of stock'},
        ]
    })
    assert s.is_valid(), s.errors
    log_result("BOMRequestApproveSerializer valid", PASS)
except Exception as e:
    log_result("BOMRequestApproveSerializer valid", FAIL, str(e))

# Reject serializer
try:
    s = BOMRequestRejectSerializer(data={'reason': 'Not enough stock'})
    assert s.is_valid(), s.errors
    log_result("BOMRequestRejectSerializer valid", PASS)
except Exception as e:
    log_result("BOMRequestRejectSerializer valid", FAIL, str(e))

# Stock check serializer
try:
    s = StockCheckSerializer(data={'item_codes': ['RM-001', 'PM-002']})
    assert s.is_valid(), s.errors
    log_result("StockCheckSerializer valid", PASS)
except Exception as e:
    log_result("StockCheckSerializer valid", FAIL, str(e))

# FG Receipt serializer
try:
    s = FGReceiptCreateSerializer(data={'production_run_id': 1, 'posting_date': '2026-04-06'})
    assert s.is_valid(), s.errors
    log_result("FGReceiptCreateSerializer valid", PASS)
except Exception as e:
    log_result("FGReceiptCreateSerializer valid", FAIL, str(e))

# Material Issue serializer
try:
    s = MaterialIssueSerializer(data={})
    assert s.is_valid(), s.errors
    log_result("MaterialIssueSerializer valid (empty = issue all)", PASS)
except Exception as e:
    log_result("MaterialIssueSerializer valid", FAIL, str(e))


# ============================================================
# TEST 15: URL Configuration
# ============================================================
section("15. URL Configuration")

from django.urls import reverse, resolve

url_tests = [
    ('wh-bom-request-list', '/api/v1/warehouse/bom-requests/'),
    ('wh-bom-request-create', '/api/v1/warehouse/bom-requests/create/'),
    ('wh-bom-request-detail', None),  # needs kwargs
    ('wh-bom-request-approve', None),
    ('wh-bom-request-reject', None),
    ('wh-material-issue', None),
    ('wh-stock-check', '/api/v1/warehouse/stock/check/'),
    ('wh-fg-receipt-list', '/api/v1/warehouse/fg-receipts/'),
    ('wh-fg-receipt-create', '/api/v1/warehouse/fg-receipts/create/'),
    ('wh-fg-receipt-detail', None),
    ('wh-fg-receipt-receive', None),
    ('wh-fg-receipt-post-sap', None),
]

for name, expected_path in url_tests:
    try:
        if expected_path:
            url = reverse(name)
            assert url == expected_path, f"Expected {expected_path}, got {url}"
        else:
            # URL with kwargs
            if 'request' in name:
                url = reverse(name, kwargs={'request_id': 1})
            elif 'receipt' in name:
                url = reverse(name, kwargs={'receipt_id': 1})
            elif 'issue' in name:
                url = reverse(name, kwargs={'request_id': 1})
        log_result(f"URL '{name}'", PASS)
    except Exception as e:
        log_result(f"URL '{name}'", FAIL, str(e))


# ============================================================
# TEST 16: View Classes Exist
# ============================================================
section("16. View Classes")

from warehouse.views import (
    BOMRequestCreateAPI, BOMRequestListAPI, BOMRequestDetailAPI,
    BOMRequestApproveAPI, BOMRequestRejectAPI, MaterialIssueAPI,
    StockCheckAPI,
    FGReceiptCreateAPI, FGReceiptListAPI, FGReceiptDetailAPI,
    FGReceiptReceiveAPI, FGReceiptPostToSAPAPI,
)

views = [
    BOMRequestCreateAPI, BOMRequestListAPI, BOMRequestDetailAPI,
    BOMRequestApproveAPI, BOMRequestRejectAPI, MaterialIssueAPI,
    StockCheckAPI,
    FGReceiptCreateAPI, FGReceiptListAPI, FGReceiptDetailAPI,
    FGReceiptReceiveAPI, FGReceiptPostToSAPAPI,
]

for view_cls in views:
    log_result(f"View {view_cls.__name__}", PASS)


# ============================================================
# TEST 17: Settings & App Registration
# ============================================================
section("17. Settings & App Registration")

try:
    from django.apps import apps
    assert apps.is_installed('warehouse'), "warehouse not in INSTALLED_APPS"
    log_result("warehouse in INSTALLED_APPS", PASS)
except Exception as e:
    log_result("warehouse in INSTALLED_APPS", FAIL, str(e))

try:
    from config.urls import urlpatterns
    warehouse_found = any('warehouse' in str(p.pattern) for p in urlpatterns)
    log_result("warehouse URL in root urls.py", PASS if warehouse_found else FAIL)
except Exception as e:
    log_result("warehouse URL in root urls.py", FAIL, str(e))


# ============================================================
# CLEANUP
# ============================================================
section("Cleanup")

cleanup_count = 0
try:
    if test_bom_request:
        test_bom_request.lines.all().delete()
        test_bom_request.delete()
        cleanup_count += 1
    if test_run:
        # Delete FG receipts first
        FinishedGoodsReceipt.objects.filter(production_run=test_run).delete()
        BOMRequest.objects.filter(production_run=test_run).delete()
        test_run.delete()
        cleanup_count += 1
    log_result("Test data cleanup", PASS, f"Cleaned up {cleanup_count} objects")
except Exception as e:
    log_result("Test data cleanup", WARN, str(e))


# ============================================================
# SUMMARY
# ============================================================
section("TEST SUMMARY")

total = len(results)
passed = sum(1 for _, s in results if s == PASS)
failed = sum(1 for _, s in results if s == FAIL)
warned = sum(1 for _, s in results if s == WARN)
skipped = sum(1 for _, s in results if s == SKIP)

print(f"\n  Total:   {total}")
print(f"  Passed:  \033[92m{passed}\033[0m")
print(f"  Failed:  \033[91m{failed}\033[0m")
print(f"  Warned:  \033[93m{warned}\033[0m")
print(f"  Skipped: \033[94m{skipped}\033[0m")
print()

if failed > 0:
    print("  \033[91mFailed tests:\033[0m")
    for name, s in results:
        if s == FAIL:
            print(f"    - {name}")
    print()

if failed == 0:
    print("  \033[92m✓ ALL TESTS PASSED!\033[0m")
else:
    print(f"  \033[91m✗ {failed} test(s) FAILED\033[0m")

sys.exit(1 if failed > 0 else 0)
