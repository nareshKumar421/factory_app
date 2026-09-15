"""Tests for the bill summary.

SAP is stubbed. The contract it enforces was established against the live Service
Layer (see the `bill_summary_service` docstring) and is pinned here as the shape
of the payload we send, not by calling SAP again.
"""

from datetime import date
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from accounts.models import User
from company.models import Company
from dispatch_plans.bill_summary_service import BillSummaryError, BillSummaryService
from dispatch_plans.hana_reader import HanaDispatchBillReader
from dispatch_plans.models import DispatchPlan
from dispatch_plans.models_bill_summary import (
    APP_SOURCE,
    SAP_SOURCE,
    BillSummary,
    BillSummarySapStatus,
    BillSummaryStatus,
)
from dispatch_plans.serializers_bill_summary import BillSummaryListSerializer

DOC_ENTRY = 5101
DOC_NUM = "626080596"
BILL_DATE = date(2026, 9, 1)
DISPATCH_DATE = date(2026, 9, 5)


def sap_line(line_num=0, item="FG1", whs="GP-FG", qty="10", pcs_per_box="10",
             litres="50", bilty="", dispatch_date=None, gross_weight="0",
             sal_factor3="0"):
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
        # SalFactor3 > 1 is SAP's own marker for a line billed in whole cartons
        # (all CSD stock, plus the three REFINED OIL codes whose names omit the
        # token). 0 is the ordinary item.
        "sal_factor3": Decimal(sal_factor3),
        "dispatched_qty": Decimal("0"),
        "sap_dispatch_date": dispatch_date,
        "sap_bilty_no": bilty,
    }


# A sentinel, so a test can stub "SAP found nothing" with bill=None and still
# tell that apart from "use the default bill".
_DEFAULT = object()


def stamped_bill(**overrides):
    """An invoice carrying a dispatch stamp, as `list_stamped_bills` returns it."""
    row = {
        "doc_entry": DOC_ENTRY,
        "doc_num": DOC_NUM,
        "doc_date": BILL_DATE.isoformat(),
        "card_code": "C1",
        "card_name": "Goel Brothers",
        "doc_total": Decimal("1630020"),
        "branch_id": 2,
        "branch_name": "FACTORY",
        "ship_to_address": "HASTBAST 89 VILLAGE BHATTIAN GT LUDHIANA PB 141008",
        "dispatch_date": DISPATCH_DATE.isoformat(),
        "bilty_no": "NCR-4494",
        "bilty_date": None,
        "transporter_name": "Pick & Ship",
        "vehicle_no": "HR67D6673",
        "driver_name": "",
        "driver_mobile": "",
        "line_count": 1,
        "total_boxes": Decimal("1"),
        "total_litres": Decimal("50"),
        "warehouses": "GP-FG",
    }
    row.update(overrides)
    return row


class _Reader:
    """Stands in for the HANA reader."""

    def __init__(self, lines, bill=_DEFAULT, stamped=None, state=_DEFAULT):
        self.lines = lines
        self.stamped = stamped if stamped is not None else [stamped_bill()]
        self.state = (
            {"doc_num": DOC_NUM, "is_cancelled": False} if state is _DEFAULT else state
        )
        self.bill = {
            "doc_entry": DOC_ENTRY, "doc_num": DOC_NUM, "doc_date": BILL_DATE.isoformat(),
            "card_code": "C1", "card_name": "Goel Brothers",
            "ship_to_address": "HASTBAST 89 VILLAGE BHATTIAN GT LUDHIANA PB 141008",
            "doc_total": 1630020.0, "branch_id": 2, "branch_name": "FACTORY",
        } if bill is _DEFAULT else bill

    def get_bill_by_number(self, number):
        return self.bill

    def list_stamped_bills(self, filters):
        doc_entry = filters.get("doc_entry")
        if doc_entry:
            return [row for row in self.stamped if row["doc_entry"] == int(doc_entry)]
        return list(self.stamped)

    def list_pickable_lines(self, doc_entries):
        return list(self.lines)

    def invoice_state(self, doc_entry):
        return self.state

    def branch_gstin(self, branch_id):
        return "06AACCJ4223F1Z0"

    def company_legal_name(self):
        return "JIVO WELLNESS PVT LTD"


class BillSummaryTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.user = User.objects.create_user(
            email="mgr@example.com", full_name="Manager", employee_code="E1", password="x"
        )
        self.service = BillSummaryService("JIVO_OIL", self.user)

    def stub(self, lines=None, bill=_DEFAULT, stamped=None, state=_DEFAULT):
        reader = _Reader(
            lines if lines is not None else [sap_line()], bill, stamped, state
        )
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
        self.assertEqual(found["prefill"]["bilty_no"], "BLT-900")
        self.assertTrue(found["has_plan"])

    def test_the_dispatch_date_is_never_prefilled(self):
        """It is written into SAP where it can never be changed again, and a
        plan's date is routinely stale by the time the truck is loaded. A date
        already in the box gets accepted without being read, so it is typed
        every time — even when the plan and SAP both offer one."""
        self.make_plan()
        with self.stub([sap_line(dispatch_date=DISPATCH_DATE)]):
            found = self.service.lookup(DOC_NUM)
        self.assertIsNone(found["prefill"]["dispatch_date"])
        # And not reported as a gap either: it is always typed, so saying it is
        # "not on the plan" would be noise rather than news.
        self.assertNotIn("dispatch_date", found["missing"])

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
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
                self.service.post_to_sap(summary.id)
        patched.assert_called_once()
        sent = patched.call_args[0][0]
        self.assertEqual(sent.bilty_no, "BLT-900")
        self.assertEqual(sent.dispatch_date, DISPATCH_DATE)
        self.assertEqual([l.dispatch_qty for l in sent.active_lines], [Decimal("10")])

    def test_a_successful_post_is_recorded(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])):
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
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])):
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
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
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
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])):
                self.service.post_to_sap(summary.id)
            summary.refresh_from_db()
            self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)

            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
                self.service.cancel(summary.id, "load pulled")
        # on_commit does not fire inside a TestCase transaction, so the clearing
        # is driven directly here -- what matters is that it clears, not stamps.
        with self.stub():
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
                self.service.post_to_sap(summary.id)
        self.assertTrue(patched.call_args.kwargs["clear"])
        summary.refresh_from_db()
        self.assertEqual(summary.sap_status, BillSummarySapStatus.NOT_POSTED)
        self.assertIsNone(summary.sap_posted_at)

    def test_a_failed_clearing_still_leaves_the_sheet_cancelled(self):
        """The floor must be able to withdraw a sheet even when SAP is down."""
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])):
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
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
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

    def test_a_box_billed_line_is_whole_boxes_not_a_loose_piece(self):
        """SAP's own BoxInt tests SalFactor3 > 1 first: the billed unit IS a box.

        FG0000013 (REFINED OIL 1000 MLS, SalFactor3 = 20) invoiced as 1 is one
        sealed 20-bottle carton, and SAP's bill prints it "1 Box". Reading only
        SalFactor2 = 1 made it "0 Box, 1.00 Loose" — the floor sent to pull a
        single bottle out of a carton. The item's name carries no CSD token, so
        the token test could not save it.
        """
        with self.stub([sap_line(qty="1", pcs_per_box="1", sal_factor3="20")]):
            summary = self.generate()
        row = summary.active_lines.first()
        self.assertEqual(row.boxes, Decimal("1"))
        self.assertEqual(row.loose_qty, Decimal("0"))
        # What SAP is told stays in the unit SAP bills in: one carton, not 20.
        self.assertEqual(row.dispatch_qty, Decimal("1"))

    def test_a_csd_carton_line_counts_every_carton_as_a_box(self):
        with self.stub([sap_line(qty="14", pcs_per_box="1", sal_factor3="16")]):
            summary = self.generate()
        row = summary.active_lines.first()
        self.assertEqual(row.boxes, Decimal("14"))
        self.assertEqual(row.loose_qty, Decimal("0"))

    def test_an_unboxed_item_stays_loose_when_sap_marks_no_carton(self):
        """The SalFactor3 rule must not turn every SalFactor2 = 1 line into boxes:
        FG0000381 (500 pcs of a 10 ML bottle) really does ship loose."""
        with self.stub([sap_line(qty="500", pcs_per_box="1", sal_factor3="1")]):
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

    def test_the_legal_entity_is_read_from_sap_not_assumed(self):
        """Oil, Mart and Beverages are not the same company. A sheet naming one
        against another's GST is a document nobody should hand to a driver."""
        with self.stub():
            summary = self.generate()
        self.assertEqual(summary.company_legal_name, "JIVO WELLNESS PVT LTD")


