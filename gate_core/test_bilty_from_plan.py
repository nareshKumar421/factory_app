"""The gatepass reads its bilty off the dispatch plan, not off the docking.

A bilty (LR) is issued per consignee. Its number and date are entered when the
vehicle is linked and are read-only on the docking; its file is uploaded on the
docking after scanning, which copies it onto that customer's plans. The gate
refuses to let a load out until the plan holds all three.

What these pin is the boundary: every customer on the docking needs its own
complete bilty on its own plan, the printed gatepass carries that customer's
number rather than a neighbour's, and a load raised before the move — whose
bilty only ever lived on a docking attachment — is not suddenly blocked.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchAttachment,
    SalesDispatchAttachmentType,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from gate_core.services.sales_dispatch_gatepass import get_gatepass_readiness
from gate_core.services.sales_dispatch_gatepass_pdf import bilty_for_customer
from vehicle_management.models import Transporter, Vehicle


class _DockingFixture:
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_B")
        role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="bilty@example.com", password="p", full_name="Dock", employee_code="DKB",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="Delhi Punjab")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR67D9271", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Sumit", mobile_no="9729209417", license_no="DL-8"
        )
        self.entry = self._docking()

    def _docking(self):
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-BLT-1", company=self.company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-BLT-1", vehicle_entry=vehicle_entry,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=90001,
            sap_doc_num="90001", status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )

    def _plan(self, doc_entry, *, bilty_no="", bilty_date=None, with_file=False):
        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            bilty_no=bilty_no,
            bilty_date=bilty_date,
            created_by=self.user,
            updated_by=self.user,
        )
        if with_file:
            plan.bilty_attachment.save(
                f"lr-{doc_entry}.pdf", SimpleUploadedFile(
                    f"lr-{doc_entry}.pdf", b"scan", content_type="application/pdf"
                ),
                save=True,
            )
        return plan

    def _document(self, doc_num, *, customer_code, customer_name, plan=None):
        return SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.entry, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=int(doc_num), sap_doc_num=doc_num,
            customer_code=customer_code, customer_name=customer_name,
            dispatch_plan=plan, created_by=self.user, updated_by=self.user,
        )

    def _readiness(self):
        entry = SalesDispatchGateOut.objects.prefetch_related(
            "documents__dispatch_plan", "attachments"
        ).get(id=self.entry.id)
        return get_gatepass_readiness(entry)


class BiltyReadFromThePlanTests(_DockingFixture, TestCase):
    # ----- the gate -------------------------------------------------------

    def test_a_complete_plan_bilty_satisfies_the_gate(self):
        plan = self._plan(90001, bilty_no="NCR-4494", bilty_date=date(2026, 9, 20), with_file=True)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    def test_a_number_with_no_scan_does_not(self):
        """All three, as the old attachment rule demanded: the LR copy is what
        SAP receives on the service GRPO afterwards."""
        plan = self._plan(90001, bilty_no="NCR-4494", bilty_date=date(2026, 9, 20))
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)
        self.assertIn("bilty_attachment", self._readiness()["missing"])

    def test_a_scan_with_no_number_does_not(self):
        plan = self._plan(90001, bilty_date=date(2026, 9, 20), with_file=True)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)
        self.assertIn("bilty_attachment", self._readiness()["missing"])

    def test_every_customer_on_the_truck_needs_its_own(self):
        covered = self._plan(
            90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20), with_file=True
        )
        bare = self._plan(90002)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=covered)
        self._document("90002", customer_code="C2", customer_name="Sharma", plan=bare)
        self.assertIn("bilty_attachment", self._readiness()["missing"])

    def test_a_bill_added_later_cannot_ride_in_on_its_neighbours_lr(self):
        """Same customer, two bills, only one of them linked with a bilty. The
        LR covers a consignment, not a customer in general."""
        covered = self._plan(
            90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20), with_file=True
        )
        added_later = self._plan(90002)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=covered)
        self._document("90002", customer_code="C1", customer_name="Goel", plan=added_later)
        self.assertIn("bilty_attachment", self._readiness()["missing"])

    def test_both_customers_covered_clears_the_gate(self):
        first = self._plan(90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20), with_file=True)
        second = self._plan(90002, bilty_no="NCR-B", bilty_date=date(2026, 9, 20), with_file=True)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=first)
        self._document("90002", customer_code="C2", customer_name="Sharma", plan=second)
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    def test_a_bill_with_no_plan_falls_back_to_the_docking_attachment(self):
        """A load raised before the capture moved. Its bilty only ever lived on
        the docking, and blocking it now would strand work already in flight."""
        self._document("90001", customer_code="C1", customer_name="Goel", plan=None)
        SalesDispatchAttachment.objects.create(
            sales_dispatch=self.entry,
            attachment_type=SalesDispatchAttachmentType.BILTY,
            customer_code="C1",
            customer_name="Goel",
            bilty_no="OLD-1",
            bilty_date=date(2026, 9, 1),
            file=SimpleUploadedFile("old.pdf", b"scan", content_type="application/pdf"),
            original_filename="old.pdf",
            uploaded_by=self.user,
        )
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    def test_a_docking_with_no_documents_reads_its_own_plan(self):
        self.entry.dispatch_plan = self._plan(
            90001, bilty_no="NCR-4494", bilty_date=date(2026, 9, 20), with_file=True
        )
        self.entry.save(update_fields=["dispatch_plan"])
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    # ----- what gets printed ---------------------------------------------

    def test_the_gatepass_prints_the_customers_own_number(self):
        goel = self._plan(90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20), with_file=True)
        sharma = self._plan(90002, bilty_no="NCR-B", bilty_date=date(2026, 9, 21), with_file=True)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=goel)
        self._document("90002", customer_code="C2", customer_name="Sharma", plan=sharma)
        entry = SalesDispatchGateOut.objects.prefetch_related(
            "documents__dispatch_plan", "attachments"
        ).get(id=self.entry.id)

        self.assertEqual(bilty_for_customer(entry, "C1"), ("NCR-A", date(2026, 9, 20)))
        self.assertEqual(bilty_for_customer(entry, "C2"), ("NCR-B", date(2026, 9, 21)))

    def test_the_plan_wins_over_a_stale_docking_attachment(self):
        """Both present on a load that straddles the move. What the driver is
        carrying is the LR the dispatch desk recorded, so that is what prints."""
        plan = self._plan(90001, bilty_no="NCR-NEW", bilty_date=date(2026, 9, 20), with_file=True)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)
        SalesDispatchAttachment.objects.create(
            sales_dispatch=self.entry,
            attachment_type=SalesDispatchAttachmentType.BILTY,
            customer_code="C1",
            customer_name="Goel",
            bilty_no="OLD-1",
            bilty_date=date(2026, 9, 1),
            file=SimpleUploadedFile("old.pdf", b"scan", content_type="application/pdf"),
            original_filename="old.pdf",
            uploaded_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.prefetch_related(
            "documents__dispatch_plan", "attachments"
        ).get(id=self.entry.id)
        self.assertEqual(bilty_for_customer(entry, "C1")[0], "NCR-NEW")


class BiltyFileUploadedAtTheDockingTests(_DockingFixture, TestCase):
    """The docking takes the bilty's file; Vehicle Linking owns its number."""

    def setUp(self):
        super().setUp()
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="gate_core",
                codename="can_upload_sales_dispatch_photo",
            )
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _upload(self, customer_code, customer_name, **fields):
        return self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{self.entry.id}/attachments/",
            {
                "attachment_type": SalesDispatchAttachmentType.BILTY,
                "file": SimpleUploadedFile("lr.pdf", b"scan", content_type="application/pdf"),
                "customer_code": customer_code,
                "customer_name": customer_name,
                **fields,
            },
            format="multipart",
            HTTP_COMPANY_CODE=self.company.code,
        )

    def test_the_file_lands_on_the_plan_and_the_linked_number_stands(self):
        plan = self._plan(90001, bilty_no="NCR-4494", bilty_date=date(2026, 9, 20))
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)

        # A number typed here is ignored: the one entered at linking stands.
        response = self._upload("C1", "Goel", bilty_no="TYPED-HERE")

        self.assertEqual(response.status_code, 201, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.bilty_no, "NCR-4494")
        self.assertTrue(plan.bilty_attachment)
        self.assertEqual(response.data["bilty_no"], "NCR-4494")
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    def test_a_truck_linked_before_the_number_was_asked_takes_it_here(self):
        plan = self._plan(90001)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)

        response = self._upload("C1", "Goel", bilty_no="OLD-77", bilty_date="2026-10-04")

        self.assertEqual(response.status_code, 201, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.bilty_no, "OLD-77")
        self.assertEqual(plan.bilty_date, date(2026, 10, 4))
        self.assertTrue(plan.bilty_attachment)
        self.assertNotIn("bilty_attachment", self._readiness()["missing"])

    def test_with_no_number_anywhere_the_upload_is_refused(self):
        plan = self._plan(90001)
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)

        response = self._upload("C1", "Goel")

        self.assertEqual(response.status_code, 400)
        self.assertIn("bilty number and date", response.data["detail"])
        plan.refresh_from_db()
        self.assertFalse(plan.bilty_attachment)
        self.assertFalse(SalesDispatchAttachment.objects.exists())

    def test_one_customers_file_stays_off_the_other_customers_plans(self):
        goel = self._plan(90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20))
        sharma = self._plan(90002, bilty_no="NCR-B", bilty_date=date(2026, 9, 20))
        self._document("90001", customer_code="C1", customer_name="Goel", plan=goel)
        self._document("90002", customer_code="C2", customer_name="Sharma", plan=sharma)

        self.assertEqual(self._upload("C1", "Goel").status_code, 201)

        goel.refresh_from_db()
        sharma.refresh_from_db()
        self.assertTrue(goel.bilty_attachment)
        self.assertFalse(sharma.bilty_attachment)
        self.assertIn("bilty_attachment", self._readiness()["missing"])

    def test_a_customer_with_no_plan_never_borrows_a_neighbours(self):
        """An unlinked bill's bilty stays on the docking; it must not land on
        the plan of another customer riding the same truck."""
        goel = self._plan(90001, bilty_no="NCR-A", bilty_date=date(2026, 9, 20))
        self._document("90001", customer_code="C1", customer_name="Goel", plan=goel)
        self._document("90002", customer_code="C2", customer_name="Sharma", plan=None)

        response = self._upload("C2", "Sharma", bilty_no="LOOSE-1", bilty_date="2026-10-04")

        self.assertEqual(response.status_code, 201, response.data)
        goel.refresh_from_db()
        self.assertFalse(goel.bilty_attachment)
        self.assertEqual(goel.bilty_no, "NCR-A")

    def test_a_second_upload_replaces_the_customers_first(self):
        plan = self._plan(90001, bilty_no="NCR-4494", bilty_date=date(2026, 9, 20))
        self._document("90001", customer_code="C1", customer_name="Goel", plan=plan)

        self._upload("C1", "Goel")
        self._upload("C1", "Goel")

        self.assertEqual(
            SalesDispatchAttachment.objects.filter(
                attachment_type=SalesDispatchAttachmentType.BILTY
            ).count(),
            1,
        )
