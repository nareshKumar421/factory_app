"""Step-by-step tests for the Flipkart sheet-driven flow.

Covers each stage of MARKETPLACE_FLIPKART_SHEET_FLOW.md at the service layer:
import → stock list (combo explosion) → unmapped gate → warehouse issue request
(partial approve / issue / receive) → issuance export → cancellation guard →
confirm (pricing).
"""
import csv
import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

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
        # Cancellation is tracked by is_cancelled; status stays OPEN so a later
        # re-approval sheet recovers the order cleanly.
        self.assertEqual(od4.status, MarketplaceOrderStatus.OPEN)

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

    def test_scan_unmapped_sku_reports_unmapped_not_already_scanned(self):
        """Scanning a tracking whose SKU has no mapping raises a clear UNMAPPED error
        — not the misleading 'already scanned' (duplicate) an empty scan would give."""
        from .services import scan_service, settings_service
        from .services.errors import MarketplaceError
        settings_service.set_skip_packing(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user)
        batch = ingest(
            self.company,
            text=self._csv_with_tracking("ODUNMAP", "TRK-UNMAP", sku="No-Such-SKU", item_id="'901"),
            filename="u.csv", user=self.user)
        self.assertIn("ODUNMAP", [o.order_id for o in batch.orders.all()])
        with self.assertRaises(MarketplaceError) as ctx:
            scan_service.scan_dispatch_by_tracking(
                self.company, MarketplaceChannel.FLIPKART, barcode="TRK-UNMAP", user=self.user)
        self.assertEqual(ctx.exception.code, "UNMAPPED")

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
        # A return is only allowed against a dispatched order.
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )

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

    @override_settings(MARKETPLACE_SIMULATE_SAP=True)
    def test_confirm_blocked_without_scan(self):
        """An order that was never scanned in Outward cannot be confirmed; a
        supervisor can still force it with override_deviation."""
        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        self._pack_order(od1)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_SCANNED")
        # Override lets a supervisor push it through anyway.
        confirm_dispatch(dispatch, user=self.user, override_deviation=True)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.CONFIRMED)

    def test_reimport_keeps_dispatched_order_in_original_sheet(self):
        """Re-uploading an overlapping CSV must NOT drag an already-dispatched order
        into the new sheet (which made it look done without scanning)."""
        from .models import MarketplaceOrder

        batch1 = self._ingest_main()
        od1 = batch1.orders.get(order_id="OD1")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        batch2 = ingest(
            self.company,
            text=make_csv([row("OD1", "Extra Virgin 1L", 1), row("ODNEW", "Canola 5L", 1)]),
            filename="again.csv", user=self.user,
        )
        od1.refresh_from_db()
        self.assertEqual(od1.import_batch_id, batch1.id)   # stayed put, not moved
        self.assertEqual(batch2.summary["dispatched_skipped"], 1)
        self.assertTrue(
            MarketplaceOrder.objects.filter(company=self.company, order_id="ODNEW").exists()
        )

    def test_carried_over_section_lists_skip_and_insights_unchanged(self):
        """A dispatched-skipped order appears in the sheet's carried_over section
        (linked to where it lives), and the sheet's own insight counts do NOT move."""
        from .services import dispatch_board_service as board

        batch1 = self._ingest_main()
        od1 = batch1.orders.get(order_id="OD1")
        d = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        batch2 = ingest(
            self.company,
            text=make_csv([row("OD1", "Extra Virgin 1L", 1), row("ODNEW", "Canola 5L", 1)]),
            filename="again.csv", user=self.user,
        )
        bd = board.sheet_board(self.company, MarketplaceChannel.FLIPKART, batch2.id)

        co = bd["carried_over"]
        self.assertEqual(len(co), 1)
        self.assertEqual(co[0]["order_id"], "OD1")
        self.assertEqual(co[0]["reason"], "DISPATCHED")
        self.assertEqual(co[0]["kept_on_batch_id"], batch1.id)
        self.assertEqual(co[0]["kept_on_filename"], batch1.filename)
        self.assertEqual(co[0]["dispatch_id"], d.id)
        self.assertEqual(co[0]["dispatch_status"], "CONFIRMED")

        # Sheet-2 insights count ONLY ODNEW — carried-over is informational.
        self.assertEqual([o["order_id"] for o in bd["orders"]], ["ODNEW"])
        self.assertEqual(bd["insights"]["total_orders"], 1)

        # Row arithmetic reconciles for the sheet.
        s = batch2.summary
        self.assertEqual(s["dispatched_skipped"], 1)
        self.assertEqual(s["skipped_order_rows"], 1)  # OD1 = 1 CSV row
        self.assertEqual(
            batch2.row_count,
            s["lines"] + s["blank_sku_skipped"] + s["skipped_order_rows"] + s["skipped"],
        )

        # Sheet-card badge counts (one query, no per-order fan-out).
        sheets = {x["id"]: x for x in
                  board.list_sheets(self.company, MarketplaceChannel.FLIPKART)["sheets"]}
        self.assertEqual(sheets[batch2.id]["carried_over_count"], 1)
        self.assertEqual(sheets[batch1.id]["carried_over_count"], 0)

    def test_ingest_retains_raw_csv_and_backfill_uses_it(self):
        """The original CSV is kept on the batch (raw_file); the backfill command can
        reconstruct skips straight from it with no --file."""
        from django.core.management import call_command
        from .models import MarketplaceImportSkip

        batch1 = self._ingest_main()
        od1 = batch1.orders.get(order_id="OD1")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        batch2 = ingest(
            self.company,
            text=make_csv([row("OD1", "Extra Virgin 1L", 1), row("ODNEW", "Canola 5L", 1)]),
            filename="again.csv", user=self.user,
        )
        self.assertTrue(batch2.raw_file)  # CSV retained
        # Drop auto-recorded skips, then backfill purely from the retained CSV.
        MarketplaceImportSkip.objects.filter(import_batch=batch2).delete()
        call_command("mp_backfill_import_skips", "--batch", str(batch2.id), "--apply")
        skips = MarketplaceImportSkip.objects.filter(import_batch=batch2)
        self.assertEqual(skips.count(), 1)
        self.assertEqual(skips.first().order_id, "OD1")

    def test_sheet_with_no_skips_has_empty_carried_over(self):
        from .services import dispatch_board_service as board
        batch = self._ingest_main()
        bd = board.sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        self.assertEqual(bd["carried_over"], [])
        sheets = {x["id"]: x for x in
                  board.list_sheets(self.company, MarketplaceChannel.FLIPKART)["sheets"]}
        self.assertEqual(sheets[batch.id]["carried_over_count"], 0)

    def test_cancel_after_scan_shows_cancelled_status_and_keeps_data(self):
        """A scanned order cancelled at pickup shows CANCELLED (its own section),
        keeps its scan data, and stays out of the delivery-note flow."""
        from .models import MarketplaceScan
        from .services import dispatch_board_service as board

        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        od1.tracking_id = "TRK-CA"
        od1.save(update_fields=["tracking_id"])
        line = od1.lines.get()
        line.tracking_id = "TRK-CA"
        line.save(update_fields=["tracking_id"])
        # Scanned, then cancelled at pickup — dispatch CANCELLED, scans kept.
        d = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CANCELLED, cancel_reason="Cancelled at pickup",
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=d, barcode_raw="TRK-CA#EV-1L",
            item_code="EV-1L", quantity=Decimal("1"), scanned_by=self.user,
        )
        bd = board.sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        o = next(x for x in bd["orders"] if x["order_id"] == "OD1")
        self.assertEqual(o["status"], "CANCELLED")
        self.assertEqual(o["cancel_reason"], "Cancelled at pickup")
        self.assertEqual(o["dispatch_id"], d.id)       # dispatch kept for reference
        self.assertEqual(o["tracking_scanned"], 1)     # scan data preserved
        self.assertEqual(bd["insights"]["cancelled_orders"], 1)

    def test_rescan_after_cancel_reactivates_order(self):
        """A fresh (active) dispatch wins over a cancelled one, so a re-scanned order
        leaves the cancelled section."""
        from .services import dispatch_board_service as board
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CANCELLED, cancel_reason="oops",
        )
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.READY,
        )
        bd = board.sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        o = next(x for x in bd["orders"] if x["order_id"] == "OD1")
        self.assertNotEqual(o["status"], "CANCELLED")  # active dispatch wins

    def test_blank_sku_row_counted_and_reconciles(self):
        batch = ingest(
            self.company,
            text=make_csv([row("ODA", "Extra Virgin 1L", 1), row("ODA", "", 1)]),
            filename="blank.csv", user=self.user,
        )
        s = batch.summary
        self.assertEqual(batch.row_count, 2)
        self.assertEqual(s["blank_sku_skipped"], 1)
        self.assertEqual(s["lines"], 1)
        self.assertEqual(
            batch.row_count,
            s["lines"] + s["blank_sku_skipped"] + s["skipped_order_rows"] + s["skipped"],
        )

    def test_backfill_import_skips_reconstructs_dispatched(self):
        import os
        import tempfile
        from django.core.management import call_command
        from .models import MarketplaceImportSkip

        batch1 = self._ingest_main()
        od1 = batch1.orders.get(order_id="OD1")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        csv_text = make_csv([row("OD1", "Extra Virgin 1L", 1), row("ODNEW", "Canola 5L", 1)])
        batch2 = ingest(self.company, text=csv_text, filename="again.csv", user=self.user)
        # Simulate a legacy batch with no skip records.
        MarketplaceImportSkip.objects.filter(import_batch=batch2).delete()

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write(csv_text)
            path = fh.name
        try:
            call_command("mp_backfill_import_skips", "--batch", str(batch2.id),
                         "--file", path, "--apply")
        finally:
            os.unlink(path)

        skips = MarketplaceImportSkip.objects.filter(import_batch=batch2)
        self.assertEqual(skips.count(), 1)
        self.assertEqual(skips.first().order_id, "OD1")
        self.assertEqual(skips.first().reason, "DISPATCHED")

    def test_backfill_confirmed_scans_fills_missing_scans(self):
        """The backfill command reconstructs the scan rows for an order confirmed
        without a scan, so it reads as fully scanned (local audit only)."""
        from django.core.management import call_command
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
            MarketplaceOrderLine, OrderImportBatch,
        )
        from .services.scan_service import dispatch_is_fully_scanned

        batch = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="bk.csv",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODBK", buyer_name="B", import_batch=batch,
        )
        for tid in ("TRK-A", "TRK-B"):
            MarketplaceOrderLine.objects.create(
                order=order, marketplace_sku="Extra Virgin 1L",
                ordered_quantity=Decimal("1"), tracking_id=tid,
            )
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        self.assertFalse(dispatch_is_fully_scanned(dispatch))

        # Dry run writes nothing.
        call_command("mp_backfill_confirmed_scans", "--company", "TST", "--channel", "FLIPKART")
        self.assertEqual(dispatch.scans.count(), 0)

        # Apply reconstructs both Tracking-ID scans.
        call_command("mp_backfill_confirmed_scans", "--company", "TST", "--channel", "FLIPKART", "--apply")
        self.assertTrue(dispatch_is_fully_scanned(dispatch))
        self.assertEqual(
            set(dispatch.scans.values_list("barcode_raw", flat=True)),
            {"TRK-A#EV-1L", "TRK-B#EV-1L"},
        )

        # Idempotent — a second apply adds nothing.
        call_command("mp_backfill_confirmed_scans", "--company", "TST", "--channel", "FLIPKART", "--apply")
        self.assertEqual(dispatch.scans.count(), 2)

    @override_settings(MARKETPLACE_SIMULATE_SAP=True)
    def test_tracking_scan_then_confirm_succeeds(self):
        """Positive path: an order scanned by Tracking ID reaches READY and confirms
        cleanly — the new full-scan gate must NOT block a properly scanned order."""
        from .services import scan_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        line = od1.lines.get()
        line.tracking_id = "FMPP-OD1-A"
        line.save(update_fields=["tracking_id"])
        self._pack_order(od1)

        dispatch, _created, _dup = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-OD1-A", user=self.user
        )
        self.assertEqual(dispatch.status, MarketplaceDispatchStatus.READY)
        confirmed = confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(confirmed.status, MarketplaceDispatchStatus.CONFIRMED)

    def test_backfill_combo_order_scans_each_fg_component(self):
        """A confirmed combo order backfills one scan per finished-goods component
        (PM excluded), keyed to the order's Tracking ID."""
        from django.core.management import call_command
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
            MarketplaceOrderLine, OrderImportBatch,
        )
        from .services.scan_service import dispatch_is_fully_scanned

        batch = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="cb.csv",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODCOMBO", buyer_name="B", import_batch=batch,
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Canola 5+1L", ordered_quantity=Decimal("1"),
            tracking_id="TRK-C",
        )
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        call_command("mp_backfill_confirmed_scans", "--company", "TST", "--channel", "FLIPKART", "--apply")
        self.assertEqual(
            set(dispatch.scans.values_list("barcode_raw", flat=True)),
            {"TRK-C#CAN-5L", "TRK-C#CAN-1L"},   # 2 FG components; PM-BOX not scanned
        )
        self.assertTrue(dispatch_is_fully_scanned(dispatch))

    def test_reconciliation_report_computes_outward_vs_portal(self):
        """reconciliation_service.build_report: a confirmed order scanned fully out
        with no return — portal==outward, physical net==outward, and (per the metric)
        outward_vs_inward == outward since nothing came back."""
        from .services import reconciliation_service, scan_service

        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")   # 1x EV-1L
        line = od1.lines.get()
        line.tracking_id = "FMPP-REC-1"
        line.save(update_fields=["tracking_id"])
        self._pack_order(od1)
        dispatch, _c, _d = scan_service.scan_dispatch_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="FMPP-REC-1", user=self.user
        )
        confirm_dispatch(dispatch, user=self.user)

        report = reconciliation_service.build_report(
            self.company, channel=MarketplaceChannel.FLIPKART, order_id="OD1"
        )
        row = next(r for r in report["rows"] if r["item_code"] == "EV-1L")
        self.assertEqual(Decimal(row["portal_quantity"]), Decimal("1"))
        self.assertEqual(Decimal(row["outward_quantity"]), Decimal("1"))
        self.assertEqual(Decimal(row["inward_quantity"]), Decimal("0"))
        self.assertEqual(Decimal(row["physical_quantity"]), Decimal("1"))
        # ordered == net shipped, so portal-vs-physical is balanced …
        self.assertEqual(Decimal(row["portal_vs_physical_deviation"]), Decimal("0"))
        # … but outward-vs-inward reflects the unreturned shipment (out − in = 1).
        self.assertEqual(Decimal(row["outward_vs_inward_deviation"]), Decimal("1"))

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

    def test_tracking_priority_unscanned_tracking_stays_partial(self):
        """Give priority to Tracking IDs on the Outward board.

        An order whose dispatch is READY by quantity (e.g. a packing-barcode scan
        completed both units) but still has an un-individually-scanned tracking ID
        must stay visible as PARTIAL — so the sheet's 'tracking left' count matches
        real, scannable work instead of showing '2 left / nothing to scan'."""
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
            MarketplaceOrderLine, MarketplaceScan, OrderImportBatch,
        )
        from .services.dispatch_board_service import sheet_board

        batch = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="s.csv",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODX", buyer_name="B", import_batch=batch,
        )
        # Two lines, SAME item, DIFFERENT tracking IDs.
        for tid in ("TRK-A", "TRK-B"):
            MarketplaceOrderLine.objects.create(
                order=order, marketplace_sku="Extra Virgin 1L",
                ordered_quantity=Decimal("1"), tracking_id=tid,
            )
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.READY,
        )
        # Only TRK-A was recorded (one scan completed both units by quantity).
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="TRK-A#EV-1L",
            item_code="EV-1L", quantity=Decimal("2"), scanned_by=self.user,
        )

        board = sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        ins = board["insights"]
        # Tracking priority: TRK-B is still owed, so the order is NOT complete.
        self.assertEqual(ins["total_orders"], 1)
        self.assertEqual(ins["completed_orders"], 0)
        self.assertEqual(ins["tracking_total"], 2)
        self.assertEqual(ins["tracking_scanned"], 1)
        self.assertEqual(ins["tracking_remaining"], 1)
        o = board["orders"][0]
        self.assertEqual(o["status"], "PARTIAL")  # surfaced as work to do
        scanned_by_tid = {i["tracking_id"]: i["scanned"] for i in o["items"]}
        self.assertTrue(scanned_by_tid["TRK-A"])
        self.assertFalse(scanned_by_tid["TRK-B"])

    def test_confirmed_order_shows_real_scan_state(self):
        """A CONFIRMED order shows its TRUE scan state — a Tracking ID that was never
        scanned is reported as unscanned, so an order confirmed without a full scan
        (e.g. a supervisor override) is visible on the board and in the CSV instead
        of silently reading 'done'."""
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
            MarketplaceOrderLine, MarketplaceScan, OrderImportBatch,
        )
        from .services.dispatch_board_service import sheet_board

        batch = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="c.csv",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODC", buyer_name="B", import_batch=batch,
        )
        for tid in ("TRK-A", "TRK-B"):
            MarketplaceOrderLine.objects.create(
                order=order, marketplace_sku="Extra Virgin 1L",
                ordered_quantity=Decimal("1"), tracking_id=tid,
            )
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        # Only TRK-A was scanned; TRK-B never was.
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="TRK-A#EV-1L",
            item_code="EV-1L", quantity=Decimal("2"), scanned_by=self.user,
        )

        board = sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        ins = board["insights"]
        o = board["orders"][0]
        self.assertEqual(o["status"], "CONFIRMED")
        self.assertEqual(ins["tracking_total"], 2)
        self.assertEqual(ins["tracking_scanned"], 1)   # real count, not faked to 2
        self.assertEqual(ins["tracking_remaining"], 1)
        scanned_by_tid = {i["tracking_id"]: i["scanned"] for i in o["items"]}
        self.assertTrue(scanned_by_tid["TRK-A"])
        self.assertFalse(scanned_by_tid["TRK-B"])   # never scanned → shown as such

    def test_legacy_order_without_trackings_uses_dispatch_status(self):
        """An order with no per-line tracking IDs falls back to dispatch status."""
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
            MarketplaceOrderLine, OrderImportBatch,
        )
        from .services.dispatch_board_service import sheet_board

        batch = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="s2.csv",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODLEG", buyer_name="B", import_batch=batch,
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Extra Virgin 1L", ordered_quantity=Decimal("1"),
        )  # no tracking_id
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.READY,
        )
        board = sheet_board(self.company, MarketplaceChannel.FLIPKART, batch.id)
        self.assertEqual(board["orders"][0]["status"], "SCANNED")

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

        # Two single-item orders that resolve to the SAME finished good so the merge
        # sums them — exercising both the insert and the accumulate branches of
        # _merge_lines. Each order is fully scanned so it can be confirmed.
        batch = ingest(
            self.company,
            text=make_csv([
                row("ODA", "Extra Virgin 1L", 1, invoice="900"),
                row("ODB", "Extra Virgin 1L", 1, invoice="900"),
            ]),
            filename="merge.csv", user=self.user,
        )
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user
        )

        _oda, d1 = self._ready_dispatch(batch, "ODA", "EV-1L")
        _odb, d2 = self._ready_dispatch(batch, "ODB", "EV-1L")
        self._pack_order(_oda)
        self._pack_order(_odb)
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
        # A return is only allowed against a dispatched order.
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od2,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )

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

    def test_reimport_reapproval_recovers_cancelled_order(self):
        """A cancelled-at-import order is is_cancelled=True / status OPEN, and a later
        re-approval sheet clears the flag; a legacy RETURNED order is recovered to OPEN."""
        from .models import MarketplaceOrder, MarketplaceOrderStatus
        ingest(self.company, text=make_csv([row("ODX", "Extra Virgin 1L", 1, state="Cancelled")]),
               filename="c.csv", user=self.user)
        odx = MarketplaceOrder.objects.get(company=self.company, order_id="ODX")
        self.assertTrue(odx.is_cancelled)
        self.assertEqual(odx.status, MarketplaceOrderStatus.OPEN)
        # Re-approve.
        ingest(self.company, text=make_csv([row("ODX", "Extra Virgin 1L", 1)]),
               filename="a.csv", user=self.user)
        odx.refresh_from_db()
        self.assertFalse(odx.is_cancelled)
        # Legacy data stuck at RETURNED is recovered on re-approve.
        odx.status = MarketplaceOrderStatus.RETURNED
        odx.save(update_fields=["status"])
        ingest(self.company, text=make_csv([row("ODX", "Extra Virgin 1L", 1)]),
               filename="a2.csv", user=self.user)
        odx.refresh_from_db()
        self.assertEqual(odx.status, MarketplaceOrderStatus.OPEN)

    def test_export_posted_delivery_note_csv(self):
        """Posted-DN CSV has one row per ORDER ITEM in the Flipkart order-sheet layout,
        plus resolved-SAP-item and delivery-note context columns."""
        import csv as _csv
        import io as _io
        from django.utils import timezone
        from .services.delivery_note_service import DN_CSV_HEADER, export_posted_delivery_note_csv

        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")  # 1x EV-1L
        ln = od1.lines.get()
        ln.hsn_code = "15099090"
        ln.invoice_amount = "900"
        ln.save(update_fields=["hsn_code", "invoice_amount"])
        od2 = batch.orders.get(order_id="OD2")  # 2x combo Canola 5+1L
        for o in (od1, od2):
            MarketplaceDispatch.objects.create(
                company=self.company, channel=MarketplaceChannel.FLIPKART, order=o,
                status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_doc_entry=7001,
                sap_delivery_note_num="DN7001", confirmed_at=timezone.now(),
            )
        filename, text = export_posted_delivery_note_csv(self.company, 7001)
        self.assertIn("DN7001", filename)
        rows = list(_csv.reader(_io.StringIO(text)))
        self.assertEqual(rows[0], DN_CSV_HEADER)
        col = {name: i for i, name in enumerate(DN_CSV_HEADER)}
        r = next(r for r in rows[1:] if r[col["Order Id"]] == "OD1")
        self.assertEqual(r[col["HSN CODE"]], "15099090")
        self.assertEqual(Decimal(r[col["Invoice Amount"]]), Decimal("900"))
        self.assertEqual(Decimal(r[col["Quantity"]]), Decimal("1"))
        self.assertIn("EV-1L", r[col["SAP Item Code"]])   # resolved finished good
        self.assertEqual(r[col["DN Number"]], "DN7001")
        self.assertEqual(r[col["Channel"]], "FLIPKART")
        # Warehouse falls back to the master's godown (order.sap_warehouse_code is blank).
        self.assertEqual(r[col["Warehouse"]], "WH1")
        self.assertEqual(r[col["SAP Qty"]], "1")
        # A combo shows one qty per item code, positionally aligned: 2 ordered x 1 each.
        r2 = next(r for r in rows[1:] if r[col["Order Id"]] == "OD2")
        self.assertEqual(r2[col["SAP Item Code"]].split("; "), ["CAN-5L", "CAN-1L"])
        self.assertEqual(r2[col["SAP Qty"]].split("; "), ["2", "2"])

    def test_dn_csv_reports_pieces_per_item_not_a_deduped_code_list(self):
        """A pack that ships the SAME item more than once (``1+1L``) must report the
        piece count, and a repeated SKU must report ITS OWN count, not the order total.

        Both are what SAP actually posts; without them the export cannot be reconciled
        against DLN1 and every combo looks short.
        """
        import csv as _csv
        import io as _io
        from django.utils import timezone
        from .models import MarketplaceOrder, MarketplaceOrderLine
        from .services.delivery_note_service import DN_CSV_HEADER, export_posted_delivery_note_csv

        # 'Pomace 1+1L' → one FG slot shipping 2 pieces of the same item.
        combo = ComboDefinition.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            code="POM-1+1", name="Pomace 1+1L",
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code="POM-1L",
            item_name="Pomace 1L", quantity=Decimal("2"),
        )
        SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="Pomace 1+1L", sku_type=SkuType.COMBO, combo=combo,
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="ODPOM", buyer_name="X",
        )
        # Same SKU twice on one order — the old per-order resolve aggregated these.
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Pomace 1+1L", ordered_quantity=Decimal("1"),
            order_item_id="P1",
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="Pomace 1+1L", ordered_quantity=Decimal("3"),
            order_item_id="P2",
        )
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_doc_entry=7003,
            sap_delivery_note_num="DN7003", confirmed_at=timezone.now(),
        )
        _, text = export_posted_delivery_note_csv(self.company, 7003)
        rows = list(_csv.reader(_io.StringIO(text)))
        col = {name: i for i, name in enumerate(DN_CSV_HEADER)}
        by_item = {r[col["ORDER ITEM ID"]]: r for r in rows[1:]}

        # 1 ordered x 2 per pack = 2 pieces; 3 ordered x 2 = 6. Not 8 on both rows.
        self.assertEqual(by_item["P1"][col["SAP Item Code"]], "POM-1L")
        self.assertEqual(by_item["P1"][col["SAP Qty"]], "2")
        self.assertEqual(by_item["P2"][col["SAP Qty"]], "6")
        # Summing the export now reproduces the SAP delivery note exactly.
        total = sum(Decimal(by_item[k][col["SAP Qty"]]) for k in ("P1", "P2"))
        self.assertEqual(total, Decimal("8"))

    def test_dn_csv_captures_invoice_columns_from_sheet(self):
        """Invoice No. / Invoice Date / Dispatch After date are captured from a sheet
        that carries them and reproduced in the posted-DN CSV."""
        import csv as _csv
        import io as _io
        from django.utils import timezone
        from .services.delivery_note_service import DN_CSV_HEADER, export_posted_delivery_note_csv

        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(HEADER + ["Invoice No.", "Invoice Date (mm/dd/yy)", "Dispatch After date"])
        w.writerow(row("ODINV", "Extra Virgin 1L", 1) + ["LWAAK123", "7/29/2026", "7/28/26 15:01"])
        batch = ingest(self.company, text=buf.getvalue(), filename="inv.csv", user=self.user)

        odinv = batch.orders.get(order_id="ODINV")
        self.assertEqual(odinv.lines.get().raw_row.get("invoice_no"), "LWAAK123")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=odinv,
            status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_doc_entry=7002,
            sap_delivery_note_num="DN7002", confirmed_at=timezone.now(),
        )
        _, text = export_posted_delivery_note_csv(self.company, 7002)
        rows = list(_csv.reader(_io.StringIO(text)))
        col = {name: i for i, name in enumerate(DN_CSV_HEADER)}
        r = next(r for r in rows[1:] if r[col["Order Id"]] == "ODINV")
        self.assertEqual(r[col["Invoice No."]], "LWAAK123")
        self.assertEqual(r[col["Invoice Date (mm/dd/yy)"]], "7/29/2026")
        self.assertEqual(r[col["Dispatch After date"]], "7/28/26 15:01")

    def _csv_with_tracking(self, order_id, tracking, *, sku="Extra Virgin 1L", item_id="'900"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(HEADER + ["Tracking ID"])
        w.writerow(row(order_id, sku, 1, item_id=item_id) + [tracking])
        return buf.getvalue()

    def test_carried_over_retracks_changed_tracking(self):
        """A carried-over (already-scanned, not-confirmed) order re-listed with a NEW
        tracking id is re-tracked and shown directly in 'To scan' on the NEW sheet."""
        from django.utils import timezone
        from .services.dispatch_board_service import sheet_board
        from .services.scan_service import dispatch_is_fully_scanned

        a = ingest(self.company, text=self._csv_with_tracking("OD-RT", "T1"),
                   filename="a.csv", user=self.user)
        o = a.orders.get(order_id="OD-RT")
        self.assertEqual(o.lines.get().tracking_id, "T1")
        disp = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=o,
            status=MarketplaceDispatchStatus.READY,
        )
        MarketplaceScan.objects.create(
            company=self.company, dispatch=disp, barcode_raw="T1#FG0000001",
            scanned_at=timezone.now(),
        )
        self.assertTrue(dispatch_is_fully_scanned(disp))  # scanned on T1

        # Re-list the same order on a newer sheet with a CHANGED tracking id.
        b = ingest(self.company, text=self._csv_with_tracking("OD-RT", "T2"),
                   filename="b.csv", user=self.user)
        o.refresh_from_db()
        self.assertEqual(o.lines.get().tracking_id, "T2")           # re-tracked
        self.assertEqual(o.import_batch_id, b.id)                   # MOVED to the new sheet
        self.assertFalse(b.skips.filter(order_id="OD-RT").exists())  # NOT a carried-over note
        self.assertFalse(dispatch_is_fully_scanned(disp))           # re-opened for scanning
        self.assertEqual(b.summary.get("retracked"), 1)
        # The stale T1 scan is retired so it can't double-count at confirm.
        self.assertEqual(
            MarketplaceScan.objects.filter(dispatch=disp, is_active=True).count(), 0)
        # It shows directly in 'To scan' (PENDING) on the new sheet's board.
        board = sheet_board(self.company, MarketplaceChannel.FLIPKART, b.id)
        ov = next(x for x in board["orders"] if x["order_id"] == "OD-RT")
        self.assertEqual(ov["status"], "PENDING")
        self.assertEqual((ov["tracking_scanned"], ov["tracking_total"]), (0, 1))

    def test_carried_over_confirmed_order_new_tracking_is_rescannable(self):
        """A shipped (CONFIRMED) order re-listed on a newer sheet with a DIFFERENT
        tracking id is a NEW parcel — the Tracking ID is the identity, not the order
        id. It is re-tracked, pulled onto the new sheet and shown in 'To scan' with a
        fresh dispatch, while the original CONFIRMED dispatch (its posted delivery
        note) is preserved untouched as history."""
        from .services.dispatch_board_service import sheet_board

        a = ingest(self.company, text=self._csv_with_tracking("OD-CF", "T1"),
                   filename="a.csv", user=self.user)
        o = a.orders.get(order_id="OD-CF")
        confirmed = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=o,
            status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_doc_entry=9100,
            sap_delivery_note_num="DN9100",
        )
        b = ingest(self.company, text=self._csv_with_tracking("OD-CF", "T9"),
                   filename="b.csv", user=self.user)
        o.refresh_from_db()
        self.assertEqual(o.lines.get().tracking_id, "T9")            # re-tracked to new parcel
        self.assertEqual(o.import_batch_id, b.id)                    # MOVED to the new sheet
        self.assertFalse(b.skips.filter(order_id="OD-CF").exists())  # NOT a carried-over note
        self.assertEqual(b.summary.get("retracked"), 1)
        # The original CONFIRMED shipment is preserved untouched (its DN stays posted).
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, MarketplaceDispatchStatus.CONFIRMED)
        self.assertEqual(confirmed.sap_delivery_note_num, "DN9100")
        # A fresh, non-confirmed dispatch now leads so the new parcel is scannable.
        latest = (MarketplaceDispatch.objects
                  .filter(company=self.company, order=o)
                  .exclude(status=MarketplaceDispatchStatus.CANCELLED)
                  .order_by("-created_at", "-id").first())
        self.assertNotEqual(latest.id, confirmed.id)
        self.assertEqual(latest.status, MarketplaceDispatchStatus.DRAFT)
        # It shows directly in 'To scan' (PENDING) on the new sheet's board.
        board = sheet_board(self.company, MarketplaceChannel.FLIPKART, b.id)
        ov = next(x for x in board["orders"] if x["order_id"] == "OD-CF")
        self.assertEqual(ov["status"], "PENDING")
        self.assertEqual((ov["tracking_scanned"], ov["tracking_total"]), (0, 1))

    def test_carried_over_confirmed_order_same_tracking_stays(self):
        """A CONFIRMED order re-listed with the SAME tracking id is a true carry-over:
        left on its original sheet, no re-track and no extra dispatch."""
        a = ingest(self.company, text=self._csv_with_tracking("OD-CF2", "T1"),
                   filename="a.csv", user=self.user)
        o = a.orders.get(order_id="OD-CF2")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=o,
            status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_doc_entry=9200,
            sap_delivery_note_num="DN9200",
        )
        b = ingest(self.company, text=self._csv_with_tracking("OD-CF2", "T1"),
                   filename="b.csv", user=self.user)
        o.refresh_from_db()
        self.assertEqual(o.lines.get().tracking_id, "T1")            # unchanged
        self.assertEqual(o.import_batch_id, a.id)                    # stays on original sheet
        self.assertEqual(b.summary.get("retracked"), 0)
        self.assertTrue(b.skips.filter(order_id="OD-CF2").exists())  # carried-over note
        self.assertEqual(MarketplaceDispatch.objects.filter(order=o).count(), 1)  # no new dispatch

    def test_return_requires_dispatched_order(self):
        from .services import scan_service
        from .services.errors import MarketplaceError
        batch = self._ingest_main()
        self._issue_batch(batch)
        od1 = batch.orders.get(order_id="OD1")
        od1.tracking_id = "T-ND"
        od1.save(update_fields=["tracking_id"])
        with self.assertRaises(MarketplaceError) as ctx:
            scan_service.scan_return_by_tracking(
                self.company, MarketplaceChannel.FLIPKART, barcode="T-ND", user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_DISPATCHED")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        _r, created, _dup = scan_service.scan_return_by_tracking(
            self.company, MarketplaceChannel.FLIPKART, barcode="T-ND", user=self.user)
        self.assertTrue(created)

    def test_return_over_return_rejected(self):
        from .models import MarketplaceReturn, MarketplaceReturnStatus
        from .services.scan_service import record_return_scan
        from .services.errors import MarketplaceError
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")  # EV-1L x1
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        mp_return = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceReturnStatus.DRAFT,
        )
        record_return_scan(mp_return, barcode_raw="A", item_code="EV-1L", quantity="1", user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            record_return_scan(mp_return, barcode_raw="B", item_code="EV-1L", quantity="1", user=self.user)
        self.assertEqual(ctx.exception.code, "OVER_RETURN")

    def test_return_submit_sets_order_status(self):
        from .models import (
            MarketplaceOrderStatus, MarketplaceReturn, MarketplaceReturnStatus,
        )
        from .services import return_service
        from .services.scan_service import record_return_scan
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")  # EV-1L x1
        MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceDispatchStatus.CONFIRMED,
        )
        mp_return = MarketplaceReturn.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            status=MarketplaceReturnStatus.DRAFT,
        )
        record_return_scan(mp_return, barcode_raw="A", item_code="EV-1L", quantity="1", user=self.user)
        return_service.submit_return(mp_return, user=self.user)
        od1.refresh_from_db()
        self.assertEqual(od1.status, MarketplaceOrderStatus.RETURNED)  # fully returned

    def test_confirm_routes_to_matching_warehouse(self):
        from .models import MarketplaceWarehouse
        from .services.confirm_service import _warehouse_for
        batch = self._ingest_main()
        od1 = batch.orders.get(order_id="OD1")
        wh2 = MarketplaceWarehouse.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, name="WH2",
            sap_warehouse_code="WH2", sap_customer_card_code="C2",
        )
        d = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=od1,
            sap_warehouse_code="WH2", status=MarketplaceDispatchStatus.READY,
        )
        self.assertEqual(_warehouse_for(d).id, wh2.id)  # routed by code
        d.sap_warehouse_code = ""
        self.assertEqual(_warehouse_for(d).sap_warehouse_code, "WH1")  # falls back to default

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

    def test_ships_as_is_reported_for_every_line_choice_or_not(self):
        """The cut screen shows what each order ships, not just the pickable ones.

        ``options`` is empty for the SKUs that map to a single SAP item -- the vast
        majority -- so the column had nothing to render and printed a dash. Every
        line now carries the item(s) it actually resolves to, with the piece count.
        """
        from .services import variant_service

        # This line HAS a choice: ships_as follows the default option (SANO).
        with_choice = variant_service.order_variants(self.order)[0]
        self.assertTrue(with_choice["has_choice"])
        self.assertEqual(
            with_choice["ships_as"],
            [{"item_code": "FG0000138", "item_name": "SANO Sunflower", "quantity": "2.000"}],
        )

        # A plain single-item SKU: no options at all, but it still reports its item.
        plain = SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="POM-1L", fsn="FSNPOM1", sku_type=SkuType.RAW,
            fg_item_code="FG0000028", fg_item_name="POMACE OLIVE 1 LTR 16 PCS",
        )
        line = self.order.lines.create(
            marketplace_sku="POM-1L", fsn="FSNPOM1", ordered_quantity=Decimal("3"))
        v = next(x for x in variant_service.order_variants(self.order) if x["line_id"] == line.id)
        self.assertFalse(v["has_choice"])
        self.assertEqual(v["options"], [])
        self.assertEqual(
            v["ships_as"],
            [{"item_code": "FG0000028", "item_name": "POMACE OLIVE 1 LTR 16 PCS", "quantity": "3.000"}],
        )
        self.assertEqual(plain.marketplace_sku, "POM-1L")

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