# What each company calls the stamp. Not invented: read off the live OINV of
# each schema. Oil and Mart double the L in Billty and capitalise VehicleNoM;
# Beverages does neither.
OIL_COLUMNS = {
    "dispatch_date": "U_Dipatch_Date",
    "bilty_no": "U_BilltyNumber",
    "bilty_date": "U_BiltyDate",
    "transporter_name": "U_TransporterName",
    "vehicle_no": "U_VehicleNoM",
    "driver_name": "U_DriverName",
    "driver_mobile": "U_Mob_No",
}
BEVERAGES_COLUMNS = {
    **OIL_COLUMNS,
    "bilty_no": "U_BiltyNumber",
    "vehicle_no": "U_VechileNom",
}
EMPTY_STAMP = {field: None for field in OIL_COLUMNS}


class StampPayloadTests(BillSummaryTestBase):
    """What we send SAP, given what SAP already has.

    The seven stamp fields are write-once: SBO_SP_TRANSACTIONNOTIFICATION
    compares the invoice with its own previous version and refuses the WHOLE
    update (1395111-1395117) if one of them changes after it holds a value.
    """

    def summary_for(self, **overrides):
        with self.stub():
            summary = self.generate(**overrides)
        return summary

    def test_the_companys_own_spelling_is_used(self):
        """Oil's spelling at Beverages is dropped by the Service Layer without a
        word, which is how Beverages invoices ended up with no bilty at all."""
        summary = self.summary_for()
        payload, *_ = self.service._stamp_payload(
            summary, BEVERAGES_COLUMNS, dict(EMPTY_STAMP)
        )
        self.assertEqual(payload["U_BiltyNumber"], "BLT-900")
        self.assertNotIn("U_BilltyNumber", payload)

    def test_a_field_sap_already_holds_is_left_alone_and_reported(self):
        summary = self.summary_for()
        payload, kept, _ = self.service._stamp_payload(
            summary, OIL_COLUMNS, {**EMPTY_STAMP, "bilty_no": "1822",
                                   "bilty_date": date(2026, 8, 27)},
        )
        self.assertNotIn("U_BilltyNumber", payload)
        self.assertNotIn("U_BiltyDate", payload)
        # The dispatch date and the quantities still go — they are the point,
        # and one stale bilty date used to take them down with it.
        self.assertEqual(payload["U_Dipatch_Date"], "2026-09-05")
        self.assertEqual(payload["DocumentLines"][0]["U_Disp_Qty"], 10.0)
        self.assertIn("bilty number 1822", kept)

    def test_a_value_sap_already_agrees_with_is_not_reported(self):
        summary = self.summary_for()
        _, kept, _dropped = self.service._stamp_payload(
            summary, OIL_COLUMNS, {**EMPTY_STAMP, "bilty_no": "BLT-900"}
        )
        self.assertEqual(kept, [])

    def test_a_dispatch_date_sap_already_has_is_refused_before_the_write(self):
        """The one field that cannot be quietly skipped: posting a sheet against
        somebody else's dispatch date would be a lie, not a compromise."""
        summary = self.summary_for()
        with self.assertRaises(BillSummaryError) as caught:
            self.service._stamp_payload(
                summary, OIL_COLUMNS, {**EMPTY_STAMP, "dispatch_date": date(2026, 9, 2)}
            )
        self.assertIn("2026-09-02", str(caught.exception))

    def test_the_same_dispatch_date_still_posts(self):
        """A sheet whose date somebody has already typed into SAP by hand is
        exactly the case the retry has to be able to finish."""
        summary = self.summary_for()
        payload, *_ = self.service._stamp_payload(
            summary, OIL_COLUMNS, {**EMPTY_STAMP, "dispatch_date": DISPATCH_DATE}
        )
        self.assertEqual(payload["U_Dipatch_Date"], "2026-09-05")

    def test_a_value_too_long_for_sap_is_left_out_rather_than_losing_the_stamp(self):
        """SAP does not trim: it refuses the whole request over one over-long
        field, and the dispatch date and every line quantity go down with it.
        A live Beverages sheet was stuck this way with a transporter's name typed
        into the driver-mobile box, against a `U_Mob_No` 12 characters wide."""
        summary = self.summary_for(driver_mobile="Arnav Transport")
        payload, _kept, dropped = self.service._stamp_payload(
            summary, BEVERAGES_COLUMNS, dict(EMPTY_STAMP), {"driver_mobile": 12}
        )
        self.assertNotIn("U_Mob_No", payload)
        # What the stamp is actually for still goes.
        self.assertEqual(payload["U_Dipatch_Date"], "2026-09-05")
        self.assertEqual(payload["DocumentLines"][0]["U_Disp_Qty"], 10.0)
        self.assertIn("driver mobile Arnav Transport", dropped)

    def test_a_value_that_fits_is_sent_whole(self):
        """Never truncated: these fields are write-once, so half a phone number
        recorded forever would be worse than none."""
        summary = self.summary_for(driver_mobile="9956958533")
        payload, _kept, dropped = self.service._stamp_payload(
            summary, BEVERAGES_COLUMNS, dict(EMPTY_STAMP), {"driver_mobile": 12}
        )
        self.assertEqual(payload["U_Mob_No"], "9956958533")
        self.assertEqual(dropped, [])

    def test_a_bilty_too_long_is_refused_with_a_message_instead(self):
        """The one that cannot simply be left out: with no bilty number SAP
        demands a receiving attachment we cannot supply, so the post would fail
        anyway — and far less clearly."""
        summary = self.summary_for(bilty_no="BLT-900-0123456789012345")
        with self.assertRaises(BillSummaryError) as caught:
            self.service._stamp_payload(
                summary, OIL_COLUMNS, dict(EMPTY_STAMP), {"bilty_no": 15}
            )
        self.assertIn("15 characters", str(caught.exception))

    def test_what_would_not_fit_is_recorded_on_the_sheet(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice",
                              return_value=([], ["driver mobile Arnav Transport"])):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        # The posting worked; the sheet just says what the invoice does not carry.
        self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)
        self.assertIn("driver mobile Arnav Transport", summary.sap_note)

    def test_what_sap_kept_is_recorded_on_the_sheet(self):
        with self.stub():
            summary = self.generate()
            with patch.object(BillSummaryService, "_patch_invoice",
                              return_value=(["bilty number 1822"], [])):
                self.service.post_to_sap(summary.id)
        summary.refresh_from_db()
        self.assertEqual(summary.sap_status, BillSummarySapStatus.POSTED)
        self.assertIn("bilty number 1822", summary.sap_note)


