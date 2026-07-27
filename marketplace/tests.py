"""Core-logic tests for the marketplace app (service layer, SAP simulated)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from company.models import Company

from .models import (
    ComboComponent,
    ComboComponentType,
    ComboDefinition,
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    MarketplaceWarehouse,
    SkuMapping,
    SkuType,
)
from .services import resolve_service
from .services.confirm_service import confirm_dispatch
from .services.errors import MarketplaceError
from .services.scan_service import dispatch_progress, is_fully_scanned, record_dispatch_scan

User = get_user_model()


@override_settings(MARKETPLACE_SIMULATE_SAP=True)
class MarketplaceFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create_user(
            email="op@ji.test", password="x", full_name="Operator", employee_code="OP1"
        )
        self.wh = MarketplaceWarehouse.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            name="Mayapuri Flipkart", sap_warehouse_code="FLP-MAY",
            sap_customer_card_code="C-FLIPKART", facility_code="MAYAPURI",
        )
        # RAW SKU → FG-A
        SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="FSN-RAW", sku_type=SkuType.RAW, fg_item_code="FG-A",
            fg_item_name="Item A",
        )
        # COMBO SKU → 1x FG-OIL-3L (FG) + 2x PM-TAPE (PM)
        combo = ComboDefinition.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            code="COMBO-OIL-3L", name="3L Oil Combo",
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG,
            item_code="FG-OIL-3L", item_name="Oil 3L", quantity=Decimal("1"),
        )
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.PM,
            item_code="PM-TAPE", item_name="Tape", quantity=Decimal("2"),
        )
        SkuMapping.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            marketplace_sku="FSN-COMBO", sku_type=SkuType.COMBO, combo=combo,
        )
        self.order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="OD-100", sap_warehouse_code="FLP-MAY",
            status=MarketplaceOrderStatus.OPEN,
        )
        MarketplaceOrderLine.objects.create(
            order=self.order, marketplace_sku="FSN-RAW", ordered_quantity=Decimal("2")
        )
        MarketplaceOrderLine.objects.create(
            order=self.order, marketplace_sku="FSN-COMBO", ordered_quantity=Decimal("3")
        )

    def _new_dispatch(self):
        return MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=self.order,
            sap_warehouse_code="FLP-MAY", created_by=self.user,
        )

    def test_pack_by_tracking_id_scan(self):
        """Scanning the Flipkart Tracking ID packs the order (no manual select)."""
        from .models import MarketplacePackingStatus
        from .services import packing_service
        from .services.dispatch_gate import order_is_packed

        self.order.tracking_id = "FMPP4144829970"
        self.order.save(update_fields=["tracking_id"])

        packing, already = packing_service.scan_pack(
            self.company, MarketplaceChannel.FLIPKART,
            barcode="FMPP4144829970", user=self.user,
        )
        self.assertFalse(already)
        self.assertEqual(packing.status, MarketplacePackingStatus.PACKED)
        self.assertEqual(packing.pack_barcode, "FMPP4144829970")
        self.assertIsNotNone(packing.packed_at)
        self.assertTrue(order_is_packed(self.order))  # now dispatchable in Outward

        # Re-scanning the same label is idempotent (reports already_packed).
        _, already2 = packing_service.scan_pack(
            self.company, MarketplaceChannel.FLIPKART,
            barcode="FMPP4144829970", user=self.user,
        )
        self.assertTrue(already2)

    def test_pack_scan_unknown_tracking_id_errors(self):
        from .services import packing_service
        from .services.errors import MarketplaceError
        with self.assertRaises(MarketplaceError):
            packing_service.scan_pack(
                self.company, MarketplaceChannel.FLIPKART, barcode="NOPE", user=self.user,
            )

    def test_confirm_requires_scan_even_when_packed(self):
        """A packed order can no longer be confirmed without an Outward scan — every
        shipment must be scanned. A supervisor override still forces it through."""
        from unittest.mock import patch
        from .models import (
            MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrderStatus,
            MarketplacePacking, MarketplacePackingStatus,
        )
        from .services import confirm_service
        from .services.errors import MarketplaceError

        MarketplacePacking.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=self.order,
            status=MarketplacePackingStatus.PACKED, packed_by=self.user,
            packed_at=timezone.now(), created_by=self.user,
        )
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART, order=self.order,
            status=MarketplaceDispatchStatus.DRAFT, created_by=self.user,
        )
        # Packed but never scanned → blocked.
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_service.confirm_dispatch(dispatch, user=self.user)
        self.assertEqual(ctx.exception.code, "NOT_SCANNED")

        # Override forces the confirm (SAP post patched — covered elsewhere).
        with patch.object(confirm_service, "_try_post_delivery_note") as posted:
            result = confirm_service.confirm_dispatch(
                dispatch, user=self.user, override_deviation=True
            )
        posted.assert_called_once()
        self.assertEqual(result.status, MarketplaceDispatchStatus.CONFIRMED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, MarketplaceOrderStatus.DISPATCHED)

    def test_return_scan_condition_serializer(self):
        from .serializers import ReturnScanConditionSerializer
        ok = ReturnScanConditionSerializer(
            data={"condition": "DAMAGED", "condition_remarks": "dented tin"}
        )
        self.assertTrue(ok.is_valid(), ok.errors)
        bad = ReturnScanConditionSerializer(data={"condition": "NOT_A_CHOICE"})
        self.assertFalse(bad.is_valid())

    def test_combo_serializer_creates_inline_fsn_mapping(self):
        """Defining a combo with an FSN also creates its SKU mapping (one form)."""
        from .models import ComboDefinition, SkuMapping, SkuType
        from .serializers import ComboDefinitionSerializer

        data = {
            "channel": MarketplaceChannel.FLIPKART,
            "code": "COMBO-GN-5L",
            "name": "Groundnut 5L Combo",
            "fsn": "FSNGN5L001",
            "marketplace_sku": "GN-5L-KIT",
            "components": [
                {"component_type": "FG", "item_code": "FG0000151", "quantity": "1", "uom": "PCS"},
                {"component_type": "PM", "item_code": "PM0000006", "quantity": "1", "uom": "PCS"},
            ],
        }
        ser = ComboDefinitionSerializer(data=data)
        self.assertTrue(ser.is_valid(), ser.errors)
        combo = ser.save(company=self.company, created_by=self.user)

        mapping = SkuMapping.objects.get(company=self.company, combo=combo)
        self.assertEqual(mapping.sku_type, SkuType.COMBO)
        self.assertEqual(mapping.fsn, "FSNGN5L001")
        self.assertEqual(mapping.marketplace_sku, "GN-5L-KIT")
        # read-back exposes the mapping's fsn on the combo
        self.assertEqual(ComboDefinitionSerializer(combo).data["fsn"], "FSNGN5L001")

        # An order line with that FSN resolves through the combo to its components.
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=MarketplaceChannel.FLIPKART,
            order_id="OD-COMBO-FSN", status=MarketplaceOrderStatus.OPEN,
        )
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku="whatever", fsn="FSNGN5L001",
            ordered_quantity=Decimal("2"),
        )
        res = resolve_service.resolve_order(order)
        self.assertEqual(res["unmapped_skus"], [])
        codes = {l["item_code"] for l in res["resolved_lines"]}
        self.assertEqual(codes, {"FG0000151", "PM0000006"})

        # Editing the combo updates the SAME mapping (no duplicate).
        ser2 = ComboDefinitionSerializer(combo, data={**data, "fsn": "FSNGN5L002"}, partial=True)
        self.assertTrue(ser2.is_valid(), ser2.errors)
        ser2.save(updated_by=self.user)
        self.assertEqual(SkuMapping.objects.filter(combo=combo).count(), 1)
        self.assertEqual(SkuMapping.objects.get(combo=combo).fsn, "FSNGN5L002")

    # ── resolve / combo expansion ────────────────────────────────────────────
    def test_resolve_expands_combo_and_aggregates(self):
        resolved = resolve_service.resolve_order(self.order)
        self.assertEqual(resolved["unmapped_skus"], [])
        by_item = {l["item_code"]: l for l in resolved["resolved_lines"]}
        self.assertEqual(by_item["FG-A"]["required_quantity"], Decimal("2"))
        self.assertEqual(by_item["FG-A"]["component_type"], "FG")
        self.assertEqual(by_item["FG-OIL-3L"]["required_quantity"], Decimal("3"))
        self.assertEqual(by_item["PM-TAPE"]["required_quantity"], Decimal("6"))  # 3 × 2
        self.assertEqual(by_item["PM-TAPE"]["component_type"], "PM")

    def test_unmapped_sku_reported(self):
        MarketplaceOrderLine.objects.create(
            order=self.order, marketplace_sku="FSN-UNKNOWN", ordered_quantity=Decimal("1")
        )
        resolved = resolve_service.resolve_order(self.order)
        self.assertEqual(resolved["unmapped_skus"], ["FSN-UNKNOWN"])

    # ── scanning ─────────────────────────────────────────────────────────────
    def test_duplicate_scan_flagged(self):
        d = self._new_dispatch()
        _, created1, dup1 = record_dispatch_scan(d, barcode_raw="BC1", item_code="FG-A", user=self.user)
        _, created2, dup2 = record_dispatch_scan(d, barcode_raw="BC1", item_code="FG-A", user=self.user)
        self.assertTrue(created1)
        self.assertFalse(dup1)
        self.assertFalse(created2)
        self.assertTrue(dup2)

    def test_over_scan_rejected(self):
        d = self._new_dispatch()
        record_dispatch_scan(d, barcode_raw="A1", item_code="FG-A", user=self.user)
        record_dispatch_scan(d, barcode_raw="A2", item_code="FG-A", user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            record_dispatch_scan(d, barcode_raw="A3", item_code="FG-A", user=self.user)
        self.assertEqual(ctx.exception.code, "OVER_SCAN")

    def test_item_not_on_order_rejected(self):
        d = self._new_dispatch()
        with self.assertRaises(MarketplaceError) as ctx:
            record_dispatch_scan(d, barcode_raw="X1", item_code="FG-NOPE", user=self.user)
        self.assertEqual(ctx.exception.code, "ITEM_NOT_ON_ORDER")

    def _fully_scan(self, d):
        record_dispatch_scan(d, barcode_raw="A1", item_code="FG-A", user=self.user)
        record_dispatch_scan(d, barcode_raw="A2", item_code="FG-A", user=self.user)
        record_dispatch_scan(d, barcode_raw="O1", item_code="FG-OIL-3L", user=self.user)
        record_dispatch_scan(d, barcode_raw="O2", item_code="FG-OIL-3L", user=self.user)
        record_dispatch_scan(d, barcode_raw="O3", item_code="FG-OIL-3L", user=self.user)

    def test_full_scan_marks_ready(self):
        d = self._new_dispatch()
        self._fully_scan(d)
        d.refresh_from_db()
        self.assertEqual(d.status, MarketplaceDispatchStatus.READY)
        self.assertTrue(is_fully_scanned(dispatch_progress(d)))

    # ── confirm ──────────────────────────────────────────────────────────────
    def test_confirm_creates_delivery_note_and_billing(self):
        d = self._new_dispatch()
        self._fully_scan(d)
        confirmed = confirm_dispatch(d, user=self.user)
        self.assertEqual(confirmed.status, MarketplaceDispatchStatus.CONFIRMED)
        self.assertEqual(confirmed.sap_delivery_note_num, f"SIMDN-{d.pk}")
        self.assertIsNotNone(confirmed.internal_billing)
        self.assertTrue(confirmed.internal_billing.invoice_number.startswith("MKT-"))
        self.assertEqual(MarketplaceOrderBilling.objects.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, MarketplaceOrderStatus.DISPATCHED)

    def test_confirm_blocked_on_under_scan(self):
        d = self._new_dispatch()
        record_dispatch_scan(d, barcode_raw="A1", item_code="FG-A", user=self.user)
        with self.assertRaises(MarketplaceError) as ctx:
            confirm_dispatch(d, user=self.user)
        # An incomplete scan is now caught by the full-scan gate.
        self.assertEqual(ctx.exception.code, "NOT_SCANNED")

    def test_confirm_override_allows_under_scan(self):
        d = self._new_dispatch()
        record_dispatch_scan(d, barcode_raw="A1", item_code="FG-A", user=self.user)
        confirmed = confirm_dispatch(d, user=self.user, override_deviation=True)
        self.assertEqual(confirmed.status, MarketplaceDispatchStatus.CONFIRMED)
