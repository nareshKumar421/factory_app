"""Step-by-step tests for the Flipkart sheet-driven flow.

Covers each stage of MARKETPLACE_FLIPKART_SHEET_FLOW.md at the service layer:
import → stock list (combo explosion) → unmapped gate → warehouse issue request
(partial approve / issue / receive) → issuance export → cancellation guard →
confirm (pricing).
"""
import csv
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from company.models import Company

from .models import (
    ComboComponentType,
    ComboDefinition,
    ComboComponent,
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceIssueLineStatus,
    MarketplaceIssueStatus,
    MarketplaceOrderBilling,
    MarketplaceOrderStatus,
    MarketplaceScan,
    MarketplaceWarehouse,
    OrderImportBatch,
    SkuMapping,
    SkuType,
)
from .services import (
    batch_resolve_service,
    issuance_export_service,
    issue_request_service,
)
from .services.confirm_service import confirm_dispatch
from .services.errors import MarketplaceError
from .services.order_import_service import ingest

HEADER = [
    "Ordered On", "ORDER ITEM ID", "Order Id", "Order State", "Order Type",
    "FSN", "SKU", "Product", "Invoice Amount", "CGST", "IGST", "SGST",
    "Selling Price Per Item", "Quantity", "Buyer name",
]


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def row(order_id, sku, qty, *, item_id="'400", state="Approved", fsn="FSNX",
        product="Prod", invoice="0", buyer="Alice"):
    return [
        "Jul 11, 2026", item_id, order_id, state, "NON_FBF", fsn, sku, product,
        invoice, "NA", "5", "NA", "500", str(qty), buyer,
    ]


class SheetFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code="TST")
        User = get_user_model()
        cls.user = User.objects.create(
            email="t@example.com", full_name="Tester", employee_code="T1", is_active=True
        )
        ch = MarketplaceChannel.FLIPKART

        MarketplaceWarehouse.objects.create(
            company=cls.company, channel=ch, name="Main",
            sap_warehouse_code="WH1", sap_customer_card_code="C-FLIP",
        )
        # RAW mappings
        SkuMapping.objects.create(
            company=cls.company, channel=ch, marketplace_sku="Extra Virgin 1L",
            sku_type=SkuType.RAW, fg_item_code="EV-1L", fg_item_name="EV 1L",
        )
        SkuMapping.objects.create(
            company=cls.company, channel=ch, marketplace_sku="Canola 5L",
            sku_type=SkuType.RAW, fg_item_code="CAN-5L", fg_item_name="Canola 5L",
        )
        # COMBO: Canola 5+1L → CAN-5L + CAN-1L + PM box
        combo = ComboDefinition.objects.create(
            company=cls.company, channel=ch, code="CANOLA-5+1", name="Canola 5+1L",
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code="CAN-5L",
            item_name="Canola 5L", quantity=Decimal("1"),
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code="CAN-1L",
            item_name="Canola 1L", quantity=Decimal("1"),
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.PM, item_code="PM-BOX",
            item_name="Carton", quantity=Decimal("1"),
        )
        SkuMapping.objects.create(
            company=cls.company, channel=ch, marketplace_sku="Canola 5+1L",
            sku_type=SkuType.COMBO, combo=combo,
        )

    # ── Step 1–2: import ──────────────────────────────────────────────────────
    def _ingest_main(self):
        text = make_csv([
            row("OD1", "Extra Virgin 1L", 1, invoice="900"),
            row("OD2", "Canola 5+1L", 2, invoice="1530"),
            row("OD3", "Canola 5L", 1, invoice="877"),
            row("OD4", "Extra Virgin 1L", 1, state="Cancelled", invoice="0"),
        ])
        return ingest(self.company, text=text, filename="orders.csv", user=self.user)

    def test_import_creates_orders_and_lines(self):
        batch = self._ingest_main()
        self.assertEqual(batch.order_count, 4)
        self.assertEqual(batch.line_count, 4)
        self.assertEqual(batch.orders.count(), 4)
        od2 = batch.orders.get(order_id="OD2")
        line = od2.lines.get()
        self.assertEqual(line.marketplace_sku, "Canola 5+1L")
        self.assertEqual(line.ordered_quantity, Decimal("2"))
        self.assertEqual(line.order_item_id, "400")  # leading apostrophe stripped
        # cancelled order flagged + not dispatchable
        od4 = batch.orders.get(order_id="OD4")
        self.assertTrue(od4.is_cancelled)
        self.assertEqual(od4.status, MarketplaceOrderStatus.RETURNED)

    def test_import_is_idempotent(self):
        self._ingest_main()
        self._ingest_main()  # re-upload same sheet
        from .models import MarketplaceOrder
        self.assertEqual(
            MarketplaceOrder.objects.filter(company=self.company, order_id="OD2").count(), 1
        )
        # lines replaced, not duplicated
        od2 = MarketplaceOrder.objects.get(company=self.company, order_id="OD2")
        self.assertEqual(od2.lines.count(), 1)

    def test_analyze_flags_duplicates(self):
        self._ingest_main()  # creates OD1..OD4
        from .services.order_import_service import analyze
        text = make_csv([row("OD1", "Extra Virgin 1L", 1), row("ODNEW", "Canola 5L", 1)])
        rep = analyze(self.company, text=text)
        self.assertTrue(rep["has_duplicates"])
        self.assertIn("OD1", rep["duplicate_order_ids"])
        self.assertIn("ODNEW", rep["new_order_ids"])
        self.assertEqual(rep["duplicate_count"], 1)
        self.assertEqual(rep["new_count"], 1)

    def test_skip_duplicates_leaves_existing_untouched(self):
        self._ingest_main()  # OD1 has 1× Extra Virgin 1L
        from .models import MarketplaceOrder
        # Re-import OD1 with a changed qty + a brand-new order, choosing to skip dups.
        text = make_csv([row("OD1", "Extra Virgin 1L", 9), row("ODNEW", "Canola 5L", 1)])
        batch = ingest(self.company, text=text, filename="x.csv", user=self.user, skip_duplicates=True)
        od1 = MarketplaceOrder.objects.get(company=self.company, order_id="OD1")
        self.assertEqual(od1.lines.get().ordered_quantity, Decimal("1"))  # NOT refreshed to 9
        self.assertTrue(MarketplaceOrder.objects.filter(company=self.company, order_id="ODNEW").exists())
        self.assertEqual(batch.summary["duplicates_skipped"], 1)
        self.assertEqual(batch.summary["created"], 1)

    def test_acknowledged_reimport_refreshes_duplicate(self):
        self._ingest_main()
        from .models import MarketplaceOrder
        text = make_csv([row("OD1", "Extra Virgin 1L", 9)])
        ingest(self.company, text=text, filename="x.csv", user=self.user, skip_duplicates=False)
        od1 = MarketplaceOrder.objects.get(company=self.company, order_id="OD1")
        self.assertEqual(od1.lines.get().ordered_quantity, Decimal("9"))  # refreshed

    # ── Step 3: consolidated stock list (combo explosion) ─────────────────────
    def test_stock_list_combo_explosion(self):
        batch = self._ingest_main()
        stock = batch_resolve_service.build_stock_list(batch)
        self.assertEqual(stock["unmapped_skus"], [])
        self.assertEqual(stock["orders"], 3)  # OD4 cancelled excluded
        by_item = {(l["item_code"], l["component_type"]): l["required_quantity"] for l in stock["lines"]}
        self.assertEqual(by_item[("CAN-5L", "FG")], Decimal("3"))  # 2 (combo) + 1 (raw)
        self.assertEqual(by_item[("CAN-1L", "FG")], Decimal("2"))
        self.assertEqual(by_item[("EV-1L", "FG")], Decimal("1"))
        self.assertEqual(by_item[("PM-BOX", "PM")], Decimal("2"))

    # ── Step 3a: unmapped gate ────────────────────────────────────────────────
    def test_unmapped_sku_detected_and_blocks_issue(self):
        text = make_csv([row("OD9", "Mystery SKU", 1)])
        batch = ingest(self.company, text=text, filename="x.csv", user=self.user)
        stock = batch_resolve_service.build_stock_list(batch)
        self.assertIn("Mystery SKU", stock["unmapped_skus"])
        with self.assertRaises(MarketplaceError) as ctx:
            issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        self.assertEqual(ctx.exception.code, "UNMAPPED_SKUS")

    # ── Step 4–5: warehouse issue request (partial approve / issue / receive) ──
    def test_issue_request_partial_approve_issue_receive(self):
        batch = self._ingest_main()
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        self.assertEqual(req.status, MarketplaceIssueStatus.SENT)
        self.assertEqual(req.lines.count(), 4)
        batch.refresh_from_db()
        self.assertEqual(batch.status, OrderImportBatch.Status.REQUESTED)

        lines = {l.item_code: l for l in req.lines.all()}
        decisions = [
            {"line_id": lines["CAN-5L"].id, "approved_qty": "2", "status": "APPROVED"},  # partial (req 3)
            {"line_id": lines["CAN-1L"].id, "approved_qty": "2", "status": "APPROVED"},  # full
            {"line_id": lines["EV-1L"].id, "approved_qty": "1", "status": "APPROVED"},   # full
            {"line_id": lines["PM-BOX"].id, "status": "REJECTED", "reason": "no stock"},
        ]
        req = issue_request_service.review(req, decisions=decisions, user=self.user)
        self.assertEqual(req.status, MarketplaceIssueStatus.PARTIALLY_APPROVED)
        lines = {l.item_code: l for l in req.lines.all()}
        self.assertEqual(lines["CAN-5L"].status, MarketplaceIssueLineStatus.PARTIALLY_APPROVED)
        self.assertEqual(lines["PM-BOX"].status, MarketplaceIssueLineStatus.REJECTED)

        req = issue_request_service.issue(req, user=self.user)
        self.assertEqual(req.status, MarketplaceIssueStatus.ISSUED)
        self.assertEqual(req.lines.get(item_code="CAN-5L").issued_qty, Decimal("2"))
        self.assertEqual(req.lines.get(item_code="PM-BOX").issued_qty, Decimal("0"))

        req = issue_request_service.receive(req, user=self.user)
        self.assertEqual(req.status, MarketplaceIssueStatus.RECEIVED)
        self.assertEqual(req.lines.get(item_code="CAN-5L").received_qty, Decimal("2"))

    def test_review_rejects_over_approval(self):
        batch = self._ingest_main()
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        line = req.lines.get(item_code="EV-1L")  # required 1
        with self.assertRaises(MarketplaceError) as ctx:
            issue_request_service.review(
                req, decisions=[{"line_id": line.id, "approved_qty": "5", "status": "APPROVED"}],
                user=self.user,
            )
        self.assertEqual(ctx.exception.code, "INVALID_QTY")

    def test_warehouse_insights(self):
        from .services import warehouse_insights_service
        batch = self._ingest_main()
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        issue_request_service.review(
            req,
            decisions=[{"line_id": l.id, "approved_qty": str(l.required_qty), "status": "APPROVED"}
                       for l in req.lines.all()],
            user=self.user,
        )
        issue_request_service.issue(req, user=self.user)

        ins = warehouse_insights_service.build(self.company, MarketplaceChannel.FLIPKART)
        # Nothing dispatched yet → dispatched is "0" (not empty), everything in packing.
        self.assertEqual(ins["totals"]["dispatched"], "0")
        self.assertEqual(ins["orders"]["dispatched"], 0)
        self.assertEqual(ins["orders"]["awaiting_dispatch"], 3)  # OD1/OD2/OD3 (OD4 cancelled)
        ev = next(i for i in ins["by_item"] if i["item_code"] == "EV-1L")
        self.assertEqual(ev["issued"], "1")
        self.assertEqual(ev["dispatched"], "0")
        self.assertEqual(ev["in_packing"], "1")

    # ── Step 6: issuance export ───────────────────────────────────────────────
    def test_issuance_export_csv(self):
        batch = self._ingest_main()
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        issue_request_service.review(
            req,
            decisions=[{"line_id": l.id, "approved_qty": str(l.required_qty), "status": "APPROVED"}
                       for l in req.lines.all()],
            user=self.user,
        )
        csv_text = issuance_export_service.build_csv(batch)
        self.assertIn("Item Code", csv_text)
        self.assertIn("CAN-5L", csv_text)
        self.assertIn("orders.csv", csv_text)
        # header + 4 lines
        self.assertEqual(csv_text.strip().count("\n"), 4)

    # ── Step 7–8: cancellation guard + confirm pricing ────────────────────────
    @override_settings(MARKETPLACE_SIMULATE_SAP=True)
    def test_cancelled_order_blocks_confirm(self):
        batch = self._ingest_main()
        od4 = batch.orders.get(order_id="OD4")
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od4,
            status=MarketplaceDispatchStatus.READY,
        )
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(ctx.exception.code, "ORDER_CANCELLED")

    @override_settings(MARKETPLACE_SIMULATE_SAP=True)
    def test_confirm_blocked_until_materials_issued(self):
        """An order cannot be dispatched until the warehouse issues its materials."""
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="EV-1L",
            item_code="EV-1L", quantity=Decimal("1"), scanned_by=self.user,
        )
        from .services.dispatch_gate import order_is_issued
        self.assertFalse(order_is_issued(od1))
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_ISSUED")

        # After issuing, it becomes dispatchable.
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        issue_request_service.review(
            req,
            decisions=[{"line_id": l.id, "approved_qty": str(l.required_qty), "status": "APPROVED"}
                       for l in req.lines.all()],
            user=self.user,
        )
        issue_request_service.issue(req, user=self.user)
        self.assertTrue(order_is_issued(od1))
        confirm_dispatch(dispatch, user=self.user)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.CONFIRMED)

    @override_settings(MARKETPLACE_SIMULATE_SAP=True)
    def test_confirm_posts_dn_and_computes_total(self):
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")  # 1× EV-1L, invoice 900
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="EV-1L",
            item_code="EV-1L", quantity=Decimal("1"), scanned_by=self.user,
        )
        # Materials must be issued from the warehouse before an order can dispatch.
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        issue_request_service.review(
            req,
            decisions=[{"line_id": l.id, "approved_qty": str(l.required_qty), "status": "APPROVED"}
                       for l in req.lines.all()],
            user=self.user,
        )
        issue_request_service.issue(req, user=self.user)

        confirm_dispatch(dispatch, user=self.user)
        dispatch.refresh_from_db()
        od1.refresh_from_db()
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.CONFIRMED)
        self.assertEqual(od1.status, MarketplaceOrderStatus.DISPATCHED)
        self.assertTrue(dispatch.sap_delivery_note_num.startswith("SIMDN-"))
        billing = MarketplaceOrderBilling.objects.get(order_id="OD1")
        self.assertEqual(billing.total_amount, Decimal("900"))
