"""The stock list must name an item even when SAP cannot be reached.

The masters rebuilt on 19 Aug were saved during a HANA outage, so 47 of 76 combo
components hold an item code and no name. The warehouse was then asked to pick
against a bare code.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company

from .models import (
    ComboComponent, ComboComponentType, ComboDefinition, MarketplaceChannel,
    MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
    MarketplaceOrderLine, MarketplaceScan, OrderImportBatch, SkuMapping, SkuType,
)
from .services.batch_resolve_service import build_stock_list
from .services.item_names import fill_missing_names, local_item_names

POMACE = "POMACE OLIVE 1 LTR 16 PCS"


class LocalItemNameTests(TestCase):
    channel = MarketplaceChannel.FLIPKART

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = get_user_model().objects.create(
            email="n@t.com", full_name="Ops", employee_code="N1", is_active=True)
        self.batch = OrderImportBatch.objects.create(
            company=self.company, channel=self.channel, filename="n.csv")

    def _combo(self, code, item, name):
        combo = ComboDefinition.objects.create(
            company=self.company, channel=self.channel, code=code, name=code)
        ComboComponent.objects.create(
            combo=combo, component_type=ComboComponentType.FG, item_code=item,
            item_name=name, quantity=Decimal("1"), uom="PCS")
        return combo

    def _order(self, sku, combo, order_id, qty=1):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, marketplace_sku=sku,
            fsn=f"FSN{order_id}", sku_type=SkuType.COMBO, combo=combo)
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=self.channel, order_id=order_id,
            import_batch=self.batch)
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku=sku, fsn=f"FSN{order_id}", ordered_quantity=qty)
        return order

    def test_a_name_is_borrowed_from_another_master_holding_the_same_code(self):
        """One combo names FG0000028; a second, saved during the outage, does not.
        The stock list folds both into one line, which must still be named."""
        self._combo("CB-NAMED", "FG0000028", POMACE)
        nameless = self._combo("CB-BLANK", "FG0000028", "")
        self._order("SKU-BLANK", nameless, "OD-1")

        line = next(l for l in build_stock_list(self.batch)["lines"]
                    if l["item_code"] == "FG0000028")
        self.assertEqual(line["item_name"], POMACE)

    def test_a_scan_names_a_code_no_master_does(self):
        nameless = self._combo("CB-ONLY", "FG0000005", "")
        order = self._order("SKU-ONLY", nameless, "OD-2")
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED)
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="T#FG0000005",
            item_code="FG0000005", item_name="EXTRA LIGHT OLIVE 1 LTR 16 PCS",
            component_type=ComboComponentType.FG, quantity=Decimal("1"))

        line = next(l for l in build_stock_list(self.batch)["lines"]
                    if l["item_code"] == "FG0000005")
        self.assertEqual(line["item_name"], "EXTRA LIGHT OLIVE 1 LTR 16 PCS")

    def test_a_master_name_beats_a_scan(self):
        """Masters were checked against OITM when written; a scan is floor data."""
        self._combo("CB-M", "FG0000028", POMACE)
        nameless = self._combo("CB-N", "FG0000028", "")
        order = self._order("SKU-M", nameless, "OD-3")
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED)
        MarketplaceScan.objects.create(
            company=self.company, dispatch=dispatch, barcode_raw="T#FG0000028",
            item_code="FG0000028", item_name="stale floor label",
            component_type=ComboComponentType.FG, quantity=Decimal("1"))

        line = next(l for l in build_stock_list(self.batch)["lines"]
                    if l["item_code"] == "FG0000028")
        self.assertEqual(line["item_name"], POMACE)

    def test_a_code_nothing_has_ever_named_stays_blank(self):
        """Better an empty cell than an invented name."""
        nameless = self._combo("CB-U", "FG9999999", "")
        self._order("SKU-U", nameless, "OD-4")
        line = next(l for l in build_stock_list(self.batch)["lines"]
                    if l["item_code"] == "FG9999999")
        self.assertEqual(line["item_name"], "")

    def test_the_marketplace_title_is_never_used_as_a_sap_name(self):
        nameless = self._combo("CB-T", "FG0000042", "")
        order = self._order("SKU-T", nameless, "OD-5")
        order.lines.update(sku_name="Jivo Extra Virgin Olive Oil Combo (Flipkart)")
        line = next(l for l in build_stock_list(self.batch)["lines"]
                    if l["item_code"] == "FG0000042")
        self.assertEqual(line["item_name"], "")

    def test_a_fully_named_list_queries_nothing(self):
        lines = [{"item_code": "FG0000028", "item_name": POMACE}]
        with self.assertNumQueries(0):
            fill_missing_names(lines)
        self.assertEqual(lines[0]["item_name"], POMACE)

    def test_lookup_is_case_and_space_insensitive(self):
        self._combo("CB-C", "FG0000028", POMACE)
        self.assertEqual(local_item_names([" fg0000028 "])["FG0000028"], POMACE)
