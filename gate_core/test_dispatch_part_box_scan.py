"""Part ("loose") boxes on a docking scan — counted as loose pieces, not as boxes.

A bill line is printed by SAP as ``floor(qty / SalFactor2)`` boxes **plus** a loose
remainder, and the goods for that remainder physically arrive as a short box. Counting
that short box as one of the full boxes made a 1,860-PCS line of a 16-PCS item (116
boxes + 4 loose) read "116 / 116 boxes" after 115 full boxes and one 4-piece box: the
box count looked complete, 16 pieces were still on the floor, and the box-count cap
refused the very box that would have finished the bill.

These tests pin the fixed accounting at the scale of the incident (3 boxes + 4 loose).
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
from gate_core.services.sales_dispatch_box_match import (
    expected_containers_for_bill_item,
    remaining_expected_boxes,
)
from gate_core.services.sales_dispatch_gatepass import (
    item_packing,
    load_scan_status,
    scanned_box_split,
    scanned_full_box_count,
)
from gate_core.services.sales_dispatch_loading import (
    REJECTED,
    SCANNED,
    scan_box_onto_docking,
)
from vehicle_management.models import Transporter, Vehicle

ITEM_CODE = "FG0000142"
ITEM_NAME = "COLD PRESS GROUNDNUT OIL 1 LTR 16 PCS"
PIECES_PER_BOX = 16
# 3 full boxes + 4 loose pieces — the shape of bill 608260260 (1,860 = 116 x 16 + 4).
INVOICED_QTY = PIECES_PER_BOX * 3 + 4


class PartBoxDockingScanTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART_T")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="dock@example.com", password="p", full_name="Dock", employee_code="DK1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="PB01AA1111", transporter=self.transporter
        )
        self.driver = Driver.objects.create(name="D", mobile_no="9000000002", license_no="DL-8")
        self.entry = self._docking()

    # ----- fixtures -------------------------------------------------------

    def _docking(self):
        ve = VehicleEntry.objects.create(
            entry_no="VE-PB", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DK-PB", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=608260260,
            sap_doc_num="608260260", status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )
        self.document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=608260260,
            sap_doc_num="608260260", created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry, document=self.document, line_num=12, item_code=ITEM_CODE,
            item_name=ITEM_NAME, quantity=INVOICED_QTY, sal_factor2=PIECES_PER_BOX,
            total_boxes=3, total_loose=4,
            created_by=self.user, updated_by=self.user,
        )
        return entry

    def _box(self, barcode, qty):
        return Box.objects.create(
            company=self.company, box_barcode=barcode, item_code=ITEM_CODE,
            item_name=ITEM_NAME, batch_number="L3 004192", qty=qty,
            current_warehouse="BH-PF", mfg_date=date(2026, 8, 1), exp_date=date(2027, 8, 1),
            status=BoxStatus.ACTIVE,
        )

    def _scan(self, barcode, qty):
        return scan_box_onto_docking(
            self.entry, self._box(barcode, qty), user=self.user,
            document_id=self.document.id,
        )

    # ----- the printed split ---------------------------------------------

    def test_line_prints_boxes_plus_loose(self):
        item = self.entry.active_items[0]
        packing = item_packing(item)
        self.assertEqual(packing.boxes, 3)
        self.assertEqual(packing.loose, 4)

    # ----- the incident ---------------------------------------------------

    def test_part_box_does_not_consume_a_full_box_slot(self):
        """115-full + one part box no longer blocks the box that finishes the bill."""
        for i in range(2):
            self.assertEqual(self._scan(f"BOX-FULL-{i}", PIECES_PER_BOX).status, SCANNED)
        # The short box that covers the line's 4 loose pieces.
        self.assertEqual(self._scan("BOX-PART", 4).status, SCANNED)

        entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        # Three boxes are physically scanned, but only two are FULL boxes.
        self.assertEqual(entry.box_scans.filter(is_active=True).count(), 3)
        self.assertEqual(scanned_full_box_count(entry), 2)
        self.assertEqual(scanned_box_split(entry), (2, 4))
        # The bill can arrive in 4 boxes (3 full + the one holding its 4 loose pieces),
        # so one box of headroom is left — the box that finishes the bill.
        self.assertEqual(expected_containers_for_bill_item(entry, self.document.id, ITEM_CODE), 4)
        self.assertEqual(remaining_expected_boxes(entry, self.document.id, ITEM_CODE), 1)

        # The last full box completes the bill instead of being refused.
        self.entry = entry
        self.assertEqual(self._scan("BOX-FULL-LAST", PIECES_PER_BOX).status, SCANNED)

        entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        self.assertEqual(scanned_box_split(entry), (3, 4))
        _scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertEqual(expected, 3)
        self.assertTrue(has_scans)
        self.assertFalse(is_partial)

    def test_extra_full_box_is_still_refused(self):
        """The anti-over-scan cap survives: a 4th full box exceeds the invoiced pieces."""
        for i in range(3):
            self.assertEqual(self._scan(f"BOX-FULL-{i}", PIECES_PER_BOX).status, SCANNED)
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)

        outcome = self._scan("BOX-FULL-EXTRA", PIECES_PER_BOX)

        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(
            SalesDispatchGateOut.objects.get(id=self.entry.id)
            .box_scans.filter(is_active=True)
            .count(),
            3,
        )

    def test_second_part_box_is_refused(self):
        """One short box covers the line's loose remainder; a second has no room left."""
        for i in range(3):
            self.assertEqual(self._scan(f"BOX-FULL-{i}", PIECES_PER_BOX).status, SCANNED)
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        # 4 pieces remain on the line: a 4-piece box fits, a second one does not.
        self.assertEqual(self._scan("BOX-PART-1", 4).status, SCANNED)
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        self.assertEqual(self._scan("BOX-PART-2", 4).status, REJECTED)
