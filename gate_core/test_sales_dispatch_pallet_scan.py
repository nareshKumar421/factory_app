"""Warehouse Dispatch Loading — pallet scan onto a docking.

Exercises ``scan_pallet_onto_docking`` / ``scan_box_onto_docking`` (the shared
core behind both the gate box-scan endpoint and the warehouse pallet-scan
endpoint) end to end: attribution to a bill, INSIDE_VEHICLE staging, the
partial-pallet guard, and the automatic freeing of the pallet's Warehouse Ops
(WMS) map location once it is fully staged.
"""
import uuid
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from barcode.models import Box, BoxStatus, Pallet, PalletStatus
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
)
from gate_core.services.sales_dispatch_loading import (
    DUPLICATE,
    SCANNED,
    scan_box_onto_docking,
    scan_pallet_onto_docking,
)
from vehicle_management.models import Transporter, Vehicle
from wms.models import Location as WmsLocation, Pallet as WmsPallet

ITEM_CODE = "FG0000142"
BOX_QTY = 10


class PalletScanOntoDockingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_T")
        self.role = UserRole.objects.create(name="Warehouse")
        self.user = get_user_model().objects.create_user(
            email="wh@example.com", password="p", full_name="WH", employee_code="WH1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LY9999", transporter=self.transporter
        )
        self.driver = Driver.objects.create(name="D", mobile_no="9000000001", license_no="DL-9")

    # ----- fixture helpers ------------------------------------------------

    def _docking(self, *, invoiced_boxes, box_qty=BOX_QTY):
        """A DOCKED docking with one INVOICE bill invoicing ``invoiced_boxes``."""
        ve = VehicleEntry.objects.create(
            entry_no="VE-1", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DK-1", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=5001,
            sap_doc_num="620000001", status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )
        document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=5001,
            sap_doc_num="620000001", created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry, document=document, line_num=0, item_code=ITEM_CODE,
            item_name="COLD PRESS", quantity=invoiced_boxes * box_qty,
            total_boxes=invoiced_boxes, created_by=self.user, updated_by=self.user,
        )
        return entry

    def _pallet_with_boxes(self, n, *, box_qty=BOX_QTY, plate="PLT-20260807-XX-001"):
        pallet = Pallet.objects.create(
            company=self.company, pallet_id=plate, item_code=ITEM_CODE, item_name="COLD PRESS",
            batch_number="L1", box_count=n, total_boxes=n, available_boxes=n,
            total_qty=n * box_qty, current_warehouse="BH-BT",
            mfg_date=date(2026, 8, 1), exp_date=date(2027, 8, 1), status=PalletStatus.ACTIVE,
        )
        for i in range(n):
            Box.objects.create(
                company=self.company, box_barcode=f"{plate}-B{i:03d}", item_code=ITEM_CODE,
                item_name="COLD PRESS", batch_number="L1", qty=box_qty, pallet=pallet,
                current_warehouse="BH-BT", mfg_date=date(2026, 8, 1), exp_date=date(2027, 8, 1),
                status=BoxStatus.ACTIVE,
            )
        return pallet

    def _place_on_map(self, pallet, code="23-R"):
        """Author a WMS location + placed pallet doc (as the map stores them)."""
        loc_id = str(uuid.uuid4())
        WmsLocation.objects.create(
            company=self.company, record_id=loc_id,
            data={"id": loc_id, "code": code, "barcode": code},
        )
        pal_id = str(uuid.uuid4())
        WmsPallet.objects.create(
            company=self.company, record_id=pal_id,
            data={
                "id": pal_id, "licensePlate": pallet.pallet_id, "currentLocationId": loc_id,
                "itemCode": pallet.item_code, "boxCount": pallet.box_count,
                "totalUnits": float(pallet.total_qty),
            },
        )
        return pal_id

    # ----- tests ----------------------------------------------------------

    def test_full_pallet_scan_stages_all_boxes_and_frees_bin(self):
        entry = self._docking(invoiced_boxes=4)
        pallet = self._pallet_with_boxes(4)
        wms_pallet_id = self._place_on_map(pallet)

        result = scan_pallet_onto_docking(entry, pallet, user=self.user)

        self.assertEqual(result.scanned, 4)
        self.assertEqual(result.rejected, 0)
        self.assertTrue(result.bin_freed)

        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.INSIDE_VEHICLE)
        self.assertEqual(
            Box.objects.filter(pallet=pallet, status=BoxStatus.INSIDE_VEHICLE).count(), 4
        )
        # Every box scan landed on the one bill.
        scans = entry.box_scans.filter(is_active=True)
        self.assertEqual(scans.count(), 4)
        self.assertTrue(all(s.document_id is not None for s in scans))
        # The WMS map location was vacated automatically.
        wms_pallet = WmsPallet.objects.get(record_id=wms_pallet_id)
        self.assertIsNone(wms_pallet.data.get("currentLocationId"))

    def test_partial_pallet_when_bill_invoices_fewer_boxes(self):
        # Bill only needs 2 of the pallet's 4 boxes.
        entry = self._docking(invoiced_boxes=2)
        pallet = self._pallet_with_boxes(4)
        wms_pallet_id = self._place_on_map(pallet)

        result = scan_pallet_onto_docking(entry, pallet, user=self.user)

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.rejected, 2)
        self.assertFalse(result.bin_freed)

        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.PARTIAL)
        self.assertEqual(
            Box.objects.filter(pallet=pallet, status=BoxStatus.ACTIVE).count(), 2
        )
        # Bin is NOT freed — the pallet still holds active boxes at its location.
        wms_pallet = WmsPallet.objects.get(record_id=wms_pallet_id)
        self.assertIsNotNone(wms_pallet.data.get("currentLocationId"))

    def test_box_scan_parity_and_duplicate(self):
        entry = self._docking(invoiced_boxes=4)
        pallet = self._pallet_with_boxes(4)
        box = pallet.boxes.first()

        first = scan_box_onto_docking(entry, box, user=self.user)
        self.assertEqual(first.status, SCANNED)
        box.refresh_from_db()
        self.assertEqual(box.status, BoxStatus.INSIDE_VEHICLE)

        again = scan_box_onto_docking(entry, box, user=self.user)
        self.assertEqual(again.status, DUPLICATE)
        self.assertEqual(entry.box_scans.filter(is_active=True).count(), 1)
