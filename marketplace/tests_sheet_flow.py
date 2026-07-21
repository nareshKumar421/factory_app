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


class MarketplaceCompanyGuardTests(TestCase):
    """The marketplace module is enabled for exactly one company unit."""

    def _run_initial(self, code):
        from unittest import mock
        from marketplace.views import MpBaseView
        v = MpBaseView()
        with mock.patch.object(MpBaseView, "company", new_callable=mock.PropertyMock) as comp, \
                mock.patch("rest_framework.views.APIView.initial", return_value=None):
            comp.return_value = type("C", (), {"code": code})()
            v.initial(mock.Mock())

    @override_settings(MARKETPLACE_COMPANY_CODE="JIVO_MART")
    def test_blocks_other_company_and_allows_configured(self):
        self._run_initial("JIVO_MART")  # allowed → no raise
        with self.assertRaises(MarketplaceError) as ctx:
            self._run_initial("JIVO_OIL")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "WRONG_COMPANY")

    @override_settings(MARKETPLACE_COMPANY_CODE="")
    def test_blank_setting_allows_any_company(self):
        self._run_initial("JIVO_OIL")  # no restriction → no raise


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
        if not order.tracking_id:
            order.tracking_id = f"FMPP-{order.order_id}"
            order.save(update_fields=["tracking_id"])
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

    # ── Packing Summary (group by item, mark item groups complete) ────────────
    def test_packing_summary_groups_pending_orders_by_item(self):
        batch = self._ingest_main()
        self._issue_batch(batch)  # OD1..OD3 issued, not yet packed (OD4 cancelled)
        summary = packing_service.packing_summary(self.company, MarketplaceChannel.FLIPKART)
        counts = {g["item_code"]: g["order_count"] for g in summary["items"]}
        # OD1→EV-1L, OD2→combo(CAN-5L,CAN-1L), OD3→CAN-5L
        self.assertEqual(counts, {"EV-1L": 1, "CAN-5L": 2, "CAN-1L": 1})
        self.assertEqual(summary["total_orders"], 3)

    def test_complete_item_group_packs_single_item_orders_and_reaches_outward(self):
        from .models import MarketplaceOrder
        from .services.dispatch_gate import order_dispatch_ready
        batch = self._ingest_main()
        self._issue_batch(batch)

        # EV-1L is only OD1 (single FG line) → completing it packs exactly OD1.
        res = packing_service.complete_item_group(
            self.company, MarketplaceChannel.FLIPKART, item_code="EV-1L", user=self.user
        )
        self.assertEqual(res["completed_count"], 1)
        self.assertEqual(res["completed_order_ids"], ["OD1"])
        od1 = MarketplaceOrder.objects.get(company=self.company, order_id="OD1")
        self.assertTrue(order_dispatch_ready(od1))  # now reaches Outward

        # CAN-5L is on OD3 (single) and OD2 (combo, multi-FG) → OD3 packs, OD2 is
        # skipped because its other item (CAN-1L) isn't complete yet.
        res2 = packing_service.complete_item_group(
            self.company, MarketplaceChannel.FLIPKART, item_code="CAN-5L", user=self.user
        )
        self.assertEqual(res2["completed_order_ids"], ["OD3"])
        self.assertEqual(res2["skipped_order_ids"], ["OD2"])
        # OD1 already packed → no longer in the summary work-list.
        after = packing_service.packing_summary(self.company, MarketplaceChannel.FLIPKART)
        self.assertNotIn("EV-1L", {g["item_code"] for g in after["items"]})

    # ── Scan-first Outward / Inward (one Tracking ID scan = whole order) ───────
    def test_scan_dispatch_by_tracking_completes_order_and_dedupes(self):
        from .models import MarketplaceDispatchStatus
        from .services import scan_service
        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        self._pack_order(od1)  # packs + sets tracking_id FMPP-OD1

        dispatch, created, dup = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD1", user=self.user
        )
        self.assertTrue(created)
        self.assertFalse(dup)
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.READY)
        self.assertEqual(dispatch.scans.count(), 1)  # EV-1L line completed
        # Re-scan the same tracking ID → duplicate, no new dispatch/scan.
        d2, created2, dup2 = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD1", user=self.user
        )
        self.assertFalse(created2)
        self.assertTrue(dup2)
        self.assertEqual(d2.pk, dispatch.pk)

    def test_scan_dispatch_by_tracking_blocks_unpacked_and_unknown(self):
        from .services import scan_service
        from .services.errors import MarketplaceError
        batch = self._ingest_main()
        self._issue_batch(batch)
        od3 = batch.orders.get(order_id="OD3")
        od3.tracking_id = "FMPP-OD3"
        od3.save(update_fields=["tracking_id"])
        # Issued but NOT packed → blocked.
        with self.assertRaises(MarketplaceError) as ctx:
            scan_service.scan_dispatch_by_tracking(
                self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD3", user=self.user
            )
        self.assertEqual(ctx.exception.code, "NOT_PACKED")
        # Unknown tracking ID → not found.
        with self.assertRaises(MarketplaceError) as ctx2:
            scan_service.scan_dispatch_by_tracking(
                self.company, MarketplaceChannel.FLIPKART, barcode="NOPE-123", user=self.user
            )
        self.assertEqual(ctx2.exception.code, "NOT_FOUND")

    def test_scan_return_by_tracking_records_all_lines(self):
        from .models import MarketplaceReturnStatus
        from .services import scan_service
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")  # combo → 2 FG lines
        od2.tracking_id = "FMPP-OD2R"
        od2.save(update_fields=["tracking_id"])

        mp_return, created, dup = scan_service.scan_return_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD2R", user=self.user
        )
        self.assertTrue(created)
        self.assertFalse(dup)
        self.assertEqual(mp_return.status, MarketplaceReturnStatus.SCANNING)
        self.assertEqual(mp_return.scans.count(), 2)  # CAN-5L + CAN-1L
        # Re-scan → duplicate, no new scans.
        _r, _c, dup2 = scan_service.scan_return_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD2R", user=self.user
        )
        self.assertTrue(dup2)

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

    def test_skip_unmapped_orders_removes_them_and_unblocks_batch(self):
        """Skipping removes exactly the orders with a missing mapping; the rest of
        the batch resolves cleanly and can proceed to the warehouse."""
        from .models import MarketplaceOrder
        text = make_csv([
            row("OD1", "Extra Virgin 1L", 1, invoice="900"),          # mapped
            row("OD9", "Mystery SKU", 1, fsn="FSN-UNMAPPED"),         # unmapped
            row("OD10", "Ghost SKU", 2, fsn="FSN-ALSO-UNMAPPED"),    # unmapped
        ])
        batch = ingest(self.company, text=text, filename="x.csv", user=self.user)

        preview = batch_resolve_service.orders_with_unmapped_skus(batch)
        self.assertEqual({o["order_id"] for o in preview}, {"OD9", "OD10"})

        result = batch_resolve_service.skip_unmapped_orders(batch, user=self.user)
        self.assertEqual(result["removed_count"], 2)
        self.assertEqual(set(result["removed_order_ids"]), {"OD9", "OD10"})
        self.assertEqual(result["blocked_order_ids"], [])
        self.assertEqual(result["remaining_unmapped_skus"], [])

        # The unmapped orders are gone; the mapped one survives.
        self.assertFalse(
            MarketplaceOrder.objects.filter(company=self.company, order_id__in=["OD9", "OD10"]).exists()
        )
        self.assertTrue(MarketplaceOrder.objects.filter(company=self.company, order_id="OD1").exists())
        # Batch now resolves with no unmapped SKUs → issue request can be created.
        self.assertEqual(batch_resolve_service.build_stock_list(batch)["unmapped_skus"], [])
        req = issue_request_service.create_from_batch(batch, warehouse_code="WH1", user=self.user)
        self.assertEqual(req.status, MarketplaceIssueStatus.SENT)

    def test_skip_unmapped_keeps_orders_already_in_dispatch(self):
        """An unmapped order that somehow already has a dispatch is kept (its order
        FK is PROTECTed) and reported as blocked, not deleted."""
        from .models import MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder
        text = make_csv([row("OD9", "Mystery SKU", 1, fsn="FSN-UNMAPPED")])
        batch = ingest(self.company, text=text, filename="x.csv", user=self.user)
        order = MarketplaceOrder.objects.get(company=self.company, order_id="OD9")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.READY,
        )
        result = batch_resolve_service.skip_unmapped_orders(batch, user=self.user)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["blocked_order_ids"], ["OD9"])
        self.assertTrue(MarketplaceOrder.objects.filter(company=self.company, order_id="OD9").exists())

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
        self.assertEqual(ctx.exception.code, "NOT_READY")

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

    def _ready_dispatch(self, batch, order_id, item_code):
        order = batch.orders.get(order_id=order_id)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.READY,
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw=item_code,
            item_code=item_code, quantity=Decimal("1"), scanned_by=self.user,
        )
        return order, dispatch

    def test_defer_delivery_note_then_bulk_cut_single_request(self):
        """With defer on, confirm leaves dispatches PENDING; the bulk cut posts ONE
        Delivery Note (single SAP request) covering them all."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus
        from .services import delivery_note_service, sap_gateway, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )

        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        _od3, d3 = self._ready_dispatch(batch, "OD3", "CAN-5L")
        self._pack_order(_od1)
        self._pack_order(_od3)

        # Confirm both — deferred, so no DN yet.
        confirm_dispatch(d1, user=self.user)
        confirm_dispatch(d3, user=self.user)
        for d in (d1, d3):
            d.refresh_from_db()
            self.assertEqual(d.status, MarketplaceDispatchStatus.CONFIRMED)
            self.assertEqual(d.sap_post_status, MarketplaceSapPostStatus.PENDING)
            self.assertEqual(d.sap_delivery_note_num, "")

        # Summary previews both dispatches and the combined lines.
        summary = delivery_note_service.build_bulk_summary(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(summary["totals"]["dispatch_count"], 2)
        self.assertEqual({d["order_id"] for d in summary["dispatches"]}, {"OD1", "OD3"})
        self.assertEqual({l["item_code"] for l in summary["fg_lines"]}, {"EV-1L", "CAN-5L"})

        # Cut — assert exactly ONE create_delivery_note call for all items.
        dn_spy = mock.Mock(return_value={"DocEntry": 9001, "DocNum": "SIMDN-BULK"})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", dn_spy):
            result = delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )
        self.assertEqual(dn_spy.call_count, 1)  # a single request
        sent_lines = {l["item_code"] for l in dn_spy.call_args.kwargs["fg_lines"]}
        self.assertEqual(sent_lines, {"EV-1L", "CAN-5L"})
        self.assertEqual(result["dispatch_count"], 2)

        for d in (d1, d3):
            d.refresh_from_db()
            self.assertEqual(d.sap_post_status, MarketplaceSapPostStatus.POSTED)
            self.assertEqual(d.sap_delivery_note_num, "SIMDN-BULK")
        self.assertEqual(MarketplaceOrderBilling.objects.filter(order_id__in=["OD1", "OD3"]).count(), 2)

        # Nothing left awaiting.
        after = delivery_note_service.build_bulk_summary(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(after["totals"]["dispatch_count"], 0)

    def test_writer_detects_approval_draft_from_location_header(self):
        """A 404 with a Location header to /Drafts(N) is parsed as a pending-approval
        draft, not an error."""
        from sap_client.service_layer.delivery_note_writer import DeliveryNoteWriter

        class Resp:
            status_code = 404
            headers = {"Location": "https://sap:50000/b1s/v2/Drafts(52269)"}
        self.assertEqual(DeliveryNoteWriter._approval_draft_entry(Resp()), 52269)
        Resp.headers = {}
        self.assertIsNone(DeliveryNoteWriter._approval_draft_entry(Resp()))

    def test_cut_pending_approval_marks_awaiting_and_blocks_recut(self):
        """When SAP routes the DN into approval (returns a draft), dispatches go
        AWAITING_APPROVAL — not POSTED — and drop out of the awaiting list so a
        re-cut can't create a duplicate draft."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus
        from .services import delivery_note_service, sap_gateway, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        _od3, d3 = self._ready_dispatch(batch, "OD3", "CAN-5L")
        self._pack_order(_od1); self._pack_order(_od3)
        confirm_dispatch(d1, user=self.user); confirm_dispatch(d3, user=self.user)

        pending = mock.Mock(return_value={"DocEntry": None, "DocNum": "",
                                          "pending_approval": True, "draft_entry": 52269})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", pending):
            result = delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )
        self.assertTrue(result["pending_approval"])
        self.assertEqual(result["draft_entry"], 52269)
        for d in (d1, d3):
            d.refresh_from_db()
            self.assertEqual(d.sap_post_status, MarketplaceSapPostStatus.AWAITING_APPROVAL)
            self.assertEqual(d.sap_delivery_note_draft_entry, 52269)
            self.assertTrue(d.sap_dn_ref)
        # No billing yet, nothing POSTED.
        self.assertEqual(MarketplaceOrderBilling.objects.count(), 0)
        # Excluded from the awaiting list → re-cut finds nothing (no duplicate drafts).
        after = delivery_note_service.build_bulk_summary(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(after["totals"]["dispatch_count"], 0)
        with self.assertRaises(MarketplaceError) as ctx:
            delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )
        self.assertEqual(ctx.exception.code, "EMPTY")

    def test_reconcile_finalizes_approved_delivery_note(self):
        """Once the approval draft is approved (a real DN exists by NumAtCard),
        reconcile records it, writes billing, and marks the dispatches POSTED."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus
        from .services import delivery_note_service, sap_gateway, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        self._pack_order(_od1); confirm_dispatch(d1, user=self.user)

        pending = mock.Mock(return_value={"DocEntry": None, "DocNum": "",
                                          "pending_approval": True, "draft_entry": 900})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", pending):
            delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )

        # Approval granted → SAP now has the real DN under the same NumAtCard.
        found = mock.Mock(return_value={"DocEntry": 7777, "DocNum": "DN7777"})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "find_delivery_note_by_ref", found):
            res = delivery_note_service.reconcile_approved_delivery_notes(
                self.company, channel=MarketplaceChannel.FLIPKART, user=self.user
            )
        self.assertEqual(res["finalized"], ["OD1"])
        d1.refresh_from_db()
        self.assertEqual(d1.sap_post_status, MarketplaceSapPostStatus.POSTED)
        self.assertEqual(d1.sap_delivery_note_num, "DN7777")
        self.assertEqual(MarketplaceOrderBilling.objects.filter(order_id="OD1").count(), 1)

    def test_reconcile_marks_rejected_approval_failed(self):
        """A rejected approval flips the dispatch to FAILED so it can be re-cut."""
        from unittest import mock
        from .models import MarketplaceSapPostStatus
        from .services import delivery_note_service, sap_gateway, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        self._pack_order(_od1); confirm_dispatch(d1, user=self.user)
        pending = mock.Mock(return_value={"DocEntry": None, "DocNum": "",
                                          "pending_approval": True, "draft_entry": 901})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", pending):
            delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "find_delivery_note_by_ref",
                               mock.Mock(return_value=None)), \
             mock.patch.object(sap_gateway.MarketplaceSapGateway, "draft_rejected",
                               mock.Mock(return_value=True)):
            res = delivery_note_service.reconcile_approved_delivery_notes(
                self.company, channel=MarketplaceChannel.FLIPKART, user=self.user
            )
        self.assertEqual(res["rejected"], ["OD1"])
        d1.refresh_from_db()
        self.assertEqual(d1.sap_post_status, MarketplaceSapPostStatus.FAILED)

    def test_multi_item_order_scans_per_tracking_id(self):
        """A multi-item order whose items carry different tracking IDs completes one
        item per scan and only becomes READY once every tracking ID is scanned."""
        from .models import MarketplaceOrder, MarketplaceOrderLine, MarketplaceDispatchStatus
        from .services import scan_service
        # A direct (non-sheet) order with two items → two distinct tracking IDs.
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODMULTI", buyer_name="X",
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Extra Virgin 1L", ordered_quantity=Decimal("1"),
            fsn="F-EV", tracking_id="TRK-A",
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Canola 5L", ordered_quantity=Decimal("1"),
            fsn="F-CAN", tracking_id="TRK-B",
        )

        # First tracking ID → only that item scanned; order NOT yet ready.
        d, created, dup = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="TRK-A", user=self.user)
        self.assertTrue(created)
        self.assertEqual(d.status, MarketplaceDispatchStatus.SCANNING)
        self.assertEqual({s.item_code for s in d.scans.all()}, {"EV-1L"})

        # Re-scanning the same tracking ID adds nothing → duplicate.
        _d, _c, dup2 = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="TRK-A", user=self.user)
        self.assertTrue(dup2)
        d.refresh_from_db(); self.assertEqual(d.scans.count(), 1)

        # Second tracking ID completes the order → READY.
        d2, _c2, _dup3 = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="TRK-B", user=self.user)
        self.assertEqual(d2.status, MarketplaceDispatchStatus.READY)
        self.assertEqual({s.item_code for s in d2.scans.all()}, {"EV-1L", "CAN-5L"})

    def test_cut_uses_selected_warehouse_else_default(self):
        """The summary/cut post against a chosen warehouse; default when unspecified."""
        from unittest import mock
        from .models import MarketplaceWarehouse
        from .services import delivery_note_service, sap_gateway, settings_service

        # Second warehouse for the channel, marked default.
        wh2 = MarketplaceWarehouse.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, name="Alt",
            sap_warehouse_code="WH2", sap_customer_card_code="C-ALT", is_default=True,
        )
        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        self._pack_order(_od1); confirm_dispatch(d1, user=self.user)

        # Summary defaults to the is_default warehouse (WH2) and lists both options.
        s = delivery_note_service.build_bulk_summary(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(s["warehouse_code"], "WH2")
        self.assertEqual(s["warehouse_id"], wh2.id)
        self.assertEqual({w["sap_warehouse_code"] for w in s["warehouses"]}, {"WH1", "WH2"})

        # Explicitly selecting WH1 overrides the default.
        s1 = delivery_note_service.build_bulk_summary(
            self.company, MarketplaceChannel.FLIPKART, warehouse_id=self._wh1_id())
        self.assertEqual(s1["warehouse_code"], "WH1")

        # Cut with the selected warehouse posts against its CardCode.
        dn_spy = mock.Mock(return_value={"DocEntry": 1, "DocNum": "DN1"})
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", dn_spy):
            delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, warehouse_id=wh2.id, user=self.user)
        self.assertEqual(dn_spy.call_args.kwargs["card_code"], "C-ALT")
        self.assertEqual(dn_spy.call_args.kwargs["warehouse_code"], "WH2")

    def _wh1_id(self):
        from .models import MarketplaceWarehouse
        return MarketplaceWarehouse.objects.get(
            company=self.company, sap_warehouse_code="WH1").id

    def test_bulk_cut_builds_real_sap_payload_from_merged_lines(self):
        """Regression: the bulk cut must build a real SAP Delivery Note payload from
        merged lines. The gateway reads ``required_quantity`` off each line, so a
        merged line missing that key 500s the whole cut in production (simulate off).
        Mock only the low-level SAP client so ``_line`` actually runs on merged lines.
        """
        from unittest import mock
        from decimal import Decimal as D
        from .models import MarketplaceSapPostStatus
        from .services import delivery_note_service, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )

        # Two orders that resolve to the SAME finished good so the merge sums them —
        # exercising both the insert and the accumulate branches of _merge_lines.
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        _od2, d2 = self._ready_dispatch(batch, "OD2", "EV-1L")
        self._pack_order(_od1)
        self._pack_order(_od2)
        confirm_dispatch(d1, user=self.user)
        confirm_dispatch(d2, user=self.user)

        # Expected merged EV-1L quantity, straight from the preview the UI shows.
        summary = delivery_note_service.build_bulk_summary(self.company, MarketplaceChannel.FLIPKART)
        expected_ev = next(D(l["quantity"]) for l in summary["fg_lines"] if l["item_code"] == "EV-1L")

        fake_client = mock.MagicMock()
        fake_client.create_delivery_note.return_value = {"DocEntry": 7001, "DocNum": "DN7001"}
        fake_client.create_goods_issue.return_value = {"DocEntry": 6001, "DocNum": "GI6001"}

        with override_settings(MARKETPLACE_SIMULATE_SAP=False), \
                mock.patch("sap_client.client.SAPClient", return_value=fake_client):
            result = delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user
            )

        # One real SAP request whose payload carries the merged quantity — building
        # this line raised KeyError('required_quantity') → HTTP 500 before the fix.
        self.assertEqual(fake_client.create_delivery_note.call_count, 1)
        payload = fake_client.create_delivery_note.call_args.args[0]
        ev_line = next(l for l in payload["DocumentLines"] if l["ItemCode"] == "EV-1L")
        self.assertEqual(D(str(ev_line["Quantity"])), expected_ev)
        self.assertEqual(result["dispatch_count"], 2)
        for d in (d1, d2):
            d.refresh_from_db()
            self.assertEqual(d.sap_post_status, MarketplaceSapPostStatus.POSTED)
            self.assertEqual(d.sap_delivery_note_num, "DN7001")

    def test_bulk_cut_surfaces_sap_error_instead_of_500(self):
        """When SAP rejects the delivery note, the bulk cut must raise a
        MarketplaceError carrying SAP's own message (so the client shows the real
        reason) rather than letting the raw SAPValidationError become an HTTP 500.
        """
        from unittest import mock
        from sap_client.exceptions import SAPValidationError
        from .services import delivery_note_service, settings_service
        from .services.errors import MarketplaceError

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        self._pack_order(_od1)
        confirm_dispatch(d1, user=self.user)

        fake_client = mock.MagicMock()
        fake_client.create_delivery_note.side_effect = SAPValidationError(
            "Item FG00001 is not valid for CardCode BH-Ec"
        )

        with override_settings(MARKETPLACE_SIMULATE_SAP=False), \
                mock.patch("sap_client.client.SAPClient", return_value=fake_client):
            with self.assertRaises(MarketplaceError) as ctx:
                delivery_note_service.cut_bulk_delivery_note(
                    self.company, MarketplaceChannel.FLIPKART, user=self.user
                )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("SAP rejected", ctx.exception.message)
        self.assertIn("Item FG00001 is not valid", ctx.exception.message)
        # Nothing was persisted — the dispatch is still awaiting, retry-safe.
        d1.refresh_from_db()
        self.assertEqual(d1.sap_delivery_note_num, "")

    # ── Packing ───────────────────────────────────────────────────────────────
    def test_packing_generates_barcodes_and_gates_dispatch(self):
        from .models import MarketplacePackingStatus
        from .services.dispatch_gate import order_is_packed
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")  # Canola 5+1L ×2 → CAN-5L, CAN-1L (+PM)
        od2.tracking_id = "FMPP-OD2"
        od2.save(update_fields=["tracking_id"])

        ready = packing_service.orders_ready_to_pack(self.company, MarketplaceChannel.FLIPKART)
        self.assertIn("OD2", [o.order_id for o in ready])
        self.assertFalse(order_is_packed(od2))

        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        self.assertEqual({b.item_code for b in bcs}, {"CAN-5L", "CAN-1L"})  # FG only, no PM
        # Every label carries the Flipkart Tracking ID — no self-minted barcodes.
        self.assertTrue(all(b.barcode == od2.tracking_id for b in bcs))
        packing.refresh_from_db()
        self.assertEqual(packing.status, MarketplacePackingStatus.PACKING)
        self.assertFalse(order_is_packed(od2))  # not completed yet

        # idempotent
        self.assertEqual(len(packing_service.generate_barcodes(packing, user=self.user)), len(bcs))

        packing_service.complete(packing, user=self.user)
        self.assertTrue(order_is_packed(od2))
        ready2 = packing_service.orders_ready_to_pack(self.company, MarketplaceChannel.FLIPKART)
        self.assertNotIn("OD2", [o.order_id for o in ready2])

    def test_skip_packing_setting_makes_issued_order_dispatch_ready(self):
        """With skip_packing ON, an issued (unpacked) order is dispatch-ready;
        with it OFF the order must be PACKED first."""
        from .services import settings_service
        from .services.dispatch_gate import order_dispatch_ready
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")

        # Default (skip off): issued but not packed → not ready.
        self.assertFalse(settings_service.is_skip_packing(self.company, MarketplaceChannel.FLIPKART))
        self.assertFalse(order_dispatch_ready(od2))

        # Turn skip_packing on → issued is enough.
        settings_service.set_skip_packing(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )
        self.assertTrue(settings_service.is_skip_packing(self.company, MarketplaceChannel.FLIPKART))
        self.assertTrue(order_dispatch_ready(od2))

        # Turning it back off restores the packed requirement.
        settings_service.set_skip_packing(
            self.company, MarketplaceChannel.FLIPKART, False, user=self.user
        )
        self.assertFalse(order_dispatch_ready(od2))

    def test_packing_queue_keeps_packed_orders_for_reprint(self):
        """The packing-screen queue keeps packed orders (so labels can be reprinted),
        unlike ``orders_ready_to_pack`` which drops them once packed."""
        from .models import MarketplacePackingStatus
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")
        od2.tracking_id = "FMPP-OD2"
        od2.save(update_fields=["tracking_id"])

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
        od2.tracking_id = "FMPP-OD2"
        od2.save(update_fields=["tracking_id"])
        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)

        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.DRAFT,
        )
        # Scanning the Tracking ID resolves to a packed order line + its qty.
        first = bcs[0]
        scan, _created, _dup = record_dispatch_scan(dispatch, barcode_raw=od2.tracking_id, user=self.user)
        self.assertEqual(scan.item_code, first.item_code)
        self.assertEqual(scan.quantity, Decimal(first.quantity))  # resolved to the order-line qty
        prog = {r["item_code"]: r["status"] for r in dispatch_progress(dispatch)}
        self.assertEqual(prog[first.item_code], "COMPLETE")

    def test_return_scan_resolves_pack_barcode(self):
        """A returned item carrying its Tracking ID label resolves to the order line,
        just like at Outward (regression: previously raised ITEM_NOT_ON_ORDER)."""
        from .models import MarketplaceReturn, MarketplaceReturnStatus
        from .services.scan_service import record_return_scan, return_progress
        batch = self._ingest_main()
        self._issue_batch(batch)
        od2 = batch.orders.get(order_id="OD2")
        od2.tracking_id = "FMPP-OD2"
        od2.save(update_fields=["tracking_id"])
        packing = packing_service.start_or_get(od2, user=self.user)
        bcs = packing_service.generate_barcodes(packing, user=self.user)
        packing_service.complete(packing, user=self.user)

        mp_return = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceReturnStatus.DRAFT,
        )
        first = bcs[0]
        scan, _created, _dup = record_return_scan(mp_return, barcode_raw=od2.tracking_id, user=self.user)
        self.assertEqual(scan.item_code, first.item_code)
        self.assertEqual(scan.quantity, Decimal(first.quantity))  # resolved to the order-line qty
        prog = {r["item_code"]: r["status"] for r in return_progress(mp_return)}
        self.assertEqual(prog[first.item_code], "COMPLETE")

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
            branch_id=1,
        )
        payload = fake_client.create_delivery_note.call_args.args[0]
        self.assertEqual(payload["Series"], 4)
        self.assertEqual(payload["CardCode"], "C-FLIP")
        self.assertEqual(payload["NumAtCard"], "OD9")
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 1)  # SAP GST branch
        self.assertEqual(payload["DocumentLines"][0]["VatGroup"], "GST18")
        self.assertEqual(payload["DocumentLines"][0]["WarehouseCode"], "WH1")

    def test_delivery_note_omits_branch_when_unset(self):
        """No BPLId key is sent when the warehouse master has no branch configured."""
        from datetime import date
        from unittest import mock
        from .services.sap_gateway import MarketplaceSapGateway
        gw = MarketplaceSapGateway("JIVO_MART")
        gw.simulate = False
        fake_client = mock.MagicMock()
        fake_client.create_delivery_note.return_value = {"DocEntry": 6, "DocNum": "DN6"}
        gw._client = fake_client
        gw.create_delivery_note(
            ref=2, card_code="C-FLIP", warehouse_code="WH1",
            fg_lines=[{"item_code": "X", "required_quantity": Decimal("1"), "warehouse_code": ""}],
            doc_date=date(2026, 7, 13), branch_id=None,
        )
        self.assertNotIn("BPL_IDAssignedToInvoice", fake_client.create_delivery_note.call_args.args[0])

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


class PartitionByStockTests(TestCase):
    """The bulk delivery note is one all-or-nothing document, so dispatches the
    warehouse can't fulfil must be held out rather than sinking the whole post."""

    def _item(self, dispatch_id, order_id, fg):
        class _O:
            pass
        o = _O(); o.order_id = order_id
        d = _O(); d.id = dispatch_id; d.order = o
        return {"dispatch": d, "fg": [{"item_code": c, "required_quantity": Decimal(str(q))} for c, q in fg]}

    def test_holds_short_dispatches_and_shares_stock(self):
        from unittest import mock
        from .services import delivery_note_service as dns

        includable = [
            self._item(1, "A", [("FG1", 3)]),   # ok
            self._item(2, "B", [("FG1", 3)]),    # ok (FG1 now exhausted: 6 total)
            self._item(3, "C", [("FG1", 1)]),    # held — FG1 depleted
            self._item(4, "D", [("FG2", 2)]),    # held — FG2 has none
        ]
        with mock.patch.object(dns, "_available_onhand", return_value={"FG1": Decimal("6")}):
            ok, held = dns._partition_by_stock("JIVO_MART", includable, "DL-EC")
        self.assertEqual([i["dispatch"].id for i in ok], [1, 2])
        self.assertEqual(sorted(h["dispatch_id"] for h in held), [3, 4])
        self.assertTrue(all("Insufficient stock" in h["reason"] for h in held))

    def test_unknown_stock_holds_nothing(self):
        from unittest import mock
        from .services import delivery_note_service as dns

        includable = [self._item(1, "A", [("FG1", 99)])]
        with mock.patch.object(dns, "_available_onhand", return_value={}):
            ok, held = dns._partition_by_stock("JIVO_MART", includable, "DL-EC")
        self.assertEqual(len(ok), 1)
        self.assertEqual(held, [])

    def test_stock_shortfall_sums_demand_minus_onhand(self):
        from .services import delivery_note_service as dns

        def item(dispatch_id, fg):
            return {"dispatch": None, "fg": [
                {"item_code": c, "item_name": f"Name {c}", "uom": "EA",
                 "required_quantity": Decimal(str(q))}
                for c, q in fg
            ]}

        includable = [
            item(1, [("FG1", 3)]),           # FG1 total demand = 5
            item(2, [("FG1", 2), ("FG2", 4)]),  # FG2 total demand = 4
            item(3, [("FG3", 1)]),           # FG3 fully in stock — not short
        ]
        onhand = {"FG1": Decimal("4"), "FG2": Decimal("0"), "FG3": Decimal("10")}
        rows = dns._stock_shortfall(includable, "DL-EC", onhand)

        # FG3 is covered, so only FG1 (5-4=1) and FG2 (4-0=4) are short, sorted by code.
        self.assertEqual([r["item_code"] for r in rows], ["FG1", "FG2"])
        fg1, fg2 = rows
        self.assertEqual(fg1["required_quantity"], "5")
        self.assertEqual(fg1["available_quantity"], "4")
        self.assertEqual(fg1["shortfall_quantity"], "1")
        self.assertEqual(fg2["shortfall_quantity"], "4")
        self.assertEqual(fg2["uom"], "EA")

    def test_stock_shortfall_empty_when_onhand_unknown(self):
        from .services import delivery_note_service as dns

        includable = [{"dispatch": None, "fg": [
            {"item_code": "FG1", "item_name": "X", "uom": "EA", "required_quantity": Decimal("9")}]}]
        self.assertEqual(dns._stock_shortfall(includable, "DL-EC", {}), [])