class InvoicePrintTests(BillSummaryTestBase):
    """Printing the BILL from the sheet — SAP's own TAX INVOICE.

    The summary is the floor's picking sheet; this is the document the customer
    gets, and it is SAP's, not ours. So nothing here is built: the invoice is
    read from SAP every time, and the only decisions this module makes are
    whether there is a live bill to print at all.
    """

    def print_bill(self, state=_DEFAULT, payload=_DEFAULT):
        bill = {"doc_num": DOC_NUM, "lines": []} if payload is _DEFAULT else payload
        client = SimpleNamespace(ar_invoice_print=lambda doc_entry: bill)
        self.client_calls = []

        def factory(company_code):
            self.client_calls.append(company_code)
            return client

        with self.stub(state=state):
            with patch("dispatch_plans.bill_summary_service.SAPClient", factory):
                return self.service.invoice_print_payload(DOC_ENTRY)

    def test_the_bill_is_read_from_sap_for_this_company(self):
        payload = self.print_bill()
        self.assertEqual(payload["doc_num"], DOC_NUM)
        self.assertEqual(self.client_calls, ["JIVO_OIL"])

    def test_an_invoice_this_company_does_not_have_is_refused(self):
        with self.assertRaises(BillSummaryError) as caught:
            self.print_bill(state=None)
        self.assertIn(str(DOC_ENTRY), str(caught.exception))

    def test_a_cancelled_invoice_is_not_printed(self):
        """A cancelled bill on the TAX INVOICE layout looks live — the sheet
        carries nothing to say otherwise, so it is refused here instead."""
        with self.assertRaises(BillSummaryError) as caught:
            self.print_bill(state={"doc_num": DOC_NUM, "is_cancelled": True})
        self.assertIn("cancelled", str(caught.exception))

    def test_a_bill_sap_cannot_produce_is_reported_rather_than_printed_empty(self):
        with self.assertRaises(BillSummaryError):
            self.print_bill(payload=None)


