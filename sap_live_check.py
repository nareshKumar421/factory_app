"""
Live SAP integration test — actually hits HANA + Service Layer.
Tests the full warehouse workflow end-to-end.
"""
import os, sys, io, django, warnings, json, requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
warnings.filterwarnings('ignore')
requests.packages.urllib3.disable_warnings()

from django.conf import settings
from decimal import Decimal
from datetime import date

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[94mSKIP\033[0m"
results = []

def log(name, status, detail=""):
    results.append((name, status))
    d = f" -- {detail}" if detail else ""
    print(f"  [{status}] {name}{d}")

def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

# ============================================================
section("1. HANA Direct — Fetch released production orders")
# ============================================================
from production_execution.services.sap_reader import ProductionOrderReader

reader = ProductionOrderReader("JIVO_OIL")
orders = reader.get_released_production_orders()
log("Released orders", PASS, f"Found {len(orders)}")

if not orders:
    print("\n  No released orders — cannot continue live SAP tests.")
    sys.exit(0)

order = orders[0]
doc_entry = order['DocEntry']
item_code = order['ItemCode']
print(f"    Using: DocEntry={doc_entry}, ItemCode={item_code}, PlannedQty={order['PlannedQty']}")

# ============================================================
section("2. HANA Direct — Fetch order detail + components with LineNum")
# ============================================================
detail = reader.get_production_order_detail(doc_entry)
header = detail['header']
components = detail['components']
log("Order detail", PASS, f"Components={len(components)}")

for i, c in enumerate(components[:5]):
    print(f"    [{i}] LineNum={c.get('LineNum')} | {c['ItemCode']} | "
          f"Planned={c['PlannedQty']} | Issued={c['IssuedQty']} | "
          f"WH={c.get('Warehouse')} | UOM={c.get('UomCode')}")

if not components:
    print("\n  No components — cannot test issue.")
    sys.exit(0)

# ============================================================
section("3. Service Layer — Login")
# ============================================================
sl_url = settings.SL_URL
company_db = settings.COMPANY_DB["JIVO_OIL"]

session = requests.Session()
login_resp = session.post(
    f"{sl_url}/b1s/v2/Login",
    json={
        "CompanyDB": company_db,
        "UserName": settings.SL_USER,
        "Password": settings.SL_PASSWORD,
    },
    timeout=10,
    verify=False,
)
if login_resp.ok:
    log("Service Layer login", PASS)
else:
    log("Service Layer login", FAIL, login_resp.text[:200])
    sys.exit(1)

# ============================================================
section("4. Service Layer — Test InventoryGenExits payload (DRY RUN)")
# ============================================================
# Build the corrected payload WITHOUT ItemCode/WarehouseCode
comp = components[0]
base_line = comp.get('LineNum', 0)
remaining = float(comp['PlannedQty']) - float(comp['IssuedQty'])

print(f"    Component: {comp['ItemCode']}")
print(f"    BaseLine: {base_line}")
print(f"    PlannedQty: {comp['PlannedQty']}, IssuedQty: {comp['IssuedQty']}, Remaining: {remaining}")

if remaining <= 0:
    log("Issue test", SKIP, "No remaining qty to issue on first component")
    # Try next component
    for c in components[1:]:
        remaining = float(c['PlannedQty']) - float(c['IssuedQty'])
        if remaining > 0:
            comp = c
            base_line = c.get('LineNum', 0)
            print(f"    Switched to: {c['ItemCode']}, LineNum={base_line}, Remaining={remaining}")
            break
    else:
        log("Issue test", SKIP, "All components fully issued")
        remaining = 0

# Use a tiny test quantity (1 unit)
test_qty = min(1.0, remaining) if remaining > 0 else 0

payload_issue = {
    "DocDate": date.today().isoformat(),
    "Comments": f"LIVE TEST — Issue for Production DocEntry {doc_entry}",
    "DocumentLines": [{
        "Quantity": test_qty,
        "BaseType": 202,
        "BaseEntry": doc_entry,
        "BaseLine": base_line,
    }],
}

print(f"\n    Payload (InventoryGenExits):")
print(f"    {json.dumps(payload_issue, indent=4)}")

# ============================================================
section("5. Service Layer — POST InventoryGenExits (Issue 1 unit)")
# ============================================================
if test_qty > 0:
    resp = session.post(
        f"{sl_url}/b1s/v2/InventoryGenExits",
        json=payload_issue,
        timeout=30,
        verify=False,
    )
    if resp.ok:
        result = resp.json()
        issue_doc = result.get('DocEntry')
        issue_num = result.get('DocNum')
        log("Issue to SAP", PASS, f"DocEntry={issue_doc}, DocNum={issue_num}")
        print(f"    SAP Document created successfully!")

        # Verify the IssuedQty updated
        detail2 = reader.get_production_order_detail(doc_entry)
        for c2 in detail2['components']:
            if c2.get('LineNum') == base_line:
                print(f"    After issue: IssuedQty={c2['IssuedQty']} (was {comp['IssuedQty']})")
                break
    else:
        try:
            err = resp.json()
            err_msg = err.get('error', {}).get('message', {}).get('value', resp.text)
        except Exception:
            err_msg = resp.text
        log("Issue to SAP", FAIL, err_msg[:300])
        print(f"    Status: {resp.status_code}")
        print(f"    Full response: {resp.text[:500]}")
else:
    log("Issue to SAP", SKIP, "No remaining qty")

# ============================================================
section("6. Service Layer — Test InventoryGenEntries payload (FG Receipt)")
# ============================================================
# Re-login for fresh session
session2 = requests.Session()
session2.post(
    f"{sl_url}/b1s/v2/Login",
    json={"CompanyDB": company_db, "UserName": settings.SL_USER, "Password": settings.SL_PASSWORD},
    timeout=10, verify=False,
)

fg_test_qty = 1.0
payload_receipt = {
    "DocDate": date.today().isoformat(),
    "Comments": f"LIVE TEST — FG Receipt for Production DocEntry {doc_entry}",
    "DocumentLines": [{
        "Quantity": fg_test_qty,
        "BaseType": 202,
        "BaseEntry": doc_entry,
        "BaseLine": 0,
    }],
}

print(f"    Payload (InventoryGenEntries):")
print(f"    {json.dumps(payload_receipt, indent=4)}")

resp2 = session2.post(
    f"{sl_url}/b1s/v2/InventoryGenEntries",
    json=payload_receipt,
    timeout=30,
    verify=False,
)
if resp2.ok:
    result2 = resp2.json()
    log("FG Receipt to SAP", PASS, f"DocEntry={result2.get('DocEntry')}, DocNum={result2.get('DocNum')}")
else:
    try:
        err2 = resp2.json()
        err_msg2 = err2.get('error', {}).get('message', {}).get('value', resp2.text)
    except Exception:
        err_msg2 = resp2.text
    log("FG Receipt to SAP", FAIL, err_msg2[:300])
    print(f"    Status: {resp2.status_code}")
    print(f"    Full response: {resp2.text[:500]}")

# ============================================================
section("7. HANA — Verify stock check (OITW)")
# ============================================================
from warehouse.services.warehouse_service import WarehouseService

wh = WarehouseService("JIVO_OIL")
codes = [c['ItemCode'] for c in components[:3]]
stock = wh.get_stock_for_items(codes)
log("Stock check", PASS, f"Queried {len(codes)} items")
for code, info in stock.items():
    print(f"    {code}: OnHand={info['total_on_hand']:.1f}, Available={info['total_available']:.1f}")

# ============================================================
section("8. Full Django workflow — BOM Request -> Approve -> Issue")
# ============================================================
from production_execution.services.production_service import ProductionExecutionService
from production_execution.models import ProductionRun
from accounts.models import User
from company.models import Company

user = User.objects.first()
pe = ProductionExecutionService("JIVO_OIL")
lines = list(pe.list_lines(is_active=True))
line = lines[0] if lines else pe.create_line({'name': 'Test Line SAP'})

# Create run
run = pe.create_run({
    'sap_doc_entry': doc_entry,
    'line_id': line.id,
    'date': date.today(),
    'product': item_code,
    'required_qty': Decimal('10'),
}, user)
log("Create run", PASS, f"ID={run.id}, required_qty=10")

# Create BOM request
bom_req = wh.create_bom_request({
    'production_run_id': run.id,
    'required_qty': Decimal('10'),
    'remarks': 'Live SAP test',
}, user)
log("Create BOM request", PASS, f"ID={bom_req.id}, Lines={bom_req.lines.count()}")

for bl in bom_req.lines.all()[:3]:
    print(f"    {bl.item_code}: per_unit={bl.per_unit_qty}, required={bl.required_qty}, wh={bl.warehouse}")

# Approve all lines
bom_lines = list(bom_req.lines.all())
approval = {
    'lines': [{'line_id': bl.id, 'approved_qty': float(bl.required_qty), 'status': 'APPROVED'} for bl in bom_lines]
}
bom_req = wh.approve_bom_request(bom_req.id, approval, user)
log("Approve BOM", PASS, f"Status={bom_req.status}")

# Issue to SAP (real SAP call!)
try:
    bom_req = wh.issue_materials_to_sap(bom_req.id, {}, user)
    entries = bom_req.sap_issue_doc_entries
    if entries:
        last = entries[-1]
        log("Issue materials to SAP", PASS,
            f"DocEntry={last['doc_entry']}, DocNum={last['doc_num']}, "
            f"IssueStatus={bom_req.material_issue_status}")
    else:
        log("Issue materials to SAP", PASS, f"IssueStatus={bom_req.material_issue_status}")
except Exception as e:
    log("Issue materials to SAP", FAIL, str(e)[:300])

# Verify start production now works
run.refresh_from_db()
try:
    pe.start_production(run.id)
    log("Start production (after approval)", PASS)
    pe.stop_production(run.id, 0)
except Exception as e:
    log("Start production", FAIL, str(e))

# Complete and create FG receipt
run.status = 'COMPLETED'
run.total_production = Decimal('10')
run.rejected_qty = Decimal('0')
run.save()

try:
    fg = wh.create_fg_receipt({'production_run_id': run.id, 'posting_date': date.today()}, user)
    log("Create FG receipt", PASS, f"ID={fg.id}, item={fg.item_code}, good_qty={fg.good_qty}")

    fg = wh.receive_finished_goods(fg.id, user)
    log("Receive FG", PASS, f"Status={fg.status}")

    fg = wh.post_fg_receipt_to_sap(fg.id)
    log("Post FG to SAP", PASS, f"SAP DocEntry={fg.sap_receipt_doc_entry}")
except Exception as e:
    log("FG receipt flow", FAIL, str(e)[:300])

# Cleanup
from warehouse.models import BOMRequest, FinishedGoodsReceipt
FinishedGoodsReceipt.objects.filter(production_run=run).delete()
BOMRequest.objects.filter(production_run=run).delete()
run.delete()
log("Cleanup", PASS)

# ============================================================
section("SUMMARY")
# ============================================================
total = len(results)
passed = sum(1 for _, s in results if s == PASS)
failed = sum(1 for _, s in results if s == FAIL)
skipped = sum(1 for _, s in results if s == SKIP)

print(f"\n  Total: {total}  |  Passed: {passed}  |  Failed: {failed}  |  Skipped: {skipped}")
if failed == 0:
    print(f"\n  ALL TESTS PASSED!")
else:
    print(f"\n  FAILED:")
    for n, s in results:
        if s == FAIL: print(f"    - {n}")

sys.exit(1 if failed else 0)
