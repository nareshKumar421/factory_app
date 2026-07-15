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
    packing_service,
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

    # ── helpers: issue + pack an order so it can be dispatched ────────────────
    def _issue_batch(self, batch):
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        issue_request_service.review(
            req,
            decisions=[{"line_id": l.id, "approved_qty": str(l.required_qty), "status": "APPROVED"}
                       for l in req.lines.all()],
            user=self.user,
        )
        issue_request_service.issue(req, user=self.user)
        return req

    def _pack_order(self, order):
        packing = packing_service.start_or_get(order, user=self.user)
        packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)
        return packing

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
        text = make_csv([row("OD9", "Mystery SKU", 1, fsn="FSN-UNMAPPED")])
        batch = ingest(self.company, text=text, filename="x.csv", user=self.user)
        stock = batch_resolve_service.build_stock_list(batch)
        # FSN is the primary mapping key, so the unmapped row is reported by its FSN.
        self.assertIn("FSN-UNMAPPED", stock["unmapped_skus"])
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
    def test_confirm_blocked_until_packed(self):
        """An order cannot be dispatched until it is packed."""
        from .services.dispatch_gate import order_is_packed
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
        self.assertFalse(order_is_packed(od1))
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_PACKED")

        # Issue → pack → now dispatchable.
        self._issue_batch(batch)
        self._pack_order(od1)
        self.assertTrue(order_is_packed(od1))
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
        # Must be issued AND packed before an order can dispatch.
        self._issue_batch(batch)
        self._pack_order(od1)

        confirm_dispatch(dispatch, user=self.user)
        dispatch.refresh_from_db()
        od1.refresh_from_db()
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.CONFIRMED)
        self.assertEqual(od1.status, MarketplaceOrderStatus.DISPATCHED)
        self.assertTrue(dispatch.sap_delivery_note_num.startswith("SIMDN-"))
        billing = MarketplaceOrderBilling.objects.get(order_id="OD1")
        self.assertEqual(billing.total_amount, Decimal("900"))

    # ── Packing ───────────────────────────────────────────────────────────────
    def test_packing_generates_barcodes_and_gates_dispatch(self):
        from .models import MarketplacePackingStatus
        from .services.dispatch_gate import order_is_packed
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")  # Canola 5+1L ×2 → CAN-5L, CAN-1L (+PM)

        ready = packing_service.orders_ready_to_pack(self.company, MarketplaceChannel.FLIPKART)
        self.assertIn("OD2", [o.order_id for o in ready])
        self.assertFalse(order_is_packed(od2))

        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        self.assertEqual({b.item_code for b in bcs}, {"CAN-5L", "CAN-1L"})  # FG only, no PM
        self.assertTrue(all(b.barcode.startswith("PACK-") for b in bcs))
        packing.refresh_from_db()
        self.assertEqual(packing.status, MarketplacePackingStatus.PACKING)
        self.assertFalse(order_is_packed(od2))  # not completed yet

        # idempotent
        self.assertEqual(len(packing_service.generate_barcodes(packing, user=self.user)), len(bcs))

        packing_service.complete(packing, user=self.user)
        self.assertTrue(order_is_packed(od2))
        ready2 = packing_service.orders_ready_to_pack(self.company, MarketplaceChannel.FLIPKART)
        self.assertNotIn("OD2", [o.order_id for o in ready2])

    def test_packing_queue_keeps_packed_orders_for_reprint(self):
        """The packing-screen queue keeps packed orders (so labels can be reprinted),
        unlike ``orders_ready_to_pack`` which drops them once packed."""
        from .models import MarketplacePackingStatus
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")

        packing = packing_service.start_or_get(od2, user=self.user)
        packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)

        queue = packing_service.packing_queue(self.company, MarketplaceChannel.FLIPKART)
        row = {o.order_id: o for o in queue}.get("OD2")
        self.assertIsNotNone(row, "packed order should stay in the packing queue")
        self.assertEqual(row.packing.status, MarketplacePackingStatus.PACKED)
        self.assertEqual(row.line_count, od2.lines.count())

    def test_outward_scan_resolves_pack_barcode(self):
        from .services.scan_service import dispatch_progress, record_dispatch_scan
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")
        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)

        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.DRAFT,
        )
        can5 = next(b for b in bcs if b.item_code == "CAN-5L")
        scan, _created, _dup = record_dispatch_scan(dispatch, barcode_raw=can5.barcode, user=self.user)
        self.assertEqual(scan.item_code, "CAN-5L")
        self.assertEqual(scan.quantity, Decimal("2"))  # resolved to the order-line qty
        prog = {r["item_code"]: r["status"] for r in dispatch_progress(dispatch)}
        self.assertEqual(prog["CAN-5L"], "COMPLETE")

    def test_return_scan_resolves_pack_barcode(self):
        """A returned item carrying its PACK-… label resolves to the order line,
        just like at Outward (regression: previously raised ITEM_NOT_ON_ORDER)."""
        from .models import MarketplaceReturn, MarketplaceReturnStatus
        from .services.scan_service import record_return_scan, return_progress
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")
        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)

        mp_return = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceReturnStatus.DRAFT,
        )
        can5 = next(b for b in bcs if b.item_code == "CAN-5L")
        scan, _created, _dup = record_return_scan(mp_return, barcode_raw=can5.barcode, user=self.user)
        self.assertEqual(scan.item_code, "CAN-5L")
        self.assertEqual(scan.quantity, Decimal("2"))  # resolved to the order-line qty
        prog = {r["item_code"]: r["status"] for r in return_progress(mp_return)}
        self.assertEqual(prog["CAN-5L"], "COMPLETE")

    def test_return_submit_issues_return_note(self):
        """Submitting a return assigns a sequential RTN- note number and is idempotent."""
        from .models import MarketplaceReturn, MarketplaceReturnStatus
        from .services import return_service
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")

        r1 = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceReturnStatus.DRAFT,
        )
        return_service.submit_return(r1, user=self.user)
        self.assertEqual(r1.status, MarketplaceReturnStatus.SUBMITTED)
        self.assertTrue(r1.internal_credit_doc_num.startswith("RTN-"))
        self.assertTrue(r1.internal_credit_doc_num.endswith("00001"))
        self.assertIsNotNone(r1.submitted_at)

        # Idempotent — re-submitting keeps the same note number.
        note = r1.internal_credit_doc_num
        return_service.submit_return(r1, user=self.user)
        self.assertEqual(r1.internal_credit_doc_num, note)

        # A second return gets the next sequence for the day.
        od1 = batch.orders.get(order_id="OD1")
        r2 = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceReturnStatus.DRAFT,
        )
        return_service.submit_return(r2, user=self.user)
        self.assertTrue(r2.internal_credit_doc_num.endswith("00002"))

    def test_delivery_note_payload_uses_warehouse_master_config(self):
        """Series + tax code configured on the warehouse master land on the SAP payload."""
        from datetime import date
        from unittest import mock
        from .services.sap_gateway import MarketplaceSapGateway
        gw = MarketplaceSapGateway("JIVO_MART")
        gw.simulate = False
        fake_client = mock.MagicMock()
        fake_client.create_delivery_note.return_value = {"DocEntry": 5, "DocNum": "DN5"}
        gw._client = fake_client

        gw.create_delivery_note(
            ref=1, card_code="C-FLIP", warehouse_code="WH1",
            fg_lines=[{"item_code": "X", "required_quantity": Decimal("2"), "warehouse_code": ""}],
            doc_date=date(2026, 7, 13), num_at_card="OD9", series="4", tax_code="GST18",
        )
        payload = fake_client.create_delivery_note.call_args.args[0]
        self.assertEqual(payload["Series"], 4)
        self.assertEqual(payload["CardCode"], "C-FLIP")
        self.assertEqual(payload["NumAtCard"], "OD9")
        self.assertEqual(payload["DocumentLines"][0]["VatGroup"], "GST18")
        self.assertEqual(payload["DocumentLines"][0]["WarehouseCode"], "WH1")

    def test_warehouse_master_can_disable_goods_issue(self):
        """post_goods_issue=False on the master means no Goods Issue is posted."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus, MarketplaceWarehouse
        from .services import sap_gateway
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")
        self._pack_order(od2)
        MarketplaceWarehouse.objects.filter(company=self.company).update(post_goods_issue=False)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.READY,
        )
        for code in ("CAN-5L", "CAN-1L"):
            MarketplaceScan.objects.create(
                company=self.company, dispatch=dispatch, barcode_raw=code,
                item_code=code, quantity=Decimal("2"), scanned_by=self.user,
            )
        gi_spy = mock.MagicMock(return_value={"DocEntry": 1, "DocNum": "GI"})
        with override_settings(MARKETPLACE_SIMULATE_SAP=True), \
                mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_goods_issue", gi_spy):
            confirm_dispatch(dispatch, user=self.user)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.POSTED)
        gi_spy.assert_not_called()
        self.assertEqual(dispatch.sap_goods_issue_num, "")

    def test_mapping_matches_by_fsn(self):
        """A line resolves via its FSN — the primary key — even when the seller
        SKU has no mapping. Falls back to SKU only when FSN doesn't match."""
        from .models import MarketplaceOrder, MarketplaceOrderLine, SkuMapping, SkuType
        from .services.resolve_service import resolve_order
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="OD-FSN-1", buyer_name="X",
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="SELLER-SKU-XYZ", fsn="FSNABC123",
            ordered_quantity=Decimal("2"),
        )
        # Mapping keyed by FSN; its own marketplace_sku differs from the line's SKU.
        SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="ANY-LABEL", fsn="FSNABC123", sku_type=SkuType.RAW,
            fg_item_code="FG0000329",
        )
        res = resolve_order(order)
        self.assertEqual(res["unmapped_skus"], [])
        self.assertEqual({l["item_code"] for l in res["resolved_lines"]}, {"FG0000329"})
        self.assertEqual(res["resolved_lines"][0]["required_quantity"], Decimal("2"))

        # An order line whose FSN is unknown reports the FSN as unmapped.
        order2 = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="OD-FSN-2", buyer_name="X",
        )
        MarketplaceOrderLine.objects.create(
            order=order2, marketplace_sku="NOPE", fsn="FSN-UNKNOWN",
            ordered_quantity=Decimal("1"),
        )
        self.assertEqual(resolve_order(order2)["unmapped_skus"], ["FSN-UNKNOWN"])

    def test_cannot_pack_unissued_order(self):
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")  # issued? no — not issued yet
        with self.assertRaises(MarketplaceError) as ctx:
            packing_service.start_or_get(od1, user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_ISSUED")

    # ── Resilient delivery note (dispatch proceeds; failed post can be retried) ─
    def test_delivery_note_failure_dispatches_then_retry_succeeds(self):
        from .models import MarketplaceSapPostStatus
        from .services.confirm_service import retry_delivery_note
        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        self._pack_order(od1)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="EV-1L",
            item_code="EV-1L", quantity=Decimal("1"), scanned_by=self.user,
        )
        # SAP not simulated → the delivery-note post fails, but the order still dispatches.
        with override_settings(MARKETPLACE_SIMULATE_SAP=False):
            confirm_dispatch(dispatch, user=self.user)
        dispatch.refresh_from_db()
        od1.refresh_from_db()
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.CONFIRMED)
        self.assertEqual(od1.status, MarketplaceOrderStatus.DISPATCHED)
        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.FAILED)
        self.assertTrue(dispatch.sap_error)
        self.assertEqual(dispatch.sap_delivery_note_num, "")

        # Retry once SAP is reachable → posts successfully.
        with override_settings(MARKETPLACE_SIMULATE_SAP=True):
            retry_delivery_note(dispatch, user=self.user)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.POSTED)
        self.assertTrue(dispatch.sap_delivery_note_num.startswith("SIMDN-"))
        self.assertEqual(dispatch.sap_error, "")

    def test_goods_issue_failure_does_not_repost_delivery_note_on_retry(self):
        """If the Goods Issue fails AFTER the Delivery Note is created, a retry must
        reuse the existing DN (never create a second one → no double stock decrement)."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus
        from .services import sap_gateway
        from .services.confirm_service import retry_delivery_note
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")  # combo → FG (CAN-5L, CAN-1L) + PM
        self._pack_order(od2)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.READY,
        )
        for code in ("CAN-5L", "CAN-1L"):
            MarketplaceScan.objects.create(
                company=self.company, dispatch=dispatch, barcode_raw=code,
                item_code=code, quantity=Decimal("2"), scanned_by=self.user,
            )

        dn_spy = mock.MagicMock(
            side_effect=lambda **kw: {"DocEntry": 900000 + int(kw["ref"]), "DocNum": f"SIMDN-{kw['ref']}"}
        )
        gi_state = {"n": 0}

        def gi_side(**kw):
            gi_state["n"] += 1
            if gi_state["n"] == 1:
                raise RuntimeError("SAP Goods Issue temporarily down")
            return {"DocEntry": 111, "DocNum": "SIMGI-OK"}

        gi_spy = mock.MagicMock(side_effect=gi_side)

        with override_settings(MARKETPLACE_SIMULATE_SAP=True), \
                mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", dn_spy), \
                mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_goods_issue", gi_spy):
            confirm_dispatch(dispatch, user=self.user)
            dispatch.refresh_from_db()
            # DN succeeded and was persisted; GI failed → whole post flagged FAILED.
            self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.FAILED)
            self.assertIsNotNone(dispatch.sap_delivery_note_doc_entry)
            dn_entry = dispatch.sap_delivery_note_doc_entry

            retry_delivery_note(dispatch, user=self.user)
            dispatch.refresh_from_db()

        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.POSTED)
        self.assertEqual(dispatch.sap_delivery_note_doc_entry, dn_entry)  # same DN, not a new one
        self.assertEqual(dn_spy.call_count, 1)  # Delivery Note created exactly ONCE
        self.assertEqual(gi_spy.call_count, 2)  # GI attempted twice (fail, then success)
        self.assertEqual(dispatch.sap_goods_issue_num, "SIMGI-OK")