class DnSheetsListTests(SheetFlowTests):
    """list_dn_sheets counts awaiting dispatches PER sheet (guards the GROUP BY bug
    where an inherited order_by folded into GROUP BY and counted 1 per row)."""

    def test_awaiting_count_is_per_sheet_not_per_row(self):
        from .services import delivery_note_service, settings_service
        batch = self._ingest_main()
        self._issue_batch(batch)
        settings_service.set_defer_delivery_note(
            self.company, MarketplaceChannel.FLIPKART, True, user=self.user)
        _od1, d1 = self._ready_dispatch(batch, "OD1", "EV-1L")
        _od3, d3 = self._ready_dispatch(batch, "OD3", "CAN-5L")
        self._pack_order(_od1); self._pack_order(_od3)
        confirm_dispatch(d1, user=self.user)
        confirm_dispatch(d3, user=self.user)

        sheets = delivery_note_service.list_dn_sheets(
            self.company, MarketplaceChannel.FLIPKART)["sheets"]
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0]["id"], batch.id)
        # Two awaiting dispatches in one sheet must count as 2, not 1.
        self.assertEqual(sheets[0]["awaiting_count"], 2)
        self.assertEqual(sheets[0]["posted_count"], 0)


class AmazonSheetTests(TestCase):
    """Amazon channel: separate parser (csv + xlsx), shared downstream flow."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="AZ Co", code="AZC")
        User = get_user_model()
        cls.user = User.objects.create(
            email="az@t.com", full_name="AZ", employee_code="AZ1", is_active=True
        )
        SkuMapping.objects.create(
            company=cls.company, channel=MarketplaceChannel.AMAZON,
            marketplace_sku="JM-EX Light 5+2L", sku_type=SkuType.RAW,
            fg_item_code="FG-AZ1", fg_item_name="AZ Oil",
        )

    _HEADER = [
        "Order Id", "Shipment Id", "Order Date", "Transaction Type", "Fulfillment Channel",
        "Shipment Item Id", "Quantity", "Item Description", "Asin", "Hsn/sac", "Sku",
        "Invoice Amount", "Cgst Tax", "Igst Tax", "Sgst Tax", "Ship To City", "Ship To State",
        "Ship To Postal Code", "Shipment Date", "Principal Amount",
    ]

    def _row(self, order_id, ttype="Shipment", sku="JM-EX Light 5+2L", ship="A0937"):
        return [order_id, ship, "2026-07-26 15:29:00", ttype, "AFN", "5715", "1",
                "Jivo Oil", "B0B8ZY", "15099090", sku, "3109", "0", "148.05", "0",
                "NILAMBUR", "KERALA", "679330", "2026-07-27 13:08:00", "3109"]

    def _csv(self, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(self._HEADER)
        for r in rows:
            w.writerow(r)
        return buf.getvalue()

    def test_amazon_csv_parses_to_canonical(self):
        from .services.order_import_service import parse_rows_for
        rows = parse_rows_for(MarketplaceChannel.AMAZON, text=self._csv([self._row("404-1")]))
        r = rows[0]
        self.assertEqual(r["order_id"], "404-1")
        self.assertEqual(r["sku"], "JM-EX Light 5+2L")
        self.assertEqual(r["tracking"], "A0937")   # Shipment Id → the scan id
        self.assertEqual(r["fsn"], "B0B8ZY")       # Asin → fsn
        self.assertEqual(r["quantity"], "1")

    def test_amazon_import_creates_orders(self):
        from .services.order_import_service import ingest
        batch = ingest(self.company, text=self._csv([self._row("404-1")]),
                       filename="amz.csv", channel=MarketplaceChannel.AMAZON, user=self.user)
        self.assertEqual(batch.channel, "AMAZON")
        self.assertEqual(batch.order_count, 1)
        o = batch.orders.get()
        self.assertEqual(o.order_id, "404-1")
        line = o.lines.get()
        self.assertEqual(line.tracking_id, "A0937")
        self.assertEqual(line.marketplace_sku, "JM-EX Light 5+2L")

    def test_amazon_cancel_transaction_flags_cancelled(self):
        from .services.order_import_service import ingest
        batch = ingest(self.company, text=self._csv([self._row("404-2", ttype="Cancel")]),
                       filename="amz.csv", channel=MarketplaceChannel.AMAZON, user=self.user)
        self.assertTrue(batch.orders.get(order_id="404-2").is_cancelled)

    def test_amazon_xlsx_parses(self):
        import openpyxl
        from datetime import datetime
        from .services.order_import_service import parse_rows_for
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(self._HEADER)
        ws.append(["404-3", "A0003", datetime(2026, 7, 26, 15, 29), "Shipment", "AFN", 5717, 1,
                   "Oil", "B0B8ZY", 15099090, "JM-EX Light 5+2L", 3109, 0, 148.05, 0,
                   "NILAMBUR", "KERALA", 679330, datetime(2026, 7, 27, 13, 8), 3109])
        buf = io.BytesIO()
        wb.save(buf)
        rows = parse_rows_for(MarketplaceChannel.AMAZON, content=buf.getvalue(), filename="amz.xlsx")
        self.assertEqual(rows[0]["order_id"], "404-3")
        self.assertEqual(rows[0]["tracking"], "A0003")
        self.assertEqual(rows[0]["quantity"], "1")

    def test_amazon_xlsx_parses_without_openpyxl(self):
        """The stdlib .xlsx reader (used when the server has no openpyxl) parses the
        real fields and converts Excel serial dates."""
        import sys
        from datetime import datetime
        from unittest import mock
        import openpyxl
        from .services.amazon_sheet import parse_amazon_rows
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(self._HEADER)
        ws.append(["404-9", "A0009", datetime(2026, 7, 26, 15, 29), "Shipment", "AFN", 5719, 2,
                   "Oil", "B0B8ZY", 15099090, "JM-EX Light 5+2L", 3109, 0, 148.05, 0,
                   "CITY", "ST", 111111, datetime(2026, 7, 27, 13, 8), 3109])
        buf = io.BytesIO()
        wb.save(buf)
        content = buf.getvalue()
        # Force the stdlib path (simulate a server without openpyxl).
        with mock.patch.dict(sys.modules, {"openpyxl": None}):
            rows = parse_amazon_rows(content=content, filename="a.xlsx")
        self.assertEqual(rows[0]["order_id"], "404-9")
        self.assertEqual(rows[0]["quantity"], "2")
        self.assertEqual(rows[0]["tracking"], "A0009")
        self.assertIn("2026", rows[0]["ordered_on"])   # Excel serial → date string

    def test_flipkart_unaffected_by_amazon_channel(self):
        """A Flipkart import still uses the Flipkart parser (isolation check)."""
        from .services.order_import_service import parse_rows_for
        rows = parse_rows_for(MarketplaceChannel.FLIPKART, text=make_csv([row("OD1", "SKU", 1)]))
        self.assertEqual(rows[0]["order_id"], "OD1")


class ReportsTests(TestCase):
    """Every report type builds a CSV (header at minimum); unknown type is rejected."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Rep Co", code="RPT")

    def test_each_report_builds_csv(self):
        from .services.reports_service import REPORTS, build_report_csv
        params = {"date_from": None, "date_to": None, "date_field": "order", "status": None}
        for slug in REPORTS:
            filename, text = build_report_csv(slug, self.company, MarketplaceChannel.FLIPKART, params)
            self.assertTrue(filename.endswith(".csv"), slug)
            self.assertGreaterEqual(len(text.splitlines()), 1, slug)  # header row present

    def test_unknown_report_type_rejected(self):
        from .services.errors import MarketplaceError
        from .services.reports_service import build_report_csv
        with self.assertRaises(MarketplaceError) as ctx:
            build_report_csv("nope", self.company, MarketplaceChannel.FLIPKART,
                             {"date_from": None, "date_to": None})
        self.assertEqual(ctx.exception.code, "NOT_FOUND")


