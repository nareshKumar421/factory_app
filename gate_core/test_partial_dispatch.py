from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    PartialDispatchApproval,
    SalesDispatchAttachment,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
)
from gate_core.views_sales_dispatch import ensure_partial_dispatch_cleared
from vehicle_management.models import Vehicle


class PartialDispatchTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Admin")
        self.user = get_user_model().objects.create_user(
            email="partial@example.com",
            password="testpass123",
            full_name="Partial User",
            employee_code="PRT001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01PD0001")
        self.driver = Driver.objects.create(
            name="PD Driver", mobile_no="9000000001", license_no="DL-PD-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    def _docking_with_two_bills(self):
        empty_ve = VehicleEntry.objects.create(
            entry_no="EVGI-PD-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
        )
        gate_in = EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no=empty_ve.entry_no,
            vehicle_entry=empty_ve,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
        )
        plans = []
        covers = []
        for doc_entry in (111, 222):
            plan = DispatchPlan.objects.create(
                company=self.company,
                sap_invoice_doc_entry=doc_entry,
                sap_invoice_doc_num=str(doc_entry),
                booking_status=DispatchPlanStatus.BOOKED,
                vehicle=self.vehicle,
                linked_vehicle_entry=empty_ve,
            )
            covers.append(
                EmptyVehicleGateInCover.objects.create(
                    empty_vehicle_gate_in=gate_in,
                    dispatch_plan=plan,
                    sap_doc_entry=doc_entry,
                    sap_doc_num=str(doc_entry),
                )
            )
            plans.append(plan)

        dock_ve = VehicleEntry.objects.create(
            entry_no="DOCKV-PD-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status="IN_PROGRESS",
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company,
            entry_no="DOCK-PD-1",
            vehicle_entry=dock_ve,
            vehicle=self.vehicle,
            driver=self.driver,
            document_type="INVOICE",
            sap_doc_entry=111,
            dispatch_plan=plans[0],
        )
        docs = []
        for idx, (doc_entry, plan) in enumerate(zip((111, 222), plans), start=1):
            doc = SalesDispatchGateOutDocument.objects.create(
                sales_dispatch=entry,
                company=self.company,
                dispatch_plan=plan,
                document_type="INVOICE",
                sap_doc_entry=doc_entry,
            )
            SalesDispatchGateOutItem.objects.create(
                sales_dispatch=entry,
                document=doc,
                line_num=idx,
                quantity=Decimal("10"),
            )
            docs.append(doc)
        return entry, docs, plans, covers

    def test_remove_bill_reschedules_plan_and_voids_cover(self):
        entry, docs, plans, covers = self._docking_with_two_bills()

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/documents/{docs[0].id}/remove/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, 200)
        docs[0].refresh_from_db()
        self.assertFalse(docs[0].is_active)
        docs[1].refresh_from_db()
        self.assertTrue(docs[1].is_active)
        plans[0].refresh_from_db()
        self.assertIsNone(plans[0].linked_vehicle_entry_id)
        self.assertEqual(plans[0].booking_status, DispatchPlanStatus.BOOKED)
        covers[0].refresh_from_db()
        self.assertFalse(covers[0].is_active)

    def test_remove_last_bill_is_blocked(self):
        entry, docs, _plans, _covers = self._docking_with_two_bills()
        # remove first; the second becomes the only active bill -> cannot remove it
        self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/documents/{docs[0].id}/remove/",
            **self.company_header,
        )
        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/documents/{docs[1].id}/remove/",
            **self.company_header,
        )
        self.assertEqual(response.status_code, 400)

    def test_partial_dispatch_guard_blocks_until_approved_with_credit_note(self):
        entry, docs, _plans, _covers = self._docking_with_two_bills()

        # No approval -> printing is clear.
        ensure_partial_dispatch_cleared(entry)

        approval = PartialDispatchApproval.objects.create(
            company=self.company,
            sales_dispatch=entry,
            document=docs[0],
            status="PENDING",
            requested_by=self.user,
        )
        with self.assertRaises(ValueError):
            ensure_partial_dispatch_cleared(entry)

        approval.status = "APPROVED"
        approval.save(update_fields=["status"])
        with self.assertRaises(ValueError):  # approved but no credit note
            ensure_partial_dispatch_cleared(entry)

        approval.credit_note_no = "CN-2026-1"
        approval.save(update_fields=["credit_note_no"])
        SalesDispatchAttachment.objects.create(
            sales_dispatch=entry,
            attachment_type="CREDIT_NOTE",
            file="sales_dispatch/attachments/cn.pdf",
        )
        ensure_partial_dispatch_cleared(entry)  # now clear

    def test_partial_approval_request_and_decide_endpoints(self):
        entry, docs, _plans, _covers = self._docking_with_two_bills()
        item = entry.items.filter(document=docs[0]).first()

        request = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/partial-approval/",
            {
                "document_id": docs[0].id,
                "reason": "2 cartons short",
                "items": [{"item_id": item.id, "dispatched_quantity": "8"}],
            },
            format="json",
            **self.company_header,
        )
        self.assertEqual(request.status_code, 201)
        approval_id = request.data["id"]
        item.refresh_from_db()
        self.assertEqual(item.dispatched_quantity, Decimal("8"))

        decide = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/partial-approval/{approval_id}/decide/",
            {"decision": "APPROVED", "credit_note_no": "CN-9"},
            format="json",
            **self.company_header,
        )
        self.assertEqual(decide.status_code, 200)
        self.assertEqual(decide.data["status"], "APPROVED")
        self.assertEqual(decide.data["credit_note_no"], "CN-9")