class DispatchBoardTests(SheetFlowTests):
    """Sheet-wise Outward board: per-order tracking scan state + sheet insights."""

    def test_sheet_board_reflects_scan_progress_and_insights(self):
        from .services import dispatch_board_service as board, scan_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        # Pack + set a tracking id per non-cancelled order (OD1/OD2/OD3).
        for oid in ("OD1", "OD2", "OD3"):
            self._pack_order(batch.orders.get(order_id=oid))

        ch = MarketplaceChannel.FLIPKART
        # Sheet appears with all 3 pending (OD4 cancelled is excluded).
        sheets = board.list_sheets(self.company, ch)["sheets"]
        self.assertEqual(len(sheets), 1)
        ins = sheets[0]["insights"]
        self.assertEqual(ins["total_orders"], 3)
        self.assertEqual(ins["completed_orders"], 0)
        self.assertEqual(ins["pending_orders"], 3)
        self.assertEqual(ins["tracking_total"], 3)
        self.assertEqual(ins["tracking_scanned"], 0)

        # Scan OD1's tracking id → it completes.
        scan_service.scan_dispatch_by_tracking(self.company, ch, barcode="FMPP-OD1", user=self.user)
        bd = board.sheet_board(self.company, ch, batch.id)
        self.assertEqual(bd["insights"]["completed_orders"], 1)
        self.assertEqual(bd["insights"]["tracking_scanned"], 1)
        self.assertEqual(bd["insights"]["progress_pct"], 33)
        by_id = {o["order_id"]: o for o in bd["orders"]}
        self.assertEqual(by_id["OD1"]["status"], "SCANNED")
        self.assertEqual(by_id["OD1"]["tracking_scanned"], 1)
        self.assertTrue(by_id["OD1"]["items"][0]["scanned"])
        self.assertEqual(by_id["OD1"]["items"][0]["tracking_id"], "FMPP-OD1")
        self.assertEqual(by_id["OD2"]["status"], "PENDING")
        self.assertFalse(by_id["OD2"]["items"][0]["scanned"])

    def test_board_unknown_sheet_404s(self):
        from .services import dispatch_board_service as board
        from .services.errors import MarketplaceError
        with self.assertRaises(MarketplaceError):
            board.sheet_board(self.company, MarketplaceChannel.FLIPKART, 999999)


