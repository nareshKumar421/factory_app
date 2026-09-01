"""Tests for the bill summary.

SAP is stubbed. The contract it enforces was established against the live Service
Layer (see the `bill_summary_service` docstring) and is pinned here as the shape
of the payload we send, not by calling SAP again.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from accounts.models import User
from company.models import Company
from dispatch_plans.bill_summary_service import BillSummaryError, BillSummaryService
from dispatch_plans.models import DispatchPlan
from dispatch_plans.models_bill_summary import (
    BillSummary,
    BillSummarySapStatus,
    BillSummaryStatus,
)

DOC_ENTRY = 5101
DOC_NUM = "626080596"
BILL_DATE = date(2026, 9, 1)
DISPATCH_DATE = date(2026, 9, 5)


def sap_line(line_num=0, item="FG1", whs="GP-FG", qty="10", pcs_per_box="10",
             litres="50", bilty="", dispatch_date=None, gross_weight="0"):
    return {
        "doc_entry": DOC_ENTRY,
        "doc_num": DOC_NUM,
        "card_code": "C1",
        "card_name": "Goel Brothers",
        "line_num": line_num,
        "item_code": item,
        "item_name": f"Item {item}",
        "uom": "PCS",
        "warehouse_code": whs,
        "quantity": Decimal(qty),
        "pcs_per_box": Decimal(pcs_per_box),
        "boxes": Decimal(qty) / Decimal(pcs_per_box) if Decimal(pcs_per_box) else Decimal("0"),
        "litres": Decimal(litres),
        "gross_weight": Decimal(gross_weight),
        # The box/loose split is driven by SalFactor2, not by a precomputed
        # pieces-per-box: SalFactor2 = 1 means the item is not boxed at all.
        "sal_factor2": Decimal(pcs_per_box),
        "dispatched_qty": Decimal("0"),
        "sap_dispatch_date": dispatch_date,
        "sap_bilty_no": bilty,
    }


# A sentinel, so a test can stub "SAP found nothing" with bill=None and still
# tell that apart from "use the default bill".
_DEFAULT = object()


class _Reader:
    """Stands in for the HANA reader."""

    def __init__(self, lines, bill=_DEFAULT):
        self.lines = lines
        self.bill = {
            "doc_entry": DOC_ENTRY, "doc_num": DOC_NUM, "doc_date": BILL_DATE.isoformat(),
            "card_code": "C1", "card_name": "Goel Brothers",
            "ship_to_address": "HASTBAST 89 VILLAGE BHATTIAN GT LUDHIANA PB 141008",
            "doc_total": 1630020.0, "branch_id": 2, "branch_name": "FACTORY",
        } if bill is _DEFAULT else bill

    def get_bill_by_number(self, number):
        return self.bill

    def list_pickable_lines(self, doc_entries):
        return list(self.lines)

    def branch_gstin(self, branch_id):
        return "06AACCJ4223F1Z0"


class BillSummaryTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.user = User.objects.create_user(
            email="mgr@example.com", full_name="Manager", employee_code="E1", password="x"
        )
        self.service = BillSummaryService("JIVO_OIL", self.user)

    def stub(self, lines=None, bill=_DEFAULT):
        reader = _Reader(lines if lines is not None else [sap_line()], bill)
        return patch.object(
            BillSummaryService, "reader",
            new_callable=lambda: property(lambda self: reader),
        )

    def make_plan(self, **kwargs):
        defaults = dict(
            company=self.company,
            sap_invoice_doc_entry=DOC_ENTRY,
            sap_invoice_doc_num=DOC_NUM,
            dispatch_date=DISPATCH_DATE,
            bilty_no="BLT-900",
        )
        defaults.update(kwargs)
        return DispatchPlan.objects.create(**defaults)

    def generate(self, **overrides):
        data = dict(
            sap_invoice_doc_entry=DOC_ENTRY,
            sap_invoice_doc_num=DOC_NUM,
            dispatch_date=DISPATCH_DATE,
            bilty_no="BLT-900",
        )
        data.update(overrides)
        with patch.object(BillSummaryService, "post_to_sap"):
            return self.service.generate(data)


class LookupTests(BillSummaryTestBase):
    def test_finds_the_bill_and_its_lines(self):
        with self.stub([sap_line(), sap_line(line_num=1, item="FG2")]):
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["doc_num"], DOC_NUM)
        self.assertEqual(found["customer_name"], "Goel Brothers")
        self.assertEqual(len(found["lines"]), 2)
        self.assertEqual(found["warehouse_codes"], ["GP-FG"])

    def test_prefills_from_the_dispatch_plan(self):
        self.make_plan()
        with self.stub():
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["prefill"]["dispatch_date"], DISPATCH_DATE)
        self.assertEqual(found["prefill"]["bilty_no"], "BLT-900")
        self.assertTrue(found["has_plan"])

    def test_names_the_bilty_as_missing_when_the_plan_has_none(self):
        """The usual case: the bilty is raised after loading, and this runs
        before it. The user should see that here, not at posting time."""
        self.make_plan(bilty_no="")
        with self.stub():
            found = self.service.lookup(DOC_NUM)
        self.assertIn("bilty_no", found["missing"])
        self.assertNotIn("dispatch_date", found["missing"])

    def test_falls_back_to_what_sap_already_holds(self):
        """A bill somebody already touched by hand in SAP."""
        with self.stub([sap_line(bilty="SAP-BLT-7", dispatch_date=DISPATCH_DATE)]):
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["prefill"]["bilty_no"], "SAP-BLT-7")
        self.assertEqual(found["prefill"]["dispatch_date"], DISPATCH_DATE)
        self.assertEqual(found["missing"], [])

    def test_the_driver_comes_from_the_gate_entry_when_the_plan_has_none(self):
        """Planning books a vehicle but hardly ever a driver — the driver is only
        known when the truck turns up and the gate records it. Reading only
        plan.driver left this blank on nearly every sheet."""
        from driver_management.models import Driver, VehicleEntry
        from vehicle_management.models import Vehicle

        vehicle = Vehicle.objects.create(vehicle_number="UP14ST3265")
        driver = Driver.objects.create(
            name="Sonu", mobile_no="1234567890", license_no="DL-1"
        )
        entry = VehicleEntry.objects.create(
            company=self.company, vehicle=vehicle, driver=driver,
            entry_type="SALES_DISPATCH",
        )
        self.make_plan(driver=None, linked_vehicle_entry=entry)

        with self.stub():
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["prefill"]["driver_name"], "Sonu")
        self.assertEqual(found["prefill"]["driver_mobile"], "1234567890")

    def test_the_plan_wins_over_sap(self):
        self.make_plan(bilty_no="PLAN-BLT")
        with self.stub([sap_line(bilty="SAP-BLT")]):
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["prefill"]["bilty_no"], "PLAN-BLT")

    def test_flags_a_bill_that_already_has_a_summary(self):
        self.make_plan()
        with self.stub():
            summary = self.generate()
            found = self.service.lookup(DOC_NUM)
        self.assertEqual(found["existing_summary"], summary.entry_no)

    def test_unknown_bill_is_refused_clearly(self):
        with self.stub(bill=None):
            with self.assertRaises(BillSummaryError) as ctx:
                self.service.lookup("NOPE")
        self.assertIn("NOPE", str(ctx.exception))

    def test_empty_search_is_refused(self):
        with self.stub():
            with self.assertRaises(BillSummaryError):
                self.service.lookup("   ")


class GenerateTests(BillSummaryTestBase):
    def test_generates_one_numbered_sheet_for_the_bill(self):
        with self.stub([sap_line(), sap_line(line_num=1, item="FG2")]):
            summary = self.generate()
        self.assertEqual(summary.entry_no, "BS-20260905-001")
        self.assertEqual(summary.sap_invoice_doc_entry, DOC_ENTRY)
        self.assertEqual(summary.customer_name, "Goel Brothers")
        self.assertEqual(summary.warehouse_codes, "GP-FG")
        self.assertEqual(summary.status, BillSummaryStatus.GENERATED)
        self.assertEqual(summary.active_lines.count(), 2)

    def test_dispatch_qty_defaults_to_the_full_billed_quantity(self):
        with self.stub([sap_line(qty="24")]):
            summary = self.generate()
        self.assertEqual(summary.active_lines.first().dispatch_qty, Decimal("24"))

    def test_a_short_dispatch_can_be_stated_per_line(self):
        with self.stub([sap_line(qty="24")]):
            summary = self.generate(lines=[{"sap_line_num": 0, "dispatch_qty": "20"}])
        row = summary.active_lines.first()
        self.assertEqual(row.dispatch_qty, Decimal("20"))
        self.assertTrue(row.is_short)

    def test_cannot_dispatch_more_than_the_bill(self):
        with self.stub([sap_line(qty="10")]):
            with self.assertRaises(BillSummaryError) as ctx:
                self.generate(lines=[{"sap_line_num": 0, "dispatch_qty": "11"}])
        self.assertIn("only for", str(ctx.exception))

    def test_a_bilty_is_required_because_sap_demands_one(self):
        with self.stub():
            with self.assertRaises(BillSummaryError) as ctx:
                self.generate(bilty_no="")
        self.assertIn("SAP will not accept", str(ctx.exception))

    def test_a_dispatch_date_before_the_bill_date_is_refused(self):
        """SAP rule 1300014 — caught here rather than at posting."""
        with self.stub():
            with self.assertRaises(BillSummaryError) as ctx:
                self.generate(dispatch_date=date(2026, 8, 20))
        self.assertIn("before the bill", str(ctx.exception))

    def test_a_second_live_sheet_for_the_same_bill_is_refused(self):
        with self.stub():
            first = self.generate()
            with self.assertRaises(BillSummaryError) as ctx:
                self.generate()
        self.assertIn(first.entry_no, str(ctx.exception))

    def test_cancelling_frees_the_bill_for_a_reissue(self):
        with self.stub():
            first = self.generate()
            self.service.cancel(first.id, "wrong vehicle")
            second = self.generate()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.status, BillSummaryStatus.GENERATED)

    def test_transport_details_are_carried_onto_the_sheet(self):
        with self.stub():
            summary = self.generate(
                transporter_name="Arnav Transport",
                vehicle_no="DL01LY5728",
                driver_name="Ramesh",
                driver_mobile="9876543210",
            )
        self.assertEqual(summary.transporter_name, "Arnav Transport")
        self.assertEqual(summary.vehicle_no, "DL01LY5728")

    def test_a_bill_spanning_two_warehouses_still_gets_one_sheet(self):
        """Rare, but it must not silently drop half the bill."""
        with self.stub([sap_line(), sap_line(line_num=1, item="FG2", whs="BH-PF")]):
            summary = self.generate()
        self.assertEqual(summary.warehouse_codes, "BH-PF, GP-FG")
        self.assertEqual(summary.active_lines.count(), 2)


class SapPostingTests(BillSummaryTestBase):
    def test_the_payload_carries_what_sap_demands(self):
        """Date, bilty and per-line dispatch qty — SAP refuses any subset."""
        with self.stub([sap_line(qty="10")]):
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice") as patched:
                self.service.post_to_sap(summary.id)
        patched.assert_called_once()
        sent = patched.call_args[0][0]
        self.assertEqual(sent.bilty_no, "BLT-900")
        self.assertEqual(sent.dispatch_date, DISPATCH_DATE)
        self.assertEqual([l.dispatch_qty for l in sent.active_lines], [Decimal("10")])

    def test_a_successful_post_is_recorded(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice"):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)
        self.assertIsNotNone(summary.sap_posted_at)
        self.assertEqual(summary.sap_error, "")

    def test_a_refused_post_keeps_the_sheet_and_records_why(self):
        """The manager still needs something to hand the floor; a refusal is a
        thing to retry, not a reason to lose the document."""
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice",
                              side_effect=BillSummaryError("(1300012) dispatch qty")):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.status, BillSummaryStatus.GENERATED)
        self.assertEqual(summary.sap_status, BillSummarySapStatus.FAILED)
        self.assertIn("1300012", summary.sap_error)

    def test_posting_can_be_retried_after_a_failure(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice",
                              side_effect=BillSummaryError("boom")):
                self.service.post_to_sap(summary.id)
            with patch.object(BillSummaryService, "_patch_invoice"):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)
        self.assertEqual(summary.sap_error, "")

    def test_a_cancelled_sheet_clears_rather_than_stamps(self):
        """Reconciling a cancelled sheet with SAP means taking the stamp OFF.
        It used to refuse outright, which left the invoice claiming a dispatch
        that had been withdrawn."""
        with self.stub():
            summary = self.generate()
            self.service.cancel(summary.id, "wrong bill")
            with patch.object(BillSummaryService, "_patch_invoice") as patched:
                self.service.post_to_sap(summary.id)
        self.assertTrue(patched.call_args.kwargs["clear"])


class PickAndCancelTests(BillSummaryTestBase):
    def test_marking_picked_records_who_and_when(self):
        with self.stub():
            summary = self.generate()
        picker = User.objects.create_user(
            email="p@example.com", full_name="Picker", employee_code="E2", password="x"
        )
        BillSummaryService("JIVO_OIL", picker).mark_picked(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.status, BillSummaryStatus.PICKED)
        self.assertEqual(summary.picked_by, picker)

    def test_cannot_mark_picked_twice(self):
        with self.stub():
            summary = self.generate()
        self.service.mark_picked(summary.id)
        with self.assertRaises(BillSummaryError):
            self.service.mark_picked(summary.id)

    def test_cancelling_a_posted_sheet_clears_the_stamp_in_sap(self):
        """Leaving a dispatch date on an invoice nobody is dispatching is worse
        than never having written it."""
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice"):
                self.service.post_to_sap(summary.id)
            summary.refresh_from_db()
            self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)

            with patch.object(BillSummaryService, "_patch_invoice") as patched:
                self.service.cancel(summary.id, "load pulled")
        # on_commit does not fire inside a TestCase transaction, so the clearing
        # is driven directly here -- what matters is that it clears, not stamps.
        with self.stub():
            with patch.object(BillSummaryService, "_patch_invoice") as patched:
                self.service.post_to_sap(summary.id)
        self.assertTrue(patched.call_args.kwargs["clear"])
        summary.refresh_from_db()
        self.assertEqual(summary.sap_status, BillSummarySapStatus.NOT_POSTED)
        self.assertIsNone(summary.sap_posted_at)

    def test_a_failed_clearing_still_leaves_the_sheet_cancelled(self):
        """The floor must be able to withdraw a sheet even when SAP is down."""
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice"):
                self.service.post_to_sap(summary.id)
            self.service.cancel(summary.id, "load pulled")
            with patch.object(BillSummaryService, "_patch_invoice",
                              side_effect=BillSummaryError("SAP down")):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.status, BillSummaryStatus.CANCELLED)
        self.assertEqual(summary.sap_status, BillSummarySapStatus.FAILED)
        self.assertIn("SAP down", summary.sap_error)

    def test_cancelling_a_sheet_never_posted_does_not_call_sap(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice") as patched:
                self.service.cancel(summary.id, "wrong bill")
        patched.assert_not_called()

    def test_cancelling_needs_a_reason(self):
        with self.stub():
            summary = self.generate()
        with self.assertRaises(BillSummaryError):
            self.service.cancel(summary.id, "   ")

    def test_totals_foot_up_the_sheet(self):
        with self.stub([sap_line(qty="10", pcs_per_box="10", litres="50", gross_weight="9.5"),
                        sap_line(line_num=1, item="FG2", qty="24", pcs_per_box="12",
                                 litres="24", gross_weight="12.5")]):
            summary = self.generate()
        totals = summary.totals()
        self.assertEqual(totals["lines"], 2)
        self.assertEqual(totals["boxes"], Decimal("3"))  # 1 + 2 full cases
        self.assertEqual(totals["litres"], Decimal("74"))
        self.assertEqual(totals["gross_weight"], Decimal("22"))

    def test_a_part_case_is_loose_pieces_not_a_fraction_of_a_box(self):
        """SAP prints full boxes and leftover pieces separately. Printing
        "0.25 box" would send a picker looking for a quarter of a carton."""
        with self.stub([sap_line(qty="4", pcs_per_box="16")]):
            summary = self.generate()
        row = summary.active_lines.first()
        self.assertEqual(row.boxes, Decimal("0"))
        self.assertEqual(row.loose_qty, Decimal("4"))

    def test_an_unboxed_item_is_entirely_loose(self):
        """SalFactor2 = 1 means the item is not transacted in boxes at all."""
        with self.stub([sap_line(qty="500", pcs_per_box="1")]):
            summary = self.generate()
        row = summary.active_lines.first()
        self.assertEqual(row.boxes, Decimal("0"))
        self.assertEqual(row.loose_qty, Decimal("500"))

    def test_the_bill_header_is_snapshotted_for_the_printed_sheet(self):
        with self.stub():
            summary = self.generate()
        self.assertEqual(summary.bill_amount, Decimal("1630020.00"))
        self.assertEqual(summary.invoice_date, BILL_DATE)
        self.assertIn("LUDHIANA", summary.delivery_address)
        self.assertEqual(summary.branch_gstin, "06AACCJ4223F1Z0")
