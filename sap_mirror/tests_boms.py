"""The production BOM copy: what starting a run needs, when HANA cannot be asked.

No HANA. Taking the copy runs a fake fetch; serving it runs the real
``ProductionOrderReader`` with ``HanaConnection.connect`` failing the way an
unreachable HANA does.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi

from company.models import Company
from production_execution.models import ProductionLine
from production_execution.services.production_service import ProductionExecutionService
from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError
from sap_client.hana.connection import HanaConnection

from . import services
from .models import MirrorRow

NOW = timezone.make_aware(datetime(2026, 10, 1, 1, 5))
HANA_DOWN = dbapi.OperationalError(-10709, "Connection failed (rc=111:Connection refused)")


def bom_row(code, name, components, pieces=20, litres=1.0):
    return {
        "item": {"ItemCode": code, "ItemName": name, "UomCode": "PCS"},
        "bom": components,
        "pieces_per_case": pieces,
        "litres_per_piece": litres,
    }


def component(code, name, planned, uom="PCS", price="6.50"):
    return {
        "ItemCode": code, "ItemName": name, "PlannedQty": Decimal(planned),
        "BomQty": Decimal(planned), "BomBaseQty": Decimal("20"),
        "PiecesPerCase": Decimal("20"), "UomCode": uom, "Warehouse": "BH-PC",
        "UnitPrice": Decimal(price),
    }


CANOLA = bom_row("FG0000121", "CANOLA OIL 1 LTR 20 PCS", [
    component("PM0000001", "PET BOTTLE 1 LTR", "20"),
    component("RM0000002", "CANOLA OIL LOOSE", "18.2", uom="KG", price="142.75"),
])
MUSTARD = bom_row("FG0000329", "MUSTARD OIL 5 LTR 4 PCS", [
    component("PM0000050", "TIN 5 LTR", "4"),
], pieces=4, litres=5.0)


class ProductionBomCopyTestCase(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        patcher = patch(
            "sap_mirror.services._fetch_production_boms", return_value=[CANOLA, MUSTARD]
        )
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)
        services.refresh(self.oil, services.PRODUCTION_BOMS, NOW)

    def hana_down(self):
        return patch.object(HanaConnection, "connect", side_effect=HANA_DOWN)


class TakingTheBomCopyTests(ProductionBomCopyTestCase):
    def test_every_run_startable_item_is_copied_with_its_bom(self):
        rows = MirrorRow.objects.filter(dataset__name=services.PRODUCTION_BOMS)
        self.assertEqual(sorted(rows.values_list("key", flat=True)), ["FG0000121", "FG0000329"])

    def test_it_is_taken_once_a_night(self):
        state = services.DATASETS[services.PRODUCTION_BOMS]
        self.assertIsNone(state.every)


class ServingTheBomCopyTests(ProductionBomCopyTestCase):
    def setUp(self):
        super().setUp()
        self.reader = ProductionOrderReader("JIVO_OIL")

    def test_the_run_item_search_comes_from_the_copy(self):
        with self.hana_down():
            found = self.reader.search_items(search="oil", limit=50, produced_only=True)
            only_one = self.reader.search_items(search="mustard", limit=50, produced_only=True)

        # SAP's own order, by item name.
        self.assertEqual([i["ItemCode"] for i in found], ["FG0000121", "FG0000329"])
        self.assertEqual([i["ItemCode"] for i in only_one], ["FG0000329"])

    def test_a_search_of_all_items_is_not_copied(self):
        with self.hana_down(), self.assertRaises(SAPReadError):
            self.reader.search_items(search="oil", limit=50, produced_only=False)

    def test_a_bom_keeps_its_quantities_exact(self):
        with self.hana_down():
            bom = self.reader.get_bom_by_item_code("FG0000121")

        self.assertEqual([c["ItemCode"] for c in bom], ["PM0000001", "RM0000002"])
        self.assertEqual(bom[1]["PlannedQty"], Decimal("18.2"))
        self.assertEqual(bom[1]["UnitPrice"], Decimal("142.75"))

    def test_an_item_the_copy_does_not_hold_is_still_sap_being_down(self):
        with self.hana_down(), self.assertRaises(SAPReadError):
            self.reader.get_bom_by_item_code("FG0099999")

    def test_pieces_per_case_and_litres_come_from_the_copy(self):
        with self.hana_down():
            pieces = self.reader.get_pieces_per_case_map(["FG0000121", "FG0000329"])
            litres = self.reader.get_litres_per_piece_map(["FG0000329"])

        self.assertEqual(pieces, {"FG0000121": 20, "FG0000329": 4})
        self.assertEqual(litres, {"FG0000329": 5.0})

    def test_a_refused_query_is_not_answered_from_the_copy(self):
        class Refusing:
            def cursor(self):
                return self

            def execute(self, *args):
                raise dbapi.ProgrammingError(260, "invalid column name")

            def close(self):
                pass

        with patch.object(HanaConnection, "connect", return_value=Refusing()):
            with self.assertRaises(SAPReadError):
                self.reader.get_bom_by_item_code("FG0000121")

    def test_the_copy_job_never_reads_the_copy(self):
        with self.hana_down(), self.assertRaises(SAPReadError):
            ProductionOrderReader("JIVO_OIL", use_copy=False).get_bom_by_item_code("FG0000121")


class StartingARunWithHanaDownTests(ProductionBomCopyTestCase):
    def test_the_run_opens_with_its_bom_from_the_copy(self):
        line = ProductionLine.objects.create(company=self.oil, name="Line-1")
        user = get_user_model().objects.create(email="line@example.com", full_name="Line Lead")

        with self.hana_down():
            run = ProductionExecutionService("JIVO_OIL").create_run(
                {
                    "line_id": line.id, "date": date(2026, 10, 1), "item_code": "FG0000121",
                    "product": "CANOLA OIL 1 LTR 20 PCS", "required_qty": Decimal("300"),
                    "rated_speed": Decimal("150"),
                },
                user,
            )

        self.assertEqual(run.pieces_per_case, 20)
        materials = {m.material_code: m for m in run.material_usages.all()}
        self.assertEqual(sorted(materials), ["PM0000001", "RM0000002"])
        # An item BOM is per box, so it is scaled by the run's 300 boxes.
        self.assertEqual(materials["RM0000002"].opening_qty, Decimal("5460.0"))
        self.assertEqual(materials["RM0000002"].unit_price, Decimal("142.75"))
