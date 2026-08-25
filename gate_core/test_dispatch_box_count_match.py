"""The docking box count must match the boxes the floor actually packs.

Docking 1250 (DOCK-20260824-0018) showed "452 / 435 boxes" on a truck whose bills were
complete but for 20 pieces. Both halves of the gap were in the EXPECTED figures, which
were built by adding up the bill's printed per-line splits:

* Bill 626080435 invoices FG0000142 as 1,600 + 13 + 67 pieces of a 16-PCS item. Split line
  by line that prints 104 boxes + 16 loose — but the 13 and 3 leftover pieces are one more
  WHOLE box, so the warehouse packs (and the scanner counts) 105.
* Bill 626080439 invoices 16 pieces of MUSTARD KACHI GHANI 15 KGS (``SalFactor2 = 1``,
  not CSD). Its bill prints "0 Box / 16 PCS"; the goods ship as 16 labelled tins, which
  were counted as 16 boxes against a bill printing none.

These tests pin the scan target (grouped per bill+item, split once) and the scanned split
(a box of an unboxed item covers printed loose pieces, never a box).
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from barcode.models import Box, BoxStatus
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
)
from gate_core.services.sales_dispatch_gatepass import (
    load_scan_status,
    resolved_expected_box_count,
    resolved_expected_loose_count,
    scan_target_split,
    scanned_box_split,
)
from gate_core.services.sales_dispatch_loading import SCANNED, scan_box_onto_docking
from vehicle_management.models import Transporter, Vehicle

BOXED_CODE = "FG0000142"
BOXED_NAME = "COLD PRESS GROUNDNUT OIL 1 LTR 16 PCS"
PIECES_PER_BOX = 16

# SAP does not transact this one in boxes: SalFactor2 = 1 and no CSD token in the name.
UNBOXED_CODE = "FG0000178"
UNBOXED_NAME = "MUSTARD KACHI GHANI 15 KGS"


class BoxCountMatchesPackedBoxesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_T")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="dock1250@example.com", password="p", full_name="Dock", employee_code="DK2",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="Delhi Punjab")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR67D9270", transporter=self.transporter
        )
        self.driver = Driver.objects.create(name="Sumit", mobile_no="9729209416", license_no="DL-9")
        self.entry = self._docking()

    # ----- fixtures -------------------------------------------------------

    def _docking(self):
        ve = VehicleEntry.objects.create(
            entry_no="VE-1250", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-1250", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=626080435,
            sap_doc_num="626080435", status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )

    def _document(self, doc_num, **totals):
        return SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(doc_num),
            sap_doc_num=doc_num, created_by=self.user, updated_by=self.user, **totals,
        )

    def _item(self, document, line_num, quantity, factor, code, name, boxes, loose):
        return SalesDispatchGateOutItem.objects.create(
            sales_dispatch=self.entry, document=document, line_num=line_num,
            item_code=code, item_name=name, quantity=quantity, sal_factor2=factor,
            # The split SAP's own bill prints for THIS line — what used to be summed.
            total_boxes=boxes, total_loose=loose,
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, document, barcode, qty, code=BOXED_CODE, name=BOXED_NAME):
        box = Box.objects.create(
            company=self.company, box_barcode=barcode, item_code=code, item_name=name,
            batch_number="L3 004192", qty=qty, current_warehouse="BH-PF",
            mfg_date=date(2026, 8, 1), exp_date=date(2027, 8, 1), status=BoxStatus.ACTIVE,
        )
        return scan_box_onto_docking(
            self.entry, box, user=self.user, document_id=document.id
        )

    def _reload(self):
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        return self.entry

    # ----- cause 1: one product invoiced on several lines -----------------

    def test_line_remainders_that_make_a_whole_box_are_counted_as_one(self):
        document = self._document("626080435", total_boxes=104, total_loose=16)
        self._item(document, 0, 1600, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 100, 0)
        self._item(document, 1, 13, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 0, 13)
        self._item(document, 2, 67, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 4, 3)
        # SAP's snapshot of the printed totals, which used to win outright.
        self.entry.total_boxes = 104
        self.entry.total_loose = 16
        self.entry.save(update_fields=["total_boxes", "total_loose"])

        entry = self._reload()
        # 1,600 + 13 + 67 = 1,680 pieces = exactly 105 boxes of 16, no loose remainder.
        self.assertEqual(scan_target_split(entry), (105, 0))
        self.assertEqual(resolved_expected_box_count(entry), 105)
        self.assertEqual(resolved_expected_loose_count(entry), 0)

    def test_a_real_remainder_still_prints_as_loose(self):
        """Merging must not swallow a genuine remainder: 20 pcs of a 16-PCS item is 1 + 4."""
        document = self._document("626080453")
        self._item(document, 0, 480, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 30, 0)
        self._item(document, 1, 20, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 1, 4)

        entry = self._reload()
        self.assertEqual(scan_target_split(entry), (31, 4))

    # ----- cause 2: an item SAP does not box ------------------------------

    def test_unboxed_item_expects_no_boxes_and_all_pieces_loose(self):
        document = self._document("626080439", total_boxes=0, total_loose=16)
        self._item(document, 0, 16, 1, UNBOXED_CODE, UNBOXED_NAME, 0, 16)

        entry = self._reload()
        self.assertEqual(scan_target_split(entry), (0, 16))

    def test_tins_of_an_unboxed_item_count_as_loose_pieces_not_boxes(self):
        document = self._document("626080439", total_boxes=0, total_loose=4)
        self._item(document, 0, 4, 1, UNBOXED_CODE, UNBOXED_NAME, 0, 4)

        for i in range(4):
            outcome = self._scan(
                document, f"TIN-{i}", 1, code=UNBOXED_CODE, name=UNBOXED_NAME
            )
            self.assertEqual(outcome.status, SCANNED)

        entry = self._reload()
        # 4 labels on the truck, but the bill prints no boxes: they are its loose pieces.
        self.assertEqual(entry.box_scans.filter(is_active=True).count(), 4)
        self.assertEqual(scanned_box_split(entry), (0, 4))
        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertEqual(expected, 0)
        self.assertTrue(has_scans)
        # Every invoiced piece is loaded, so the load is complete — not partial.
        self.assertFalse(is_partial)

    # ----- the two together, as the truck carried them --------------------

    def test_load_of_both_shapes_reads_complete(self):
        """Both shapes on one truck, at the arithmetic of the incident.

        Kept small on purpose -- each scan is a full pass through the scan pipeline, and
        the counts prove themselves at 3 boxes as well as at 105.
        """
        boxed = self._document("626080435", total_boxes=2, total_loose=16)
        # 32 + 13 + 3 = 48 pieces of a 16-PCS item. Printed line by line that is
        # "2 boxes + 16 loose"; packed it is 3 whole boxes -- the docking 1250 shape.
        self._item(boxed, 0, 32, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 2, 0)
        self._item(boxed, 1, 13, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 0, 13)
        self._item(boxed, 2, 3, PIECES_PER_BOX, BOXED_CODE, BOXED_NAME, 0, 3)
        tins = self._document("626080439", total_boxes=0, total_loose=3)
        # line_num is unique per DOCKING, not per bill, so the second bill numbers on.
        self._item(tins, 3, 3, 1, UNBOXED_CODE, UNBOXED_NAME, 0, 3)

        entry = self._reload()
        self.assertEqual(scan_target_split(entry), (3, 3))

        for i in range(3):
            self.assertEqual(self._scan(boxed, f"BOX-{i}", PIECES_PER_BOX).status, SCANNED)
            self._reload()
        for i in range(3):
            self.assertEqual(
                self._scan(tins, f"TIN-{i}", 1, code=UNBOXED_CODE, name=UNBOXED_NAME).status,
                SCANNED,
            )
            self._reload()

        entry = self._reload()
        self.assertEqual(entry.box_scans.filter(is_active=True).count(), 6)
        # 3 full boxes against 3 expected; the 3 tins are the printed loose pieces.
        self.assertEqual(scanned_box_split(entry), (3, 3))
        _scanned, expected, _has_scans, is_partial = load_scan_status(entry)
        self.assertEqual(expected, 3)
        self.assertFalse(is_partial)
