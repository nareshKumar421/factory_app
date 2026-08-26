"""The Purchase-from-plan screen counts stock only in production warehouses.

Summing the whole estate treats finished-goods godowns, non-moving stores,
job-work locations and wastage as material production can draw on. That
understates every shortage and under-buys, which is the expensive direction to be
wrong in.
"""

from decimal import Decimal

from django.test import TestCase, override_settings

from .services.plan_service import default_stock_warehouses
from .tests import FakeReader, make_plan_service

SCOPE = ["BH-PC", "BH-PS", "BH-PM"]


def stock_row(item_code, warehouse, on_hand):
    return {
        "ItemCode": item_code,
        "WhsCode": warehouse,
        "OnHand": Decimal(on_hand),
        "MinStock": Decimal("0"),
        "Committed": Decimal("0"),
        "OnOrder": Decimal("0"),
        "Uom": "PCS",
        "LastPurchasePrice": Decimal("1"),
        "ItemGroup": "PACKAGING MATERIAL",
        "LastConsumptionDate": None,
        "DaysSinceLastConsumption": None,
    }


class DefaultWarehouseScopeTests(TestCase):
    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=SCOPE)
    def test_the_setting_is_read(self):
        self.assertEqual(default_stock_warehouses(), SCOPE)

    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=[" BH-PC ", "", "BH-PM"])
    def test_blanks_and_padding_are_tolerated(self):
        """A hand-edited .env should not silently produce a phantom warehouse."""
        self.assertEqual(default_stock_warehouses(), ["BH-PC", "BH-PM"])

    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=SCOPE)
    def test_requirement_counts_only_the_scoped_warehouses(self):
        """Stock outside the scope must not reduce the shortage.

        The reader filters by warehouse, so the assertion is that the service
        passes the scope down rather than defaulting to everything.
        """
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("PM0000053", "BH-PM", "5000"),
            # A finished-goods godown and a wastage store. Neither is material
            # production can draw on, and neither is in scope.
            stock_row("PM0000053", "BH-FG", "999999"),
            stock_row("PM0000053", "BH-WST", "999999"),
        ]
        result = make_plan_service(reader).get_requirement(43)
        rows = {r["component_code"]: r for r in result["data"]}

        self.assertEqual(rows["PM0000053"]["on_hand_qty"], Decimal("5000"))
        self.assertEqual(result["meta"]["warehouse_scope"], SCOPE)

    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=SCOPE)
    def test_an_explicit_filter_still_wins(self):
        """A user narrowing the warehouses must override the default, not add to it."""
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("PM0000053", "BH-PM", "5000"),
            stock_row("PM0000053", "BH-PC", "3000"),
        ]
        result = make_plan_service(reader).get_requirement(43, warehouses=["BH-PC"])

        rows = {r["component_code"]: r for r in result["data"]}
        self.assertEqual(rows["PM0000053"]["on_hand_qty"], Decimal("3000"))
        self.assertEqual(result["meta"]["warehouse_scope"], ["BH-PC"])

    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=SCOPE)
    def test_the_scope_is_stated_on_the_response(self):
        """The reader has to be able to see which stores the number came from."""
        result = make_plan_service().get_requirement(43)
        self.assertEqual(result["meta"]["warehouse_scope"], SCOPE)
        self.assertTrue(
            any("BH-PC" in note for note in result["meta"]["notes"]),
            "the scope should be named in the notes, not just in a field",
        )

    @override_settings(PLANNING_PURCHASE_STOCK_WAREHOUSES=[])
    def test_an_empty_setting_falls_back_to_every_warehouse(self):
        """Misconfiguration must not silently report zero stock everywhere.

        Reporting no stock would raise a purchase order for the whole plan;
        counting everything is the safer failure and is visible in the meta.
        """
        result = make_plan_service().get_requirement(43)
        self.assertEqual(result["meta"]["warehouse_scope"], "ALL")