class PickableBoxExprTests(SimpleTestCase):
    """The SalFactor3 rule belongs to the picking sheet and nothing else.

    The dispatch dashboard and docking read boxes through ``_box_pieces_expr``,
    and their scan locks are calibrated on those counts. Widening the shared
    expression would have moved them too, so this pins the boundary.
    """

    COLUMNS = {"SalFactor2", "SalFactor3", "ItemName"}

    def test_the_picking_sheet_counts_a_sap_marked_carton_as_a_box(self):
        expr = HanaDispatchBillReader._pickable_box_pieces_expr(self.COLUMNS)
        self.assertIn('"SalFactor3"', expr)

    def test_the_shared_expression_is_left_alone(self):
        expr = HanaDispatchBillReader._box_pieces_expr(self.COLUMNS)
        self.assertNotIn('"SalFactor3"', expr)

    def test_a_company_without_the_column_falls_back_to_the_shared_rule(self):
        without = {"SalFactor2", "ItemName"}
        self.assertEqual(
            HanaDispatchBillReader._pickable_box_pieces_expr(without),
            HanaDispatchBillReader._box_pieces_expr(without),
        )


class StampColumnTests(TestCase):
    def reader(self, columns, sizes=None):
        """A reader over one company's OINV, described as HANA describes it:
        each column with the number of characters it holds, or None where a
        limit is meaningless (a date, or `U_DriverName`, which is a CLOB)."""
        sizes = sizes or {}
        reader = HanaDispatchBillReader.__new__(HanaDispatchBillReader)
        reader._columns_cache = {"OINV": {c: sizes.get(c) for c in columns}}
        return reader

    def test_each_company_resolves_to_its_own_column(self):
        oil = self.reader(OIL_COLUMNS.values())
        beverages = self.reader(BEVERAGES_COLUMNS.values())
        self.assertEqual(oil.dispatch_stamp_columns()["bilty_no"], "U_BilltyNumber")
        self.assertEqual(beverages.dispatch_stamp_columns()["bilty_no"], "U_BiltyNumber")
        self.assertEqual(beverages.dispatch_stamp_columns()["vehicle_no"], "U_VechileNom")

    def test_a_field_the_company_lacks_is_simply_absent(self):
        reader = self.reader(["U_Dipatch_Date"])
        self.assertEqual(list(reader.dispatch_stamp_columns()), ["dispatch_date"])

    def test_the_width_of_each_field_is_read_from_the_company_too(self):
        """Not the same width twice: `U_Mob_No` is 11 characters at Mart and 12
        at Oil and Beverages, the vehicle number 12 at Oil and 20 at Beverages.
        SAP refuses the whole stamp over one character too many, so the limit has
        to come from the company being written to, not from a constant."""
        beverages = self.reader(
            BEVERAGES_COLUMNS.values(),
            {"U_Mob_No": 12, "U_VechileNom": 20, "U_BiltyNumber": 20},
        )
        sizes = beverages.dispatch_stamp_sizes()
        self.assertEqual(sizes["driver_mobile"], 12)
        self.assertEqual(sizes["vehicle_no"], 20)

    def test_a_field_with_no_meaningful_limit_is_not_capped(self):
        """`U_DriverName` is a CLOB and the dates are dates. Inventing a limit
        for them would drop values SAP would have taken."""
        oil = self.reader(OIL_COLUMNS.values(), {"U_Mob_No": 12})
        sizes = oil.dispatch_stamp_sizes()
        self.assertNotIn("driver_name", sizes)
        self.assertNotIn("dispatch_date", sizes)


