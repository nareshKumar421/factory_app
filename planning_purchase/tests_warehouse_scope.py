"""Stock is counted per material type, in the production-facing warehouses.

Packaging and oil are not kept in the same places, so scoping them together lets
a packaging item pick up stock from a tank farm and an oil pick up stock from a
carton store. Scoping at all matters more than which list wins: summing the whole
estate treats finished-goods godowns, non-moving stores, job-work locations and
wastage as material production can draw on, which understates every shortage and
under-buys, the expensive direction to be wrong in.
"""

from decimal import Decimal

from django.test import TestCase, override_settings

from .services import warehouse_scope as scope
from .tests import FakeReader, make_plan_service

PM_SCOPE = ["BH-PS", "BH-PC", "BH-PM"]
RM_SCOPE = ["BH-LO", "BH-OT"]

scoped = override_settings(
    PLANNING_PURCHASE_PM_WAREHOUSES=PM_SCOPE,
    PLANNING_PURCHASE_RM_WAREHOUSES=RM_SCOPE,
)


def stock_row(item_code, warehouse, on_hand, group="PACKAGING MATERIAL"):
    return {
        "ItemCode": item_code,
        "WhsCode": warehouse,
        "OnHand": Decimal(on_hand),
        "MinStock": Decimal("0"),
        "Committed": Decimal("0"),
        "OnOrder": Decimal("0"),
        "Uom": "PCS",
        "LastPurchasePrice": Decimal("1"),
        "ItemGroup": group,
        "LastConsumptionDate": None,
        "DaysSinceLastConsumption": None,
    }


@scoped
class ScopeConfigTests(TestCase):
    def test_each_material_type_has_its_own_list(self):
        by_type = scope.scope_by_material_type()
        self.assertEqual(by_type[scope.PACKAGING], PM_SCOPE)
        self.assertEqual(by_type[scope.RAW], RM_SCOPE)

    def test_other_gets_the_union_rather_than_nothing(self):
        """A consumable must not read as zero stock and be ordered in full."""
        self.assertEqual(
            scope.scope_by_material_type()[scope.OTHER], PM_SCOPE + RM_SCOPE
        )

    def test_the_fetch_covers_both_lists(self):
        self.assertEqual(scope.all_scoped_warehouses(), PM_SCOPE + RM_SCOPE)

    @override_settings(
        PLANNING_PURCHASE_PM_WAREHOUSES=[" BH-PM ", "", "BH-PC"],
        PLANNING_PURCHASE_RM_WAREHOUSES=[],
    )
    def test_a_hand_edited_setting_is_tolerated(self):
        self.assertEqual(scope.packaging_warehouses(), ["BH-PM", "BH-PC"])
        self.assertEqual(scope.raw_warehouses(), [])

    def test_counts_respects_the_material_type(self):
        self.assertTrue(scope.counts(scope.PACKAGING, "BH-PM", None))
        self.assertFalse(scope.counts(scope.PACKAGING, "BH-LO", None))
        self.assertTrue(scope.counts(scope.RAW, "BH-LO", None))
        self.assertFalse(scope.counts(scope.RAW, "BH-PC", None))

    def test_an_explicit_filter_replaces_the_scope(self):
        """Asking for one warehouse shows it, rather than an empty table."""
        self.assertTrue(scope.counts(scope.RAW, "BH-PC", ["BH-PC"]))
        self.assertFalse(scope.counts(scope.RAW, "BH-LO", ["BH-PC"]))

    @override_settings(
        PLANNING_PURCHASE_PM_WAREHOUSES=[], PLANNING_PURCHASE_RM_WAREHOUSES=[]
    )
    def test_an_empty_scope_counts_everything(self):
        """The safer failure: reporting no stock would buy the whole plan."""
        self.assertTrue(scope.counts(scope.PACKAGING, "ANYWHERE", None))


