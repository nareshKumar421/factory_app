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
from decimal import Decimal

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
    remaining_invoiced_qty,
)
from gate_core.services.sales_dispatch_gatepass import (
    item_packing,
    load_scan_status,
    scanned_box_split,
    scanned_full_box_count,
)
from gate_core.services.sales_dispatch_loading import (
    REJECT_BILL_BOXES_COMPLETE,
    REJECT_BILL_QTY_COMPLETE,
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
        # The bill can arrive in at most 7 boxes (3 full + up to one per piece of its
        # 4-piece remainder), so headroom is left — including the box that finishes it.
        self.assertEqual(expected_containers_for_bill_item(entry, self.document.id, ITEM_CODE), 7)
        self.assertEqual(remaining_expected_boxes(entry, self.document.id, ITEM_CODE), 4)

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
        """Two short boxes cannot both fit a 4-piece remainder — the QUANTITY guard says so.

        Not the box-count cap: the first part box takes the line to its full 52 invoiced
        pieces, so the second is refused for having nothing left to cover. The count cap
        only stops FULL boxes (see ``SplitRemainderDockingScanTests``)."""
        for i in range(3):
            self.assertEqual(self._scan(f"BOX-FULL-{i}", PIECES_PER_BOX).status, SCANNED)
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        # 4 pieces remain on the line: a 4-piece box fits, a second one does not.
        self.assertEqual(self._scan("BOX-PART-1", 4).status, SCANNED)
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        self.assertEqual(self._scan("BOX-PART-2", 4).status, REJECTED)


# ---------------------------------------------------------------------------
# Bill 626090220 — a loose remainder split across MORE than one short box.
# ---------------------------------------------------------------------------

SPLIT_ITEM_CODE = "FG0000306"
SPLIT_ITEM_NAME = "YELLOW MUSTARD OIL 1 LTR 20 PCS"
SPLIT_PACK = 20
# The two lines SAP wrote the item on: 200 PCS (10 boxes) + 10 PCS (all remainder).
SPLIT_LINES = ((200, 10, 0), (10, 0, 10))  # (quantity, total_boxes, total_loose)


class SplitRemainderDockingScanTests(TestCase):
    """A line's loose remainder can arrive in several short boxes, not just one.

    Bill 626090220 invoiced 210 PCS of a 20-PCS item across two lines (200 + 10), so the
    box-count cap allowed ``floor + 1 = 11`` containers on the assumption the 10-piece
    remainder would come in a single short box. The warehouse repacked it as two 5-piece
    boxes, so 12 containers held the bill: the 12th was refused as "already has the
    expected number of boxes ... 5.000 PCS are still short", with no way to finish the
    scan. Short boxes are now bounded by invoiced QUANTITY alone; only full boxes are
    capped on count.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_T")
        self.role = UserRole.objects.create(name="Dock SR")
        self.user = get_user_model().objects.create_user(
            email="dock-sr@example.com", password="p", full_name="Dock SR",
            employee_code="DK2",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="T2")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="PB01AA2222", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="D2", mobile_no="9000000003", license_no="DL-9"
        )
        self.entry = self._docking(SPLIT_LINES)

    # ----- fixtures -------------------------------------------------------

    def _docking(self, lines, suffix="", doc_num="626090220"):
        ve = VehicleEntry.objects.create(
            entry_no=f"VE-SR{suffix}", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no=f"DK-SR{suffix}", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(doc_num),
            sap_doc_num=doc_num, status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )
        self.document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=626090220,
            sap_doc_num="626090220", created_by=self.user, updated_by=self.user,
        )
        for line_num, (qty, boxes, loose) in enumerate(lines, start=1):
            SalesDispatchGateOutItem.objects.create(
                sales_dispatch=entry, document=self.document, line_num=line_num,
                item_code=SPLIT_ITEM_CODE, item_name=SPLIT_ITEM_NAME, quantity=qty,
                sal_factor2=SPLIT_PACK, total_boxes=boxes, total_loose=loose,
                created_by=self.user, updated_by=self.user,
            )
        return entry

    def _scan(self, barcode, qty):
        box = Box.objects.create(
            company=self.company, box_barcode=barcode, item_code=SPLIT_ITEM_CODE,
            item_name=SPLIT_ITEM_NAME, batch_number="L1 004192", qty=qty,
            current_warehouse="BH-PF", mfg_date=date(2026, 9, 1), exp_date=date(2027, 9, 1),
            status=BoxStatus.ACTIVE,
        )
        outcome = scan_box_onto_docking(
            self.entry, box, user=self.user, document_id=self.document.id,
        )
        # The guards read committed scan state, so re-fetch between scans.
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        return outcome

    # ----- the container cap ---------------------------------------------

    def test_cap_allows_a_container_per_printed_box_and_per_remainder_piece(self):
        """10 printed boxes + 10 loose pieces = at most 20 containers, not 11."""
        entry = self.entry
        self.assertEqual(
            expected_containers_for_bill_item(entry, self.document.id, SPLIT_ITEM_CODE), 20
        )
        self.assertEqual(
            remaining_expected_boxes(entry, self.document.id, SPLIT_ITEM_CODE), 20
        )

    # ----- the incident ---------------------------------------------------

    def test_remainder_split_across_two_short_boxes_completes_the_bill(self):
        """The box holding the last 5 pieces is accepted past the container count."""
        for i in range(10):
            self.assertEqual(self._scan(f"BOX-SR-FULL-{i}", SPLIT_PACK).status, SCANNED)
        # The two 5-piece repack boxes the invoice's 10 loose pieces came in.
        self.assertEqual(self._scan("BOX-20260907-RP-0003", 5).status, SCANNED)

        # Where the old flat "+1" allowance stood, 11 containers held the bill and
        # 5 PCS were still short — the point the scan used to deadlock.
        self.assertEqual(self.entry.box_scans.filter(is_active=True).count(), 11)
        self.assertEqual(
            remaining_invoiced_qty(self.entry, self.document.id, SPLIT_ITEM_CODE),
            Decimal("5"),
        )

        outcome = self._scan("BOX-20260907-RP-0004", 5)

        self.assertEqual(outcome.status, SCANNED, outcome.detail)
        self.assertEqual(self.entry.box_scans.filter(is_active=True).count(), 12)
        # 210 invoiced PCS are now accounted for, in 10 full boxes + 10 loose pieces.
        self.assertEqual(
            remaining_invoiced_qty(self.entry, self.document.id, SPLIT_ITEM_CODE),
            Decimal("0"),
        )
        self.assertEqual(scanned_box_split(self.entry), (10, Decimal("10")))

    def test_a_thirteenth_box_is_still_refused(self):
        """Quantity, not the container count, is what closes the bill."""
        for i in range(10):
            self.assertEqual(self._scan(f"BOX-SR-FULL-{i}", SPLIT_PACK).status, SCANNED)
        for seq in ("0003", "0004"):
            self.assertEqual(self._scan(f"BOX-20260907-RP-{seq}", 5).status, SCANNED)

        outcome = self._scan("BOX-20260907-RP-0005", 5)

        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_BILL_QTY_COMPLETE)
        self.assertEqual(self.entry.box_scans.filter(is_active=True).count(), 12)

    # ----- the cap still bites on full boxes ------------------------------

    def test_extra_box_is_capped_on_count_when_there_is_no_remainder(self):
        """A line invoicing an exact 10 boxes cannot take an 11th container.

        With no loose remainder the cap is the printed box count exactly. Ten SHORT boxes
        leave a full pack's worth of quantity un-scanned, so the quantity guard would wave
        an 11th box through — the container count is the only rule that catches it, and it
        applies whether that box is full or short (the 581-vs-580 case).
        """
        self.entry = self._docking(((200, 10, 0),), suffix="-2", doc_num="626090221")
        self.assertEqual(
            expected_containers_for_bill_item(self.entry, self.document.id, SPLIT_ITEM_CODE),
            10,
        )
        for i in range(10):
            self.assertEqual(self._scan(f"BOX-SR-SHORT-{i}", 18).status, SCANNED)
        self.assertEqual(
            remaining_invoiced_qty(self.entry, self.document.id, SPLIT_ITEM_CODE),
            Decimal("20"),
        )

        outcome = self._scan("BOX-SR-FULL-EXTRA", SPLIT_PACK)

        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_BILL_BOXES_COMPLETE)
        self.assertEqual(self.entry.box_scans.filter(is_active=True).count(), 10)