class SapSourcedSummaryTests(BillSummaryTestBase):
    """Dispatches stamped straight into SAP, listed beside the app's own sheets.

    The point of these rows is that the screen cannot tell them apart: same
    shape, same fields, same box split. What it must NOT do is show the same
    dispatch twice, or let a projection be mistaken for a record.
    """

    def test_a_stamped_bill_is_listed_like_a_sheet(self):
        with self.stub():
            rows = self.service.list_sap_summaries({"date_from": "2026-09-01",
                                                    "date_to": "2026-09-30"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], SAP_SOURCE)
        self.assertEqual(row["key"], f"sap-{DOC_ENTRY}")
        self.assertIsNone(row["id"])
        self.assertEqual(row["entry_no"], f"SAP-{DOC_NUM}")
        self.assertEqual(row["sap_invoice_doc_num"], DOC_NUM)
        self.assertEqual(row["bilty_no"], "NCR-4494")
        self.assertEqual(row["dispatch_date"], DISPATCH_DATE.isoformat())
        # It is live and SAP holds it, which is what an app sheet that posted
        # cleanly says — so the badges read the same.
        self.assertEqual(row["status"], BillSummaryStatus.GENERATED)
        self.assertEqual(row["sap_status"], BillSummarySapStatus.POSTED)

    def test_the_apps_own_sheets_say_where_they_came_from(self):
        with self.stub():
            summary = self.generate()
        data = BillSummaryListSerializer(summary).data
        self.assertEqual(data["source"], APP_SOURCE)
        self.assertEqual(data["key"], str(summary.id))

    def test_a_bill_the_app_already_has_a_sheet_for_is_not_listed_twice(self):
        with self.stub():
            self.generate()
        with self.stub():
            rows = self.service.list_sap_summaries({"date_from": "2026-09-01",
                                                    "date_to": "2026-09-30"})
        self.assertEqual(rows, [])

    def test_a_cancelled_sheet_does_not_hide_the_stamped_bill(self):
        with self.stub():
            summary = self.generate()
            self.service.cancel(summary.id, "wrong vehicle")
        with self.stub():
            rows = self.service.list_sap_summaries({"date_from": "2026-09-01",
                                                    "date_to": "2026-09-30"})
        self.assertEqual(len(rows), 1)

    def test_opening_one_returns_its_lines_split_into_boxes(self):
        with self.stub([sap_line(qty="25", pcs_per_box="10")]):
            row = self.service.get_sap_summary(DOC_ENTRY)
        line = row["lines"][0]
        # 2 full boxes and 5 loose pieces, never "2.5 box".
        self.assertEqual(line["boxes"], Decimal("2"))
        self.assertEqual(line["loose_qty"], Decimal("5"))
        self.assertEqual(row["totals"]["lines"], 1)

    def test_the_dispatched_quantity_is_what_sap_holds(self):
        line = sap_line(qty="10")
        line["dispatched_qty"] = Decimal("4")
        with self.stub([line]):
            row = self.service.get_sap_summary(DOC_ENTRY)
        self.assertEqual(row["lines"][0]["dispatch_qty"], Decimal("4"))
        self.assertTrue(row["lines"][0]["is_short"])

    def test_a_bill_with_no_dispatch_qty_falls_back_to_the_billed_quantity(self):
        with self.stub([sap_line(qty="10")]):
            row = self.service.get_sap_summary(DOC_ENTRY)
        self.assertEqual(row["lines"][0]["dispatch_qty"], Decimal("10"))
        self.assertFalse(row["lines"][0]["is_short"])

    def test_the_printed_letterhead_is_read_for_the_opened_sheet(self):
        with self.stub():
            row = self.service.get_sap_summary(DOC_ENTRY)
        self.assertEqual(row["company_legal_name"], "JIVO WELLNESS PVT LTD")
        self.assertEqual(row["branch_gstin"], "06AACCJ4223F1Z0")
        self.assertIn("BHATTIAN", row["delivery_address"])

    def test_a_bill_with_no_stamp_is_not_found(self):
        with self.stub(stamped=[]):
            with self.assertRaises(BillSummaryError):
                self.service.get_sap_summary(DOC_ENTRY)

    def test_opening_one_that_was_taken_over_names_the_sheet_it_became(self):
        with self.stub():
            summary = self.generate()
        with self.stub():
            row = self.service.get_sap_summary(DOC_ENTRY)
        self.assertEqual(row["app_summary_id"], summary.id)