class VariantChoiceTests(TestCase):
    """One FSN → several SAP items: default resolution + per-order override."""

    def setUp(self):
        from .models import MarketplaceOrder
        self.MarketplaceOrder = MarketplaceOrder
        self.company = Company.objects.create(name="VarCo", code="VAR")
        User = get_user_model()
        self.user = User.objects.create(email="v@x.com", full_name="V", employee_code="V1", is_active=True)
        ch = MarketplaceChannel.FLIPKART
        self.mapping = SkuMapping.objects.create(
            company=self.company, channel=ch, marketplace_sku="SUN-1L-2",
            fsn="FSNSUN12", sku_type=SkuType.RAW, fg_item_code="FG0000081", fg_item_name="Sunflower",
        )
        from .models import SkuMappingOption
        # Two SAP items for the same FSN: SANO (default) and plain sunflower.
        SkuMappingOption.objects.create(mapping=self.mapping, label="SANO", sku_type=SkuType.RAW,
                                        fg_item_code="FG0000138", fg_item_name="SANO Sunflower", is_default=True)
        self.alt = SkuMappingOption.objects.create(mapping=self.mapping, label="Plain", sku_type=SkuType.RAW,
                                        fg_item_code="FG0000081", fg_item_name="Sunflower", is_default=False)
        self.order = self.MarketplaceOrder.objects.create(company=self.company, channel=ch, order_id="ODV1")
        self.line = self.order.lines.create(marketplace_sku="SUN-1L-2", fsn="FSNSUN12", ordered_quantity=Decimal("2"))

    def _fg(self):
        from .services.resolve_service import resolve_order, fg_lines
        return {l["item_code"]: Decimal(l["required_quantity"]) for l in fg_lines(resolve_order(self.order)["resolved_lines"])}

    def test_default_option_used_when_no_pick(self):
        self.assertEqual(self._fg(), {"FG0000138": Decimal("2")})  # default = SANO

    def test_override_ships_chosen_item(self):
        from .services import variant_service
        variant_service.set_line_option(self.company, line_id=self.line.id, option_id=self.alt.id, user=self.user)
        self.assertEqual(self._fg(), {"FG0000081": Decimal("2")})  # now plain
        # clear → back to default
        variant_service.set_line_option(self.company, line_id=self.line.id, option_id=None, user=self.user)
        self.assertEqual(self._fg(), {"FG0000138": Decimal("2")})

    def test_order_variants_lists_choices(self):
        from .services import variant_service
        v = variant_service.order_variants(self.order, choosable_only=True)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0]["has_choice"])
        self.assertEqual(len(v[0]["options"]), 2)
        self.assertEqual(v[0]["chosen_option_id"], self.mapping.options.get(is_default=True).id)

    def test_reject_option_from_other_mapping(self):
        from .services import variant_service
        from .services.errors import MarketplaceError
        other = SkuMapping.objects.create(company=self.company, channel=MarketplaceChannel.FLIPKART,
                                          marketplace_sku="OTHER", fsn="FSNOTHER", sku_type=SkuType.RAW, fg_item_code="FG0000005")
        from .models import SkuMappingOption
        bad = SkuMappingOption.objects.create(mapping=other, fg_item_code="FG0000005", is_default=True)
        with self.assertRaises(MarketplaceError):
            variant_service.set_line_option(self.company, line_id=self.line.id, option_id=bad.id, user=self.user)