class GateTests(TestCase):
    """Gate check: confirmed orders queue per sheet, approve/hold as a whole sheet."""

    @classmethod
    def setUpTestData(cls):
        from .models import MarketplaceOrder, MarketplaceOrderLine, OrderImportBatch
        cls.company = Company.objects.create(name="Gate Co", code="GTE")
        User = get_user_model()
        cls.user = User.objects.create(
            email="gate@t.com", full_name="Gate Guard", employee_code="G1", is_active=True)
        cls.batch = OrderImportBatch.objects.create(
            company=cls.company, channel=MarketplaceChannel.FLIPKART, filename="gate.csv")
        # Two confirmed orders (each 1 parcel) on the sheet.
        for oid, tid in [("OG1", "T-OG1"), ("OG2", "T-OG2")]:
            o = MarketplaceOrder.objects.create(
                company=cls.company, channel=MarketplaceChannel.FLIPKART, order_id=oid,
                import_batch=cls.batch, buyer_name="Buyer", city="Delhi", state="Delhi")
            MarketplaceOrderLine.objects.create(
                order=o, marketplace_sku="SKU", sku_name="Item", ordered_quantity=1, tracking_id=tid)
            MarketplaceDispatch.objects.create(
                company=cls.company, channel=MarketplaceChannel.FLIPKART, order=o,
                status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_num="DN1")

    def test_queue_then_approve_sheet(self):
        from .models import MarketplaceGateStatus
        from .services import gate_service
        q = gate_service.gate_queue(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(q["total_sheets"], 1)
        self.assertEqual(q["sheets"][0]["orders"], 2)
        self.assertEqual(q["sheets"][0]["parcels"], 2)
        self.assertEqual(q["sheets"][0]["gate_pending"], 2)

        detail = gate_service.sheet_gate_detail(self.company, MarketplaceChannel.FLIPKART, self.batch.id)
        self.assertEqual(detail["total_parcels"], 2)
        self.assertEqual(detail["orders"][0]["gate_status"], "PENDING")

        res = gate_service.approve_sheet(self.company, MarketplaceChannel.FLIPKART, self.batch.id, user=self.user)
        self.assertEqual(res["approved"], 2)
        d = MarketplaceDispatch.objects.filter(company=self.company).first()
        self.assertEqual(d.gate_status, MarketplaceGateStatus.APPROVED)
        self.assertEqual(d.gate_checked_by, self.user)
        # After approval the sheet has 0 pending.
        q2 = gate_service.gate_queue(self.company, MarketplaceChannel.FLIPKART)
        self.assertEqual(q2["sheets"][0]["gate_approved"], 2)
        self.assertEqual(q2["sheets"][0]["gate_pending"], 0)

    def test_hold_sheet_records_remark(self):
        from .models import MarketplaceGateStatus
        from .services import gate_service
        res = gate_service.hold_sheet(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id, user=self.user, remarks="damaged box")
        self.assertEqual(res["held"], 2)
        d = MarketplaceDispatch.objects.filter(company=self.company).first()
        self.assertEqual(d.gate_status, MarketplaceGateStatus.HOLD)
        self.assertEqual(d.gate_remarks, "damaged box")

    def test_remanifested_parcel_stays_on_the_sheet_it_shipped_from(self):
        """A re-manifested order moves onto the NEW sheet, but the parcel it already
        shipped stays on the OLD one. Each sheet shows its own parcel — the new sheet
        must not grow a 3rd row for a parcel that left under the old sheet."""
        from .models import MarketplaceOrder, MarketplaceScan, OrderImportBatch
        from .services import gate_service

        o = MarketplaceOrder.objects.get(company=self.company, order_id="OG1")
        first = MarketplaceDispatch.objects.get(order=o)
        first.import_batch = self.batch          # shipped under the original sheet
        first.save(update_fields=["import_batch"])
        MarketplaceScan.objects.create(
            company=self.company, dispatch=first, barcode_raw="T-OLD#FG1", quantity=1)

        # Flipkart re-manifests: the order is pulled onto a NEWER sheet, its lines are
        # re-tracked, and a second dispatch is confirmed there against the new parcel.
        newer = OrderImportBatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, filename="gate2.csv")
        o.lines.update(tracking_id="T-NEW")
        o.import_batch = newer
        o.save(update_fields=["import_batch"])
        second = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=o,
            import_batch=newer, status=MarketplaceDispatchStatus.CONFIRMED,
            sap_delivery_note_num="DN2")
        MarketplaceScan.objects.create(
            company=self.company, dispatch=second, barcode_raw="T-NEW#FG1", quantity=1)

        sheets = {s["batch_id"]: s
                  for s in gate_service.gate_queue(self.company, MarketplaceChannel.FLIPKART)["sheets"]}
        # Original sheet keeps BOTH its orders and the parcel that went out on it.
        self.assertEqual(sheets[self.batch.id]["orders"], 2)
        self.assertEqual(sheets[self.batch.id]["dispatches"], 2)
        # The newer sheet has exactly the one order it re-listed — not two rows for it.
        self.assertEqual(sheets[newer.id]["orders"], 1)
        self.assertEqual(sheets[newer.id]["dispatches"], 1)
        self.assertEqual(sheets[newer.id]["parcels"], 1)

        old = gate_service.sheet_gate_detail(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id)
        self.assertEqual(old["filename"], "gate.csv")
        self.assertEqual([r["tracking_ids"] for r in old["orders"] if r["dispatch_id"] == first.id],
                         [["T-OLD"]])  # the parcel that actually went out, not the re-track
        new = gate_service.sheet_gate_detail(
            self.company, MarketplaceChannel.FLIPKART, newer.id)
        self.assertEqual(new["filename"], "gate2.csv")
        self.assertEqual(new["total_orders"], 1)
        self.assertEqual(new["total_rows"], 1)
        self.assertEqual([r["dispatch_id"] for r in new["orders"]], [second.id])
        self.assertEqual(new["orders"][0]["tracking_ids"], ["T-NEW"])

    def test_legacy_dispatch_without_batch_falls_back_to_the_orders_sheet(self):
        """Rows predating ``dispatch.import_batch`` still list under their order's sheet."""
        from .services import gate_service
        self.assertTrue(
            MarketplaceDispatch.objects.filter(import_batch__isnull=True).exists())
        sheet = gate_service.gate_queue(self.company, MarketplaceChannel.FLIPKART)["sheets"][0]
        self.assertEqual(sheet["batch_id"], self.batch.id)
        self.assertEqual(sheet["orders"], 2)

    def test_dispatch_without_scans_falls_back_to_line_tracking(self):
        """Legacy / force-confirmed dispatches have no scans — the order's line
        tracking is still shown so the gate row is never blank."""
        from .services import gate_service
        detail = gate_service.sheet_gate_detail(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id)
        self.assertEqual(
            sorted(t for r in detail["orders"] for t in r["tracking_ids"]),
            ["T-OG1", "T-OG2"],
        )