class SapSummaryAdoptionTests(BillSummaryTestBase):
    """Acting on a stamped bill puts it on the app's books first."""

    def adopt(self):
        with self.stub():
            return self.service.adopt_sap_summary(DOC_ENTRY)

    def test_adopting_records_the_sheet_from_what_sap_holds(self):
        summary = self.adopt()
        self.assertEqual(summary.sap_invoice_doc_num, DOC_NUM)
        self.assertEqual(summary.dispatch_date, DISPATCH_DATE)
        self.assertEqual(summary.bilty_no, "NCR-4494")
        self.assertEqual(summary.vehicle_no, "HR67D6673")
        self.assertEqual(summary.entry_no, f"BS-{DISPATCH_DATE:%Y%m%d}-001")
        self.assertEqual(summary.lines.count(), 1)

    def test_sap_is_not_written_to_again(self):
        """It already holds the stamp, and those fields are write-once."""
        with patch.object(BillSummaryService, "post_to_sap") as post:
            self.adopt()
        post.assert_not_called()

    def test_it_is_recorded_as_already_posted(self):
        self.assertEqual(self.adopt().sap_status, BillSummarySapStatus.POSTED)

    def test_nobody_is_credited_with_issuing_it(self):
        summary = self.adopt()
        self.assertIsNone(summary.issued_by)
        self.assertIn("typed straight into SAP", summary.remarks)

    def test_adopting_twice_lands_on_the_same_sheet(self):
        first = self.adopt()
        second = self.adopt()
        self.assertEqual(first.id, second.id)
        self.assertEqual(BillSummary.objects.count(), 1)

    def test_an_adopted_sheet_cancels_like_any_other(self):
        summary = self.adopt()
        self.service.cancel(summary.id, "bill amended")
        summary.refresh_from_db()
        self.assertEqual(summary.status, BillSummaryStatus.CANCELLED)
        # It was recorded as posted, so cancelling has SAP to undo -- the
        # clearing itself rides on_commit, which a TestCase transaction never
        # reaches, so it is driven directly here as the other cancel tests do.
        with self.stub():
            with patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as patched:
                self.service.post_to_sap(summary.id)
        self.assertTrue(patched.call_args.kwargs["clear"])

    def test_a_bill_sap_has_no_stamp_for_cannot_be_adopted(self):
        with self.stub(stamped=[]):
            with self.assertRaises(BillSummaryError):
                self.service.adopt_sap_summary(DOC_ENTRY)


