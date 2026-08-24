"""A refused docking scan must leave a ``barcode.ScanLog`` row behind.

Business rejections ("bill already has the full invoiced quantity", "box is already
loaded in a vehicle") were returned to the operator as a 400 and then discarded, so
the only dock failure anything could count was ``NOT_FOUND`` — a barcode that did not
resolve at all. That is roughly a third of the real failures, and it is the wrong
third: a NOT_FOUND is usually cleared by a re-scan in under five minutes, while a
business rejection needs a decision and is what actually holds a truck at the dock.

These tests pin that every refusal reaching the operator is now recorded, with a
machine-readable ``reject_code``, against the docking it happened on.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from barcode.models import Box, BoxStatus, ScanLog, ScanResult
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
    REJECT_BILL_QTY_COMPLETE,
    REJECT_BOX_UNAVAILABLE,
    REJECTED,
    SCANNED,
    scan_box_onto_docking,
)
from vehicle_management.models import Transporter, Vehicle

ITEM_CODE = "FG0000142"
ITEM_NAME = "COLD PRESS GROUNDNUT OIL 1 LTR 16 PCS"
PIECES_PER_BOX = 16


class DockingFixtureMixin:
    """One docked truck, one bill invoicing exactly one full box."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART_RJ")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="rej@example.com", password="p", full_name="Dock", employee_code="RJ1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="PB01RJ1111", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="D", mobile_no="9000000044", license_no="DL-44"
        )
        self.entry = self._docking()

    def _docking(self):
        ve = VehicleEntry.objects.create(
            entry_no="VE-RJ", company=self.company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DK-RJ", vehicle_entry=ve, vehicle=self.vehicle,
            transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=700100100,
            sap_doc_num="700100100", status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )
        self.document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=700100100,
            sap_doc_num="700100100", created_by=self.user, updated_by=self.user,
        )
        # One full box invoiced — the second identical box must be refused.
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry, document=self.document, line_num=1, item_code=ITEM_CODE,
            item_name=ITEM_NAME, quantity=PIECES_PER_BOX, sal_factor2=PIECES_PER_BOX,
            total_boxes=1, total_loose=0,
            created_by=self.user, updated_by=self.user,
        )
        return entry

    def _box(self, barcode, status=BoxStatus.ACTIVE):
        return Box.objects.create(
            company=self.company, box_barcode=barcode, item_code=ITEM_CODE,
            item_name=ITEM_NAME, batch_number="L3 004192", qty=PIECES_PER_BOX,
            current_warehouse="BH-PF", mfg_date=date(2026, 8, 1),
            exp_date=date(2027, 8, 1), status=status,
        )

    def _scan(self, box):
        return scan_box_onto_docking(
            self.entry, box, user=self.user, document_id=self.document.id
        )


class DockingScanRejectionCodeTests(DockingFixtureMixin, TestCase):
    """The service names why it refused, so the log can group refusals."""

    def test_over_quantity_rejection_carries_its_code(self):
        self.assertEqual(self._scan(self._box("BOX-RJ-1")).status, SCANNED)

        outcome = self._scan(self._box("BOX-RJ-2"))

        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_BILL_QTY_COMPLETE)
        self.assertIn("full invoiced", outcome.detail)

    def test_unavailable_box_rejection_carries_its_code(self):
        outcome = self._scan(self._box("BOX-RJ-3", status=BoxStatus.DISPATCHED))

        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_BOX_UNAVAILABLE)

    def test_accepted_scan_has_no_reject_code(self):
        outcome = self._scan(self._box("BOX-RJ-4"))

        self.assertEqual(outcome.status, SCANNED)
        self.assertEqual(outcome.code, "")


class DockingScanRejectionLoggingTests(DockingFixtureMixin, TestCase):
    """End-to-end through the endpoint: the refusal reaches ScanLog."""

    def setUp(self):
        super().setUp()
        self.user.user_permissions.add(
            Permission.objects.get(codename="can_edit_sales_dispatch_out"),
            Permission.objects.get(codename="can_view_sales_dispatch_out"),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = (
            f"/api/v1/gate-core/sales-dispatch/{self.entry.id}/box-scans/"
        )

    def _rejections(self):
        return ScanLog.objects.filter(
            scan_result=ScanResult.REJECTED,
            context_ref_type="SALES_DISPATCH",
            context_ref_id=self.entry.id,
        )

    def _post(self, barcode):
        return self.client.post(
            self.url, {"barcode_raw": barcode}, format="json",
            headers={"Company-Code": self.company.code},
        )

    def test_refused_scan_is_logged_once_with_its_code(self):
        self._box("BOX-RJ-L1")
        self._box("BOX-RJ-L2")
        self.assertEqual(self._post("BOX-RJ-L1").status_code, 201)

        response = self._post("BOX-RJ-L2")

        self.assertEqual(response.status_code, 400)
        log = self._rejections().get()
        self.assertEqual(log.barcode_raw, "BOX-RJ-L2")
        self.assertEqual(log.reject_code, REJECT_BILL_QTY_COMPLETE)
        self.assertIn("full invoiced", log.reject_message)

    def test_one_physical_scan_makes_one_row(self):
        """The refusal stamps the row process_scan already wrote, never adds a
        second — otherwise every rejection would double-count against the retry
        statistics this logging exists to produce."""
        self._box("BOX-RJ-L3")
        self._box("BOX-RJ-L4")
        self._post("BOX-RJ-L3")

        self._post("BOX-RJ-L4")

        self.assertEqual(
            ScanLog.objects.filter(barcode_raw="BOX-RJ-L4").count(), 1
        )

    def test_accepted_scan_logs_no_rejection(self):
        self._box("BOX-RJ-L5")

        self.assertEqual(self._post("BOX-RJ-L5").status_code, 201)

        self.assertEqual(self._rejections().count(), 0)

    def test_unresolvable_barcode_stays_not_found_not_rejected(self):
        """NOT_FOUND and REJECTED are different failures and must stay separable."""
        response = self._post("BOX-DOES-NOT-EXIST")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._rejections().count(), 0)
        self.assertEqual(
            ScanLog.objects.filter(
                barcode_raw="BOX-DOES-NOT-EXIST", scan_result=ScanResult.NOT_FOUND
            ).count(),
            1,
        )