@scoped
class RequirementScopeTests(TestCase):
    def test_packaging_counts_only_the_packaging_stores(self):
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("PM0000053", "BH-PM", "5000"),
            # An oil store, a finished-goods godown and wastage. None of them is a
            # place production draws packaging from.
            stock_row("PM0000053", "BH-LO", "999999"),
            stock_row("PM0000053", "BH-FG", "999999"),
            stock_row("PM0000053", "BH-WST", "999999"),
        ]
        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        self.assertEqual(rows["PM0000053"]["on_hand_qty"], Decimal("5000"))

    def test_raw_material_counts_only_the_oil_stores(self):
        """BH-PC holds real oil but is a consumption staging area, not a store."""
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("RM0000002", "BH-LO", "1000", "RAW MATERIAL"),
            stock_row("RM0000002", "BH-OT", "500", "RAW MATERIAL"),
            stock_row("RM0000002", "BH-PC", "999999", "RAW MATERIAL"),
            stock_row("RM0000002", "BH-PM", "999999", "RAW MATERIAL"),
        ]
        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        self.assertEqual(rows["RM0000002"]["on_hand_qty"], Decimal("1500"))

    def test_the_two_types_do_not_borrow_each_others_stores(self):
        """One fetch covers the union, so this guards that it stays split."""
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("PM0000053", "BH-LO", "7777"),
            stock_row("RM0000002", "BH-PM", "8888", "RAW MATERIAL"),
        ]
        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        self.assertEqual(rows["PM0000053"]["on_hand_qty"], Decimal("0"))
        self.assertEqual(rows["RM0000002"]["on_hand_qty"], Decimal("0"))

    def test_an_explicit_filter_overrides_both_lists(self):
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("RM0000002", "BH-LO", "1000", "RAW MATERIAL"),
            stock_row("RM0000002", "BH-PC", "3000", "RAW MATERIAL"),
        ]
        result = make_plan_service(reader).get_requirement(43, warehouses=["BH-PC"])
        rows = {r["component_code"]: r for r in result["data"]}
        self.assertEqual(rows["RM0000002"]["on_hand_qty"], Decimal("3000"))
        self.assertEqual(result["meta"]["warehouse_scope"], {"ALL": ["BH-PC"]})
        self.assertTrue(result["meta"]["warehouse_filtered"])

    def test_the_scope_is_stated_on_the_response(self):
        meta = make_plan_service().get_requirement(43)["meta"]
        self.assertEqual(meta["warehouse_scope"][scope.PACKAGING], PM_SCOPE)
        self.assertEqual(meta["warehouse_scope"][scope.RAW], RM_SCOPE)
        self.assertFalse(meta["warehouse_filtered"])
        self.assertTrue(
            any("BH-LO" in note for note in meta["notes"]),
            "the scope should be named in the notes, not only in a field",
        )


@scoped
class ProducibleScopeTests(TestCase):
    """The two screens must not answer "how much do we have" differently."""

    def test_producible_uses_the_same_per_type_scope(self):
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("PM0000053", "BH-PM", "4000"),
            stock_row("PM0000053", "BH-LO", "999999"),
            stock_row("PM0000013", "BH-PM", "999999"),
            stock_row("RM0000002", "BH-LO", "999999", "RAW MATERIAL"),
        ]
        result = make_plan_service(reader).get_producible(43)
        bottle = next(
            c for c in result["components"] if c["component_code"] == "PM0000053"
        )
        self.assertEqual(bottle["on_hand_qty"], Decimal("4000"))
        self.assertEqual(result["meta"]["warehouse_scope"][scope.PACKAGING], PM_SCOPE)

    def test_raw_material_in_a_packaging_store_is_not_counted(self):
        reader = FakeReader()
        reader.data["stock"] = [
            stock_row("RM0000002", "BH-PC", "999999", "RAW MATERIAL"),
        ]
        result = make_plan_service(reader).get_producible(43)
        oil = next(
            c for c in result["components"] if c["component_code"] == "RM0000002"
        )
        self.assertEqual(oil["on_hand_qty"], Decimal("0"))