class DeliveryNoteBackdateTests(TestCase):
    """Cutting a delivery note into the previous month — the guard rails.

    A month can close with orders confirmed but not yet cut; those notes belong in
    the month the goods actually left. ``resolve_doc_date`` decides what is allowed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="DN Co", code="DNB")

    class _User:
        def __init__(self, allowed):
            self.allowed = allowed
            self.email = "dn@t.com"

        def has_perm(self, codename):
            return self.allowed

    @staticmethod
    def _items(*confirmed_dates):
        """``includable``-shaped stubs confirmed on the given dates."""
        from types import SimpleNamespace
        out = []
        for d in confirmed_dates:
            when = timezone.make_aware(datetime.datetime(d.year, d.month, d.day, 10, 0))
            out.append({"dispatch": SimpleNamespace(confirmed_at=when)})
        return out

    def _resolve(self, doc_date, items, allowed=True, today=datetime.date(2026, 8, 4)):
        from .services.delivery_note_service import resolve_doc_date
        return resolve_doc_date(items, doc_date, user=self._User(allowed), today=today)

    def _code(self, ctx):
        return ctx.exception.code

    def test_no_date_given_is_today_and_not_backdated(self):
        from .services.delivery_note_service import resolve_doc_date
        today = datetime.date(2026, 8, 4)
        self.assertEqual(
            resolve_doc_date(self._items(datetime.date(2026, 7, 30)), None, today=today),
            (today, False),
        )

    def test_same_month_date_is_not_treated_as_backdating(self):
        """An earlier day of the CURRENT month needs no permission — same period."""
        items = self._items(datetime.date(2026, 8, 1))
        doc_date, backdated = self._resolve(datetime.date(2026, 8, 2), items, allowed=False)
        self.assertEqual(doc_date, datetime.date(2026, 8, 2))
        self.assertFalse(backdated)

    def test_previous_month_date_is_allowed_and_flagged(self):
        items = self._items(datetime.date(2026, 7, 20), datetime.date(2026, 7, 30))
        doc_date, backdated = self._resolve(datetime.date(2026, 7, 31), items)
        self.assertEqual(doc_date, datetime.date(2026, 7, 31))
        self.assertTrue(backdated)

    def test_future_date_is_rejected(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self._resolve(datetime.date(2026, 8, 5), self._items(datetime.date(2026, 7, 30)))
        self.assertEqual(self._code(ctx), "DOC_DATE_FUTURE")

    def test_backdating_without_permission_is_forbidden(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self._resolve(datetime.date(2026, 7, 31),
                          self._items(datetime.date(2026, 7, 30)), allowed=False)
        self.assertEqual(self._code(ctx), "BACKDATE_FORBIDDEN")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_month_before_last_is_closed(self):
        with self.assertRaises(MarketplaceError) as ctx:
            self._resolve(datetime.date(2026, 6, 30), self._items(datetime.date(2026, 6, 20)))
        self.assertEqual(self._code(ctx), "DOC_DATE_TOO_OLD")

    def test_date_before_the_goods_were_confirmed_is_rejected(self):
        """The note cannot pre-date the goods leaving the building."""
        with self.assertRaises(MarketplaceError) as ctx:
            self._resolve(datetime.date(2026, 7, 25), self._items(datetime.date(2026, 7, 30)))
        self.assertEqual(self._code(ctx), "DOC_DATE_BEFORE_CONFIRM")

    def test_backdating_a_mixed_month_selection_is_rejected(self):
        """Otherwise August orders would post into July's books."""
        items = self._items(datetime.date(2026, 7, 30), datetime.date(2026, 8, 2))
        with self.assertRaises(MarketplaceError) as ctx:
            self._resolve(datetime.date(2026, 7, 31), items)
        self.assertEqual(self._code(ctx), "DOC_DATE_MIXED_MONTHS")

    def test_january_backdates_into_december(self):
        """The previous-month floor must cross the year boundary."""
        items = self._items(datetime.date(2026, 12, 30))
        doc_date, backdated = self._resolve(
            datetime.date(2026, 12, 31), items, today=datetime.date(2027, 1, 3))
        self.assertEqual(doc_date, datetime.date(2026, 12, 31))
        self.assertTrue(backdated)

    def test_backdated_cut_sends_the_date_to_sap_and_records_it(self):
        """End to end: the chosen DocDate reaches the SAP payload (SAP picks its
        monthly numbering series from it) and is stored on every dispatch."""
        from unittest import mock
        from .services import delivery_note_service

        posted = {}

        def fake_resolve(includable, doc_date=None, user=None, today=None):
            return datetime.date(2026, 7, 31), True

        def fake_post_group(company, channel, gateway, warehouse, items, ship_to_code,
                            doc_date, user):
            posted["doc_date"] = doc_date
            return {"ship_to_code": ship_to_code, "pending_approval": False,
                    "delivery_note_num": "SIMDN-BD", "delivery_note_doc_entry": 1,
                    "dispatch_count": len(items),
                    "order_ids": [i["dispatch"].order.order_id for i in items]}

        with mock.patch.object(delivery_note_service, "resolve_doc_date", fake_resolve), \
             mock.patch.object(delivery_note_service, "_post_group", fake_post_group), \
             mock.patch.object(delivery_note_service, "_collect",
                               return_value=([{"dispatch": mock.Mock(), "fg": [], "pm": [],
                                               "amount": Decimal("0")}], [])), \
             mock.patch.object(delivery_note_service, "resolve_cut_warehouse"), \
             mock.patch.object(delivery_note_service, "MarketplaceSapGateway") as gw:
            gw.return_value.simulate = True
            result = delivery_note_service.cut_bulk_delivery_note(
                self.company, MarketplaceChannel.FLIPKART,
                doc_date=datetime.date(2026, 7, 31), user=self._User(True),
            )

        self.assertEqual(posted["doc_date"], datetime.date(2026, 7, 31))
        self.assertEqual(result["doc_date"], "2026-07-31")
        self.assertEqual(result["doc_month"], "July 2026")
        self.assertTrue(result["backdated"])