class ComboComponentAlternativeTests(TestCase):
    """A combo slot can be filled by several interchangeable SAP items; the
    operator's pick per order drives what the delivery note deducts."""

    def setUp(self):
        from .models import ComboComponentOption, MarketplaceOrder
        self.company = Company.objects.create(name="ComboCo", code="CMB")
        ch = MarketplaceChannel.FLIPKART
        combo = ComboDefinition.objects.create(
            company=self.company, channel=ch, code="C1", name="Mustard 5L+1L")
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG,
            item_code="FG-5L", item_name="Mustard 5L", quantity=Decimal("1"))
        self.slot = ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG,
            item_code="FG-1L", item_name="Mustard 1L", quantity=Decimal("1"))
        # The 1L slot can ship as either item.
        ComboComponentOption.objects.create(
            component=self.slot, item_code="FG-1L", item_name="Mustard 1L", is_default=True)
        self.alt = ComboComponentOption.objects.create(
            component=self.slot, item_code="FG-1L-ALT", item_name="Mustard 1L Round", is_default=False)
        SkuMapping.objects.create(
            company=self.company, channel=ch, marketplace_sku="CS1", fsn="CFSN1",
            sku_type=SkuType.COMBO, combo=combo)
        self.order = MarketplaceOrder.objects.create(
            company=self.company, channel=ch, order_id="CORD1")
        self.line = self.order.lines.create(
            marketplace_sku="CS1", fsn="CFSN1", ordered_quantity=Decimal("2"))

    def _fg(self):
        from .services.resolve_service import resolve_order, fg_lines
        return {l["item_code"]: Decimal(l["required_quantity"])
                for l in fg_lines(resolve_order(self.order)["resolved_lines"])}

    def test_component_default_used_when_no_pick(self):
        self.assertEqual(self._fg(), {"FG-5L": Decimal("2"), "FG-1L": Decimal("2")})

    def test_picking_alternative_changes_what_ships(self):
        from .services import variant_service
        variant_service.set_component_option(
            self.company, line_id=self.line.id, component_id=self.slot.id, option_id=self.alt.id)
        self.line.refresh_from_db()
        self.assertEqual(self._fg(), {"FG-5L": Decimal("2"), "FG-1L-ALT": Decimal("2")})
        # clearing reverts to the component default
        variant_service.set_component_option(
            self.company, line_id=self.line.id, component_id=self.slot.id, option_id=None)
        self.line.refresh_from_db()
        self.assertEqual(self._fg(), {"FG-5L": Decimal("2"), "FG-1L": Decimal("2")})

    def test_variants_expose_only_slots_with_alternatives(self):
        from .services import variant_service
        v = variant_service.order_variants(self.order, choosable_only=True)
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0]["has_choice"])
        # only the 1L slot has options; the 5L slot has none
        self.assertEqual([c["component_id"] for c in v[0]["components"]], [self.slot.id])
        self.assertEqual(len(v[0]["components"][0]["options"]), 2)

    def test_reject_option_from_another_component(self):
        from .models import ComboComponentOption
        from .services import variant_service
        from .services.errors import MarketplaceError
        other = ComboComponent.objects.create(
            combo=self.slot.combo, component_type=ComboComponentType.FG,
            item_code="X", quantity=Decimal("1"))
        bad = ComboComponentOption.objects.create(component=other, item_code="X", is_default=True)
        with self.assertRaises(MarketplaceError):
            variant_service.set_component_option(
                self.company, line_id=self.line.id, component_id=self.slot.id, option_id=bad.id)


