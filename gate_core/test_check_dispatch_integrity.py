"""The check_dispatch_integrity command detects and heals the two drift classes."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
)
from vehicle_management.models import Transporter, Vehicle


class CheckDispatchIntegrityTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create_user(
            email="ci@example.com", password="p", full_name="CI", employee_code="CI1",
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01LAC9967", transporter=self.transporter)
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")

    def _drifted_docking(self):
        """A docking whose header still lists two bills but one document has been
        deactivated WITHOUT re-aggregating the header, with its scans left active."""
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-CI", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-CI", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=5001, sap_doc_num="5001, 5002",  # header still lists both bills
            total_quantity=Decimal("30.000"),
            status=SalesDispatchGateOutStatus.DOCKED, created_by=self.user, updated_by=self.user,
        )
        keep = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=self.company, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=5001, sap_doc_num="5001", total_quantity=Decimal("20.000"),
            created_by=self.user, updated_by=self.user,
        )
        removed = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=self.company, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=5002, sap_doc_num="5002", total_quantity=Decimal("10.000"),
            is_active=False,  # already removed, but header/scans not unwound
            created_by=self.user, updated_by=self.user,
        )
        SalesDispatchBoxScan.objects.create(
            company=self.company, sales_dispatch=docking, document=removed, box_barcode="BOX-ORPHAN-1",
            item_code="X", quantity=Decimal("1.00"), created_by=self.user, updated_by=self.user,
        )
        return docking, keep, removed

    def test_detects_and_exits_nonzero(self):
        self._drifted_docking()
        with self.assertRaises(SystemExit) as cm:
            call_command("check_dispatch_integrity")
        self.assertEqual(cm.exception.code, 1)

    def test_fix_heals_header_and_orphaned_scans(self):
        docking, keep, removed = self._drifted_docking()

        call_command("check_dispatch_integrity", "--fix")

        docking.refresh_from_db()
        # Header re-aggregated to the surviving bill only.
        self.assertEqual(docking.sap_doc_num, "5001")
        self.assertEqual(docking.total_quantity, Decimal("20.000"))
        # Orphaned scan deactivated -> cannot settle at dispatch.
        self.assertEqual(
            SalesDispatchBoxScan.objects.filter(document=removed, is_active=True).count(), 0
        )
        # Idempotent: a second run finds nothing (exits 0, no SystemExit).
        call_command("check_dispatch_integrity")

    def test_clean_data_passes(self):
        # A docking whose header matches its single active document is clean.
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-OK", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-OK", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=6001, sap_doc_num="6001", total_quantity=Decimal("5.000"),
            status=SalesDispatchGateOutStatus.DOCKED, created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=self.company, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=6001, sap_doc_num="6001", total_quantity=Decimal("5.000"),
            created_by=self.user, updated_by=self.user,
        )
        call_command("check_dispatch_integrity")  # no SystemExit -> exit 0