class GatePassTests(TestCase):
    """The outward trip: vehicle, weighment, gatepass, out at the gate.

    Modelled on the sales-dispatch gate-out, so the rules that matter there are
    asserted here too — frozen transport snapshots, and no load leaving until the
    vehicle has been weighed both empty and full.
    """

    @classmethod
    def setUpTestData(cls):
        from driver_management.models import Driver
        from vehicle_management.models import Transporter, Vehicle, VehicleType

        from .models import MarketplaceOrder, MarketplaceOrderLine, OrderImportBatch

        cls.company = Company.objects.create(name="Pass Co", code="GPS")
        User = get_user_model()
        cls.user = User.objects.create(
            email="gp@t.com", full_name="Gate Guard", employee_code="GP1", is_active=True)
        cls.batch = OrderImportBatch.objects.create(
            company=cls.company, channel=MarketplaceChannel.FLIPKART, filename="pass.csv")

        vt = VehicleType.objects.create(name="TEMPO-GP")
        cls.transporter = Transporter.objects.create(name="Arnav Transport", gstin="07AAA1111A1Z5")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="DL01GP0001", vehicle_type=vt, transporter=cls.transporter)
        cls.driver = Driver.objects.create(
            name="Soyab", mobile_no="9671747754", license_no="DL-GP-1")

        for oid, tid in [("GP1", "T-GP1"), ("GP2", "T-GP2")]:
            o = MarketplaceOrder.objects.create(
                company=cls.company, channel=MarketplaceChannel.FLIPKART, order_id=oid,
                import_batch=cls.batch, buyer_name="Buyer", city="Delhi", state="Delhi")
            MarketplaceOrderLine.objects.create(
                order=o, marketplace_sku="SKU", sku_name="Item",
                ordered_quantity=1, tracking_id=tid)
            MarketplaceDispatch.objects.create(
                company=cls.company, channel=MarketplaceChannel.FLIPKART, order=o,
                status=MarketplaceDispatchStatus.CONFIRMED, sap_delivery_note_num="DN1")

    def setUp(self):
        from .services import gate_service
        # A trip only carries parcels the gate has already passed.
        gate_service.approve_sheet(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id, user=self.user)

    def _open(self, **kwargs):
        from .services import gate_pass_service as gp
        return gp.create_gate_pass(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id, user=self.user,
            vehicle=self.vehicle, driver=self.driver, **kwargs)

    def _ready_to_leave(self):
        """A trip weighed and printed — one step from the gate."""
        from .services import gate_pass_service as gp
        p = self._open()
        return gp.record_weighment(
            self.company, p.id, user=self.user,
            tare_weight=Decimal("1000.000"), gross_weight=Decimal("1250.500"),
            weighbridge_slip_no="WB-1",
        )

    # ─── opening a trip ───────────────────────────────────────────────────

    def test_opening_a_trip_freezes_the_transport_details(self):
        """The printed pass must not change when a master record is renamed later."""
        p = self._open()
        self.assertEqual(p.vehicle_no, "DL01GP0001")
        self.assertEqual(p.driver_name, "Soyab")
        self.assertEqual(p.driver_mobile_no, "9671747754")
        self.assertEqual(p.driver_license_no, "DL-GP-1")

    def test_the_transporter_is_taken_from_the_vehicle_when_not_given(self):
        p = self._open()
        self.assertEqual(p.transporter_id, self.transporter.id)
        self.assertEqual(p.transporter_name, "Arnav Transport")
        self.assertEqual(p.transporter_gstin, "07AAA1111A1Z5")

    def test_renaming_the_master_afterwards_does_not_rewrite_the_pass(self):
        p = self._open()
        self.transporter.name = "Renamed Later Pvt Ltd"
        self.transporter.save(update_fields=["name"])
        p.refresh_from_db()
        self.assertEqual(p.transporter_name, "Arnav Transport")

    def test_a_trip_cannot_be_opened_with_nothing_approved_to_carry(self):
        from .models import MarketplaceGateStatus
        from .services.errors import MarketplaceError
        MarketplaceDispatch.objects.filter(company=self.company).update(
            gate_status=MarketplaceGateStatus.PENDING)
        with self.assertRaises(MarketplaceError) as ctx:
            self._open()
        self.assertEqual(ctx.exception.code, "NOTHING_TO_DISPATCH")

    # ─── weighment ────────────────────────────────────────────────────────

    def test_net_weight_is_derived_only_once_both_halves_are_in(self):
        from .services import gate_pass_service as gp
        p = self._open()
        p = gp.record_weighment(
            self.company, p.id, user=self.user, tare_weight=Decimal("1000.000"))
        # Empty in, loaded not yet — a gross with no tare is half a weighment.
        self.assertIsNone(p.net_weight)
        self.assertEqual(p.status, "DRAFT")

        p = gp.record_weighment(
            self.company, p.id, user=self.user, gross_weight=Decimal("1250.500"))
        self.assertEqual(p.net_weight, Decimal("250.500"))
        self.assertEqual(p.status, "WEIGHED")
        self.assertIsNotNone(p.first_weighment_at)
        self.assertIsNotNone(p.second_weighment_at)

    def test_tare_heavier_than_gross_is_refused(self):
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        p = self._open()
        with self.assertRaises(MarketplaceError) as ctx:
            gp.record_weighment(
                self.company, p.id, user=self.user,
                tare_weight=Decimal("2000.000"), gross_weight=Decimal("1250.500"))
        self.assertEqual(ctx.exception.code, "INVALID_WEIGHT")

    def test_a_zero_gross_is_refused(self):
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        p = self._open()
        with self.assertRaises(MarketplaceError):
            gp.record_weighment(self.company, p.id, user=self.user, gross_weight=Decimal("0"))

    # ─── gatepass ─────────────────────────────────────────────────────────

    def test_printing_assigns_a_numbered_pass(self):
        from .services import gate_pass_service as gp
        p = gp.print_gatepass(self.company, self._open().id, user=self.user)
        self.assertTrue(p.gatepass_no.startswith(f"MKT/{self.company.code}/"))
        self.assertEqual(p.status, "GATEPASS_PRINTED")
        self.assertTrue(p.qr_payload)

    def test_reprinting_keeps_the_original_number(self):
        """The pass in the driver's hand and the record must agree."""
        from .services import gate_pass_service as gp
        p = gp.print_gatepass(self.company, self._open().id, user=self.user)
        first = p.gatepass_no
        p = gp.print_gatepass(self.company, p.id, user=self.user)
        self.assertEqual(p.gatepass_no, first)

    def test_a_trip_with_no_vehicle_cannot_be_printed(self):
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        p = gp.create_gate_pass(
            self.company, MarketplaceChannel.FLIPKART, self.batch.id, user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            gp.print_gatepass(self.company, p.id, user=self.user)
        self.assertEqual(ctx.exception.code, "NO_VEHICLE")

    def test_an_abandoned_draft_never_burns_a_gatepass_number(self):
        from .services import gate_pass_service as gp
        self._open()  # opened and left as a draft
        p = gp.print_gatepass(self.company, self._open().id, user=self.user)
        self.assertTrue(p.gatepass_no.endswith("000001"))

    # ─── out at the gate ──────────────────────────────────────────────────

    def test_marking_out_stamps_the_parcels_and_freezes_the_load(self):
        from .services import gate_pass_service as gp
        p = gp.dispatch_out(
            self.company, self._ready_to_leave().id, user=self.user, security_name="Guard")
        self.assertEqual(p.status, "DISPATCHED")
        self.assertEqual(p.order_count, 2)
        self.assertEqual(p.parcel_count, 2)
        self.assertEqual(p.security_name, "Guard")
        self.assertIsNotNone(p.gate_out_date)
        self.assertIsNotNone(p.out_time)
        self.assertEqual(
            MarketplaceDispatch.objects.filter(gate_pass=p).count(), 2)

    def test_a_trip_cannot_leave_unweighed(self):
        """Mirrors the sales-dispatch rule: no load leaves without gross and tare."""
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        p = gp.print_gatepass(self.company, self._open().id, user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            gp.dispatch_out(self.company, p.id, user=self.user, security_name="Guard")
        self.assertEqual(ctx.exception.code, "WEIGHT_REQUIRED")

    def test_a_weighed_trip_leaves_without_a_separate_print_step(self):
        """Printing is what the driver carries and can happen after the truck has
        gone; the record must not wait on it."""
        from .services import gate_pass_service as gp
        p = self._open()
        gp.record_weighment(
            self.company, p.id, user=self.user,
            tare_weight=Decimal("1000.000"), gross_weight=Decimal("1250.500"))
        out = gp.dispatch_out(self.company, p.id, user=self.user, security_name="Guard")
        self.assertEqual(out.status, "DISPATCHED")
        # The number is still assigned, so the trip has a document reference.
        self.assertTrue(out.gatepass_no.startswith("MKT/"))

    def test_printing_after_the_trip_has_gone_keeps_it_dispatched(self):
        from .services import gate_pass_service as gp
        p = self._open()
        gp.record_weighment(
            self.company, p.id, user=self.user,
            tare_weight=Decimal("1000.000"), gross_weight=Decimal("1250.500"))
        out = gp.dispatch_out(self.company, p.id, user=self.user)
        printed = gp.print_gatepass(self.company, out.id, user=self.user)
        self.assertEqual(printed.status, "DISPATCHED")
        self.assertEqual(printed.gatepass_no, out.gatepass_no)
        self.assertIsNotNone(printed.printed_at)

    def test_a_parcel_that_has_gone_cannot_ride_a_second_trip(self):
        """Loading it twice would double-count the stock that left the site."""
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        gp.dispatch_out(self.company, self._ready_to_leave().id, user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            self._open()
        self.assertEqual(ctx.exception.code, "NOTHING_TO_DISPATCH")

    def test_a_dispatched_trip_can_no_longer_be_changed(self):
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        p = gp.dispatch_out(self.company, self._ready_to_leave().id, user=self.user)
        for call in (
            lambda: gp.record_weighment(
                self.company, p.id, user=self.user, gross_weight=Decimal("9")),
            lambda: gp.update_transport(self.company, p.id, user=self.user, driver=self.driver),
            lambda: gp.cancel_gate_pass(self.company, p.id, user=self.user, reason="x"),
        ):
            with self.assertRaises(MarketplaceError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "ALREADY_DISPATCHED")

    # ─── the locking read ─────────────────────────────────────────────────

    def test_the_locking_read_joins_nothing(self):
        """PostgreSQL refuses FOR UPDATE against the nullable side of an outer
        join, and vehicle / transporter / driver are all nullable — so combining
        select_for_update with select_related 500s in production while SQLite
        ignores the lock and passes. Assert on the SQL instead of the behaviour,
        because this backend cannot reproduce the failure.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from .services import gate_pass_service as gp

        p = self._open()
        with CaptureQueriesContext(connection) as ctx:
            gp.record_weighment(
                self.company, p.id, user=self.user, tare_weight=Decimal("10"))

        locking = [q["sql"] for q in ctx.captured_queries
                   if "FOR UPDATE" in q["sql"].upper()
                   or ("marketplace_marketplacegatepass" in q["sql"]
                       and q["sql"].upper().startswith("SELECT"))]
        self.assertTrue(locking, "expected a read of the gate pass")
        for sql in locking:
            self.assertNotIn(
                "LEFT OUTER JOIN", sql.upper(),
                "the locked read must not join the nullable FKs")

    # ─── cancelling ───────────────────────────────────────────────────────

    def test_cancelling_returns_the_parcels_to_the_waiting_list(self):
        from .services import gate_pass_service as gp
        p = gp.cancel_gate_pass(
            self.company, self._open().id, user=self.user, reason="vehicle broke down")
        self.assertEqual(p.status, "CANCELLED")
        self.assertEqual(p.cancel_reason, "vehicle broke down")
        # Nothing was stamped, so the parcels are free for the next trip.
        self.assertEqual(
            gp.eligible_dispatches(
                self.company, MarketplaceChannel.FLIPKART, self.batch.id).count(), 2)

    def test_cancelling_needs_a_reason(self):
        from .services import gate_pass_service as gp
        from .services.errors import MarketplaceError
        with self.assertRaises(MarketplaceError) as ctx:
            gp.cancel_gate_pass(self.company, self._open().id, user=self.user, reason="  ")
        self.assertEqual(ctx.exception.code, "NO_REASON")