class ShipToSplitTests(SheetFlowTests):
    """Split the bulk delivery note by ship-to state → SAP ship-to address (place of
    supply). Branch/warehouse stay the same; only ShipToCode differs per group."""

    def test_resolve_shipto_maps_state_else_default(self):
        from types import SimpleNamespace
        from .services import delivery_note_service as dns
        wh = SimpleNamespace(shipto_by_state={"Delhi": "SHIP-DL", "*": "SHIP-HR"})
        self.assertEqual(dns._shipto_for_state("Delhi", wh), "SHIP-DL")
        self.assertEqual(dns._shipto_for_state("Maharashtra", wh), "SHIP-HR")
        self.assertEqual(dns._shipto_for_state("", wh), "SHIP-HR")
        # No map → "" → SAP uses the customer default (pre-split behaviour).
        self.assertEqual(dns._shipto_for_state("Delhi", SimpleNamespace(shipto_by_state={})), "")

    def test_cut_splits_delivery_note_by_ship_to_state(self):
        from unittest import mock
        from .models import MarketplaceOrder, MarketplaceWarehouse
        from .services import delivery_note_service, sap_gateway, settings_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user)

        wh = MarketplaceWarehouse.objects.get(company=self.company, sap_warehouse_code="WH1")
        wh.shipto_by_state = {"Delhi": "FLIPKART B2C DELHI", "*": "FLIPKART B2C HARYANA"}
        wh.save()

        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        _od3, d3 = self._ready_dispatch(batch, "OD3", "CAN-5L")
        self._pack_order(_od1); self._pack_order(_od3)
        MarketplaceOrder.objects.filter(order_id="OD1").update(state="Delhi")
        MarketplaceOrder.objects.filter(order_id="OD3").update(state="Maharashtra")
        confirm_dispatch(d1, user=self.user)
        confirm_dispatch(d3, user=self.user)

        calls = []
        def fake_dn(**kw):
            calls.append(kw)
            return {"DocEntry": 9000 + len(calls), "DocNum": f"DN-{len(calls)}"}
        with mock.patch.object(sap_gateway.MarketplaceSapGateway, "create_delivery_note", side_effect=fake_dn):
            result = delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART, user=self.user)

        self.assertEqual(len(result["groups"]), 2)
        shipto = {kw["ship_to_code"] for kw in calls}
        self.assertEqual(shipto, {"FLIPKART B2C DELHI", "FLIPKART B2C HARYANA"})
        # Delhi order -> Delhi address; Maharashtra order -> Haryana (default) address.
        by_ship = {kw["ship_to_code"]: {l["item_code"] for l in kw["fg_lines"]} for kw in calls}
        self.assertEqual(by_ship["FLIPKART B2C DELHI"], {"EV-1L"})
        self.assertEqual(by_ship["FLIPKART B2C HARYANA"], {"CAN-5L"})

    def test_gateway_sets_shiptocode_in_payload(self):
        import datetime
        from decimal import Decimal
        from unittest import mock
        from .services.sap_gateway import MarketplaceSapGateway
        gw = MarketplaceSapGateway("TST"); gw.simulate = False
        gw._client = mock.Mock()   # bypass the lazy `client` property (no live SAP)
        gw._client.create_delivery_note.return_value = {"DocEntry": 1, "DocNum": "X"}
        with mock.patch.object(gw, "_batch_stock", return_value={}), \
             mock.patch.object(gw, "_cost_centers", return_value={}):
            gw.create_delivery_note(
                ref=1, card_code="C", warehouse_code="WH1",
                fg_lines=[{"item_code": "A", "item_name": "A", "uom": "", "warehouse_code": "WH1",
                           "required_quantity": Decimal("1")}],
                doc_date=datetime.date(2026, 7, 21),
                ship_to_code="FLIPKART B2C HARYANA")
        payload = gw._client.create_delivery_note.call_args.args[0]
        self.assertEqual(payload["ShipToCode"], "FLIPKART B2C HARYANA")
        self.assertEqual(payload["PayToCode"], "FLIPKART B2C HARYANA")
