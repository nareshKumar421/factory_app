"""Explaining a committed-stock figure.

The one thing these must not allow is a confident-looking breakdown that does not
add up to `IsCommited`. A partial explanation presented as complete would send a
buyer chasing the wrong documents, so the reconciliation is asserted rather than
assumed.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .services.commitments import STALE_AFTER_DAYS
from .services.errors import PlanningError
from .services.plan_service import PlanService

ZERO = Decimal(0)


def production_row(doc_num, qty, due, issued="0", produces="FG0000030"):
    return {
        "Source": "PRODUCTION_ORDER",
        "DocEntry": doc_num,
        "DocNum": doc_num,
        "DocStatus": "R",
        "RefCode": produces,
        "RefName": "MUSTARD KACHI GHANI 1 LTR 20 PCS",
        "PlannedQty": Decimal(qty) + Decimal(issued),
        "IssuedQty": Decimal(issued),
        "CommittedQty": Decimal(qty),
        "DueDate": due,
        "DocDate": due,
        "ToWarehouse": "",
    }


def transfer_row(doc_num, qty, due, to_wh="BH-PC"):
    return {
        "Source": "TRANSFER_REQUEST",
        "DocEntry": doc_num,
        "DocNum": doc_num,
        "DocStatus": "O",
        "RefCode": to_wh,
        "RefName": "",
        "PlannedQty": Decimal(qty),
        "IssuedQty": ZERO,
        "CommittedQty": Decimal(qty),
        "DueDate": due,
        "DocDate": due,
        "ToWarehouse": to_wh,
    }


class FakeCommitmentReader:
    def __init__(self, stock, documents):
        self._stock = stock
        self._documents = documents

    def get_item_warehouse_stock(self, item_code, warehouse):
        return self._stock

    def get_commitment_breakdown(self, item_code, warehouse):
        return list(self._documents)


def make_service(stock, documents):
    with patch("planning_purchase.services.plan_service.CompanyContext"), \
         patch("planning_purchase.services.plan_service.HanaProductionPlanReader"):
        service = PlanService("JIVO_OIL")
    service.reader = FakeCommitmentReader(stock, documents)
    return service


def stock(on_hand="123018", committed="20666"):
    return {
        "ItemCode": "RM0000003",
        "WhsCode": "BH-LO",
        "OnHand": Decimal(on_hand),
        "IsCommited": Decimal(committed),
        "OnOrder": Decimal("0"),
        "ItemName": "MUSTARD LOOSE OIL",
        "Uom": "LTR",
    }


class CommitmentBreakdownTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.recent = self.today - timedelta(days=5)
        self.ancient = self.today - timedelta(days=644)

    def test_the_documents_are_returned_with_what_they_reserve(self):
        service = make_service(
            stock(committed="18400"),
            [production_row(1124026615, "8400", self.ancient),
             production_row(625926818, "10000", self.recent)],
        )
        result = service.get_commitments("RM0000003", "BH-LO")

        self.assertEqual(result["committed_qty"], Decimal("18400"))
        self.assertEqual(len(result["documents"]), 2)
        self.assertEqual(result["documents"][0]["doc_num"], 1124026615)
        self.assertEqual(result["documents"][0]["reference_code"], "FG0000030")

    def test_a_matching_breakdown_reconciles(self):
        service = make_service(
            stock(committed="18400"),
            [production_row(1, "8400", self.recent),
             production_row(2, "10000", self.recent)],
        )
        meta = service.get_commitments("RM0000003", "BH-LO")["meta"]
        self.assertTrue(meta["reconciles"])
        self.assertEqual(meta["explained_qty"], Decimal("18400"))
        self.assertEqual(meta["unexplained_qty"], ZERO)

    def test_a_gap_is_reported_rather_than_hidden(self):
        """A partial explanation must never look complete.

        SAP recalculates IsCommited on its own schedule, so a gap can be timing
        rather than a missing document type. Either way the reader is told.
        """
        service = make_service(
            stock(committed="20000"), [production_row(1, "8400", self.recent)]
        )
        meta = service.get_commitments("RM0000003", "BH-LO")["meta"]
        self.assertFalse(meta["reconciles"])
        self.assertEqual(meta["unexplained_qty"], Decimal("11600"))

    def test_no_documents_at_all_still_reports_the_gap(self):
        service = make_service(stock(committed="500"), [])
        result = service.get_commitments("RM0000003", "BH-LO")
        self.assertEqual(result["meta"]["document_count"], 0)
        self.assertFalse(result["meta"]["reconciles"])
        self.assertEqual(result["meta"]["unexplained_qty"], Decimal("500"))

    def test_nothing_committed_reconciles_trivially(self):
        service = make_service(stock(committed="0"), [])
        meta = service.get_commitments("RM0000003", "BH-LO")["meta"]
        self.assertTrue(meta["reconciles"])
        self.assertEqual(meta["stale_qty"], ZERO)

    # -- the useful part: which reservations are abandoned ---------------

    def test_an_old_reservation_is_flagged_stale(self):
        """Mustard oil held by an order due Nov 2024 is the headline finding."""
        service = make_service(
            stock(committed="8400"), [production_row(1, "8400", self.ancient)]
        )
        result = service.get_commitments("RM0000003", "BH-LO")
        doc = result["documents"][0]
        self.assertTrue(doc["is_stale"])
        self.assertEqual(doc["days_overdue"], 644)
        self.assertEqual(result["meta"]["stale_qty"], Decimal("8400"))

    def test_a_recent_reservation_is_not_stale(self):
        service = make_service(
            stock(committed="8400"), [production_row(1, "8400", self.recent)]
        )
        result = service.get_commitments("RM0000003", "BH-LO")
        self.assertFalse(result["documents"][0]["is_stale"])
        self.assertEqual(result["meta"]["stale_qty"], ZERO)

    def test_the_staleness_boundary_is_the_configured_window(self):
        just_inside = self.today - timedelta(days=STALE_AFTER_DAYS)
        just_outside = self.today - timedelta(days=STALE_AFTER_DAYS + 1)

        inside = make_service(
            stock(committed="100"), [production_row(1, "100", just_inside)]
        ).get_commitments("RM0000003", "BH-LO")
        self.assertFalse(inside["documents"][0]["is_stale"])

        outside = make_service(
            stock(committed="100"), [production_row(1, "100", just_outside)]
        ).get_commitments("RM0000003", "BH-LO")
        self.assertTrue(outside["documents"][0]["is_stale"])

    def test_a_future_due_date_is_not_overdue(self):
        service = make_service(
            stock(committed="100"),
            [production_row(1, "100", self.today + timedelta(days=30))],
        )
        doc = service.get_commitments("RM0000003", "BH-LO")["documents"][0]
        self.assertEqual(doc["days_overdue"], 0)
        self.assertFalse(doc["is_stale"])

    # -- grouping and ordering ------------------------------------------

    def test_sources_are_totalled_separately(self):
        service = make_service(
            stock(committed="47429"),
            [production_row(1, "47400", self.ancient), transfer_row(2, "29", self.ancient)],
        )
        by_source = {
            row["source"]: row
            for row in service.get_commitments("RM0000003", "BH-LO")["by_source"]
        }
        self.assertEqual(
            by_source["PRODUCTION_ORDER"]["committed_qty"], Decimal("47400")
        )
        self.assertEqual(by_source["TRANSFER_REQUEST"]["committed_qty"], Decimal("29"))
        self.assertEqual(by_source["TRANSFER_REQUEST"]["document_count"], 1)

    def test_a_transfer_names_the_receiving_warehouse(self):
        service = make_service(
            stock(committed="16000"), [transfer_row(226656535, "16000", self.recent)]
        )
        doc = service.get_commitments("RM0000003", "BH-LO")["documents"][0]
        self.assertEqual(doc["source_label"], "Transfer request")
        self.assertEqual(doc["to_warehouse"], "BH-PC")

    def test_documents_are_ordered_oldest_due_first(self):
        service = make_service(
            stock(committed="300"),
            [production_row(1, "100", self.today),
             production_row(2, "100", self.ancient),
             production_row(3, "100", self.recent)],
        )
        due_dates = [
            d["due_date"] for d in service.get_commitments("RM0000003", "BH-LO")["documents"]
        ]
        self.assertEqual(due_dates, sorted(due_dates))

    def test_free_stock_is_reported_alongside(self):
        service = make_service(stock(on_hand="123018", committed="20666"), [])
        result = service.get_commitments("RM0000003", "BH-LO")
        self.assertEqual(result["free_qty"], Decimal("102352"))

    # -- input handling -------------------------------------------------

    def test_a_missing_stock_record_is_a_404(self):
        service = make_service(None, [])
        with self.assertRaises(PlanningError) as ctx:
            service.get_commitments("NOPE", "BH-LO")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_both_arguments_are_required(self):
        service = make_service(stock(), [])
        for item, warehouse in (("", "BH-LO"), ("RM0000003", ""), ("  ", " ")):
            with self.subTest(item=item, warehouse=warehouse):
                with self.assertRaises(PlanningError):
                    service.get_commitments(item, warehouse)