class StampedBillQueryTests(SimpleTestCase):
    """The SQL behind the SAP-stamped list, which no test can run for real.

    Built without a connection and inspected, the way `StampColumnTests` does:
    what it must get right is the company's own spelling of the stamp columns and
    the fact that it bounds on the dispatch date rather than the create date.
    """

    HEADER = {
        "DocEntry", "DocNum", "DocDate", "CardCode", "CardName", "DocTotal",
        "BPLId", "BPLName", "Address2", "CANCELED",
    }

    def reader(self, extra_header, line=("Quantity", "U_Disp_Qty", "WhsCode", "LineNum"),
               item=("SalFactor2", "SalFactor3", "SalPackUn", "U_IsLitre")):
        reader = HanaDispatchBillReader.__new__(HanaDispatchBillReader)
        reader._columns_cache = {
            "OINV": self.HEADER | set(extra_header),
            "INV1": set(line),
            "OITM": set(item),
        }
        reader.connection = SimpleNamespace(schema="JIVO_OIL")
        reader.captured = []
        reader._execute = lambda query, params: (
            reader.captured.append((query, params)) or []
        )
        return reader

    def query_for(self, columns, filters=None):
        reader = self.reader(columns.values())
        reader.list_stamped_bills(
            filters or {"date_from": "2026-09-01", "date_to": "2026-09-30"}
        )
        return reader.captured[-1]

    def test_each_company_bounds_on_its_own_stamp_columns(self):
        oil, _ = self.query_for(OIL_COLUMNS)
        self.assertIn('H."U_Dipatch_Date" >= ?', oil)
        self.assertIn('"U_BilltyNumber"', oil)
        beverages, _ = self.query_for(BEVERAGES_COLUMNS)
        self.assertIn('"U_BiltyNumber"', beverages)
        self.assertIn('"U_VechileNom"', beverages)

    def test_only_stamped_uncancelled_bills_are_read(self):
        query, _ = self.query_for(OIL_COLUMNS)
        self.assertIn('H."U_Dipatch_Date" IS NOT NULL', query)
        self.assertIn("\"CANCELED\" = 'N'", query)

    def test_the_header_filter_is_bound_twice(self):
        """Once inside the line-aggregate subquery, once in the outer WHERE."""
        _, params = self.query_for(OIL_COLUMNS)
        self.assertEqual(params, ["2026-09-01", "2026-09-30", "2026-09-01", "2026-09-30"])

    def test_one_bill_is_looked_up_by_doc_entry_not_by_date(self):
        query, params = self.query_for(OIL_COLUMNS, {"doc_entry": DOC_ENTRY})
        self.assertIn('H."DocEntry" = ?', query)
        self.assertNotIn('H."U_Dipatch_Date" >= ?', query)
        self.assertEqual(params, [DOC_ENTRY, DOC_ENTRY])

    def test_boxes_are_counted_on_what_went_out(self):
        """Not on the billed quantity: a short dispatch is fewer boxes to fetch."""
        query, _ = self.query_for(OIL_COLUMNS)
        self.assertIn('L."U_Disp_Qty"', query)

    def test_a_company_with_no_dispatch_date_column_has_none_of_these(self):
        reader = self.reader([])
        self.assertEqual(
            reader.list_stamped_bills({"date_from": "2026-09-01", "date_to": "2026-09-30"}),
            [],
        )
        self.assertEqual(reader.captured, [])

