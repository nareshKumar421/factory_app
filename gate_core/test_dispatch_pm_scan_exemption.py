"""Packaging material is not part of a docking's scan target.

Bill 626090324 invoices 60 PCS of a 16-PCS olive oil (3 boxes + 12 loose) plus 8 PCS of
PM0000003, the carton itself. Cartons carry no box label -- nothing is ever printed for a
PM line, and nothing can be scanned against it -- but the scan page counted those 8 pieces
as goods owed: the bill read "3/3 boxes, 12/20 PCS loose  Partial" with its PM row sitting
"Open" while the whole load was on the truck.

These tests pin the exemption on the gate the operator meets: the scan target, the
per-(bill, item) completeness check, and a PM-only bill, which needs no scan at all.
Mirrors the BST rule (``warehouse.services.bst_service.is_pm_item_code``) and the scan
report's ``PM - scanning exempt`` status.
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
    arrival_scan_status,
    has_unscanned_bill_lines,
    load_scan_status,
    scan_target_split,
    scannable_lines,
)
from gate_core.services.sales_dispatch_loading import SCANNED, scan_box_onto_docking
from vehicle_management.models import Transporter, Vehicle

FG_CODE = "FG0000042"
FG_NAME = "EXTRA VIRGIN OLIVE 1 LTR 16 PCS"
PIECES_PER_BOX = 16

PM_CODE = "PM0000003"
PM_NAME = "CARTON 1 LTR POMACE 16 PCS"


class PackagingMaterialIsScanExemptTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_T")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="dockpm@example.com", password="p", full_name="Dock", employee_code="DKPM",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="Delhi Punjab")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR67D9271", transporter=self.transporter
        )
        self.driver = Driver.objects.create(name="Sumit", mobile_no="9729209417", license_no="DL-8")
        self.entry = self._docking()

    # ----- fixtures -------------------------------------------------------

    def _docking(self):
        ve = VehicleEntry.objects.create(
            entry_no="VE-4793", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-4793", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=626090324,
            sap_doc_num="626090324", status=SalesDispatchGateOutStatus.DOCKED,
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
            total_boxes=boxes, total_loose=loose,
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, document, barcode, qty, code=FG_CODE, name=FG_NAME):
        box = Box.objects.create(
            company=self.company, box_barcode=barcode, item_code=code, item_name=name,
            batch_number="L3 004192", qty=qty, current_warehouse="BH-PF",
            mfg_date=date(2026, 8, 1), exp_date=date(2027, 8, 1), status=BoxStatus.ACTIVE,
        )
        return scan_box_onto_docking(self.entry, box, user=self.user, document_id=document.id)

    def _reload(self):
        self.entry = SalesDispatchGateOut.objects.get(id=self.entry.id)
        return self.entry

    def _bill_626090324(self):
        document = self._document("626090324", total_boxes=3, total_loose=20)
        self._item(document, 0, 60, PIECES_PER_BOX, FG_CODE, FG_NAME, 3, 12)
        self._item(document, 1, 8, 1, PM_CODE, PM_NAME, 0, 8)
        return document

    # ----- the exemption --------------------------------------------------

    def test_pm_line_is_not_a_scannable_line(self):
        self._bill_626090324()

        codes = [item.item_code for item in scannable_lines(self._reload())]
        self.assertEqual(codes, [FG_CODE])

    def test_pm_pieces_are_not_part_of_the_scan_target(self):
        """The 8 carton pieces were showing as loose goods owed: 12 expected, not 20."""
        self._bill_626090324()

        self.assertEqual(scan_target_split(self._reload()), (3, 12))

    def test_bill_reads_complete_once_its_goods_are_scanned(self):
        document = self._bill_626090324()

        for i in range(3):
            self.assertEqual(self._scan(document, f"BOX-{i}", PIECES_PER_BOX).status, SCANNED)
            self._reload()
        # The 12 loose pieces ride out in a part box of their own.
        self.assertEqual(self._scan(document, "BOX-LOOSE", 12).status, SCANNED)

        entry = self._reload()
        self.assertFalse(has_unscanned_bill_lines(entry))
        _scanned, expected, has_scans, is_partial = load_scan_status(entry)
        self.assertEqual(expected, 3)
        self.assertTrue(has_scans)
        self.assertFalse(is_partial)

    def test_pm_only_bill_needs_no_scan_at_all(self):
        document = self._document("626090325", total_boxes=0, total_loose=8)
        self._item(document, 0, 8, 1, PM_CODE, PM_NAME, 0, 8)

        entry = self._reload()
        self.assertEqual(scan_target_split(entry), (0, 0))
        # No scan exists, and none is owed -- the truck-wide gate must not hold the load.
        _scanned, _expected, _has_scans, is_partial = arrival_scan_status(entry)
        self.assertFalse(is_partial)

    def test_a_short_fg_line_is_still_caught_on_a_bill_carrying_pm(self):
        """The exemption is PM-only: real goods still gate the load."""
        document = self._bill_626090324()
        self.assertEqual(self._scan(document, "BOX-0", PIECES_PER_BOX).status, SCANNED)

        entry = self._reload()
        self.assertTrue(has_unscanned_bill_lines(entry))
        self.assertTrue(load_scan_status(entry)[3])
