from unittest.mock import patch
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from .models import Notification, NotificationPreference, NotificationType

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOPosting, GRPOStatus, ServiceGRPOPosting
from grpo.notifications import notify_material_grpo_failed, notify_service_grpo_failed
from quality_control.enums import ArrivalSlipStatus, FactoryHeadDecision, InspectionStatus
from quality_control.models import MaterialArrivalSlip, RawMaterialInspection
from quality_control.services.rules import update_entry_status
from raw_material_gatein.models import POItemReceipt, POReceipt
from raw_material_gatein.notifications import (
    notify_gate_entry_completed,
    notify_po_received,
)
from security_checks.models import SecurityCheck
from vehicle_management.models import Vehicle
from weighment.models import Weighment


class NotificationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            email="notify@example.com",
            password="password",
            full_name="Notify User",
            employee_code="EMP-NOTIFY",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="password",
            full_name="Other User",
            employee_code="EMP-OTHER",
        )
        self.client.force_authenticate(user=self.user)

    def test_list_supports_frontend_limit_offset_and_count(self):
        Notification.objects.create(
            recipient=self.user,
            title="First",
            body="Unread notification",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )
        Notification.objects.create(
            recipient=self.user,
            title="Second",
            body="Read notification",
            notification_type=NotificationType.GRPO_POSTED,
            is_read=True,
        )
        Notification.objects.create(
            recipient=self.other_user,
            title="Other",
            body="Should not leak",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )

        response = self.client.get(
            "/api/v1/notifications/",
            {"limit": 1, "offset": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["total_count"], 2)
        self.assertEqual(response.data["unread_count"], 1)
        self.assertEqual(response.data["limit"], 1)
        self.assertEqual(response.data["offset"], 1)
        self.assertEqual(len(response.data["results"]), 1)

    def test_detail_marks_own_notification_as_read(self):
        notification = Notification.objects.create(
            recipient=self.user,
            title="Detail",
            body="Open me",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )

        response = self.client.get(f"/api/v1/notifications/{notification.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)

    def test_preferences_default_enabled_and_update_by_type(self):
        response = self.client.get("/api/v1/notifications/preferences/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(NotificationType.choices))
        general = next(
            item
            for item in response.data
            if item["code"] == NotificationType.GENERAL_ANNOUNCEMENT
        )
        self.assertTrue(general["is_enabled"])

        response = self.client.post(
            "/api/v1/notifications/preferences/",
            {
                "notification_type": NotificationType.GRPO_POSTED,
                "is_enabled": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["code"], NotificationType.GRPO_POSTED)
        self.assertFalse(response.data["is_enabled"])
        self.assertFalse(
            NotificationPreference.objects.get(
                user=self.user,
                notification_type=NotificationType.GRPO_POSTED,
            ).is_enabled
        )

    @patch("notifications.views.NotificationService._send_to_tokens")
    def test_test_notification_endpoint_sends_to_fcm_token(self, send_to_tokens):
        send_to_tokens.return_value = {
            "success_count": 1,
            "failure_count": 0,
            "responses": [],
        }

        response = self.client.post(
            "/api/v1/notifications/test/",
            {
                "token": "valid-looking-token",
                "title": "Test title",
                "body": "Test body",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        send_to_tokens.assert_called_once()
        _, kwargs = send_to_tokens.call_args
        self.assertEqual(kwargs["tokens"], ["valid-looking-token"])
        self.assertEqual(kwargs["title"], "Test title")
        self.assertEqual(kwargs["body"], "Test body")
        self.assertEqual(
            kwargs["data"]["notification_type"],
            NotificationType.GENERAL_ANNOUNCEMENT,
        )


class WorkflowNotificationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.company = Company.objects.create(name="Notify Flow Co", code="NFC")
        self.role = UserRole.objects.create(name="Notifier")
        self.actor = User.objects.create_user(
            email="actor@example.com",
            password="password",
            full_name="Flow Actor",
            employee_code="FLOW-ACTOR",
        )
        UserCompany.objects.create(user=self.actor, company=self.company, role=self.role)

        self.raw_user = self._create_group_user(
            email="raw@example.com",
            employee_code="FLOW-RAW",
            group_name="raw_material_gatein",
        )
        self.qc_store_user = self._create_group_user(
            email="qc-store@example.com",
            employee_code="FLOW-QCSTORE",
            group_name="qc_store",
        )
        self.qc_chemist_user = self._create_group_user(
            email="qc-chemist@example.com",
            employee_code="FLOW-QCCHEM",
            group_name="qc_chemist",
        )
        self.qc_manager_user = self._create_group_user(
            email="qc-manager@example.com",
            employee_code="FLOW-QCMGR",
            group_name="qc_manager",
        )
        self.grpo_user = self._create_group_user(
            email="grpo@example.com",
            employee_code="FLOW-GRPO",
            group_name="grpo",
        )
        self.factory_head_user = self._create_group_user(
            email="factory-head@example.com",
            employee_code="FLOW-FH",
            group_name="factory head",
        )
        factory_head_permission = Permission.objects.get(
            codename="can_factory_head_decision",
            content_type__app_label="quality_control",
        )
        self.factory_head_user.user_permissions.add(factory_head_permission)

        self.vehicle = Vehicle.objects.create(vehicle_number="HR55FLOW001")
        self.driver = Driver.objects.create(
            name="Flow Driver",
            mobile_no="9999999999",
            license_no="FLOW-DL",
        )
        self.sequence = 0

    def _create_group_user(self, *, email, employee_code, group_name):
        User = get_user_model()
        user = User.objects.create_user(
            email=email,
            password="password",
            full_name=email.split("@")[0],
            employee_code=employee_code,
        )
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _entry(self, status=GateEntryStatus.IN_PROGRESS):
        self.sequence += 1
        return VehicleEntry.objects.create(
            entry_no=f"FLOW-{self.sequence:03d}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=status,
            created_by=self.actor,
            updated_by=self.actor,
        )

    def _po_item(self, entry):
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number=f"PO-{entry.entry_no}",
            supplier_code="SUP-FLOW",
            supplier_name="Flow Supplier",
            created_by=self.actor,
        )
        po_item = POItemReceipt.objects.create(
            po_receipt=po_receipt,
            po_item_code=f"ITEM-{entry.entry_no}",
            item_name="Flow Item",
            ordered_qty=Decimal("10.000"),
            received_qty=Decimal("10.000"),
            uom="KG",
            created_by=self.actor,
        )
        return po_receipt, po_item

    def _submitted_slip(self, po_item):
        return MaterialArrivalSlip.objects.create(
            po_item_receipt=po_item,
            particulars=po_item.item_name,
            arrival_datetime=timezone.now(),
            weighing_required=False,
            party_name=po_item.po_receipt.supplier_name,
            billing_qty=Decimal("10.000"),
            billing_uom=po_item.uom,
            truck_no_as_per_bill=self.vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED,
            is_submitted=True,
            submitted_at=timezone.now(),
            submitted_by=self.actor,
            created_by=self.actor,
        )

    def _inspection(self, slip):
        self.sequence += 1
        return RawMaterialInspection.objects.create(
            arrival_slip=slip,
            report_no=f"RPT-FLOW-{self.sequence:04d}",
            internal_lot_no=f"LOT-FLOW-{self.sequence:04d}",
            inspection_date=timezone.localdate(),
            description_of_material=slip.po_item_receipt.item_name,
            sap_code=slip.po_item_receipt.po_item_code,
            supplier_name=slip.po_item_receipt.po_receipt.supplier_name,
            purchase_order_no=slip.po_item_receipt.po_receipt.po_number,
            vehicle_no=self.vehicle.vehicle_number,
            created_by=self.actor,
        )

    def test_gate_in_notifications_are_created_for_raw_material_events(self):
        with self.captureOnCommitCallbacks(execute=True):
            entry = self._entry()

        created = Notification.objects.get(notification_type=NotificationType.GATE_ENTRY_CREATED)
        self.assertEqual(created.recipient, self.raw_user)
        self.assertEqual(created.reference_id, entry.id)
        self.assertEqual(created.click_action_url, f"/gate/raw-materials/edit/{entry.id}/step1")

        security_check = SecurityCheck.objects.create(
            vehicle_entry=entry,
            inspected_by_name="Security User",
            created_by=self.actor,
            updated_by=self.actor,
        )
        with self.captureOnCommitCallbacks(execute=True):
            security_check.is_submitted = True
            security_check.updated_by = self.actor
            security_check.save()

        security = Notification.objects.get(notification_type=NotificationType.SECURITY_CHECK_DONE)
        self.assertEqual(security.recipient, self.raw_user)
        self.assertEqual(security.reference_id, security_check.id)

        with self.captureOnCommitCallbacks(execute=True):
            Weighment.objects.create(
                vehicle_entry=entry,
                gross_weight=Decimal("100.000"),
                tare_weight=Decimal("20.000"),
                created_by=self.actor,
                updated_by=self.actor,
            )

        weighment = Notification.objects.get(notification_type=NotificationType.WEIGHMENT_RECORDED)
        self.assertEqual(weighment.recipient, self.raw_user)
        self.assertIn("/step5", weighment.click_action_url)

        po_receipt, _po_item = self._po_item(entry)
        with self.captureOnCommitCallbacks(execute=True):
            notify_po_received(po_receipt, self.actor)

        po_notification = Notification.objects.get(notification_type=NotificationType.PO_RECEIVED)
        self.assertEqual(po_notification.recipient, self.raw_user)
        self.assertEqual(po_notification.reference_id, po_receipt.id)

        with self.captureOnCommitCallbacks(execute=True):
            notify_gate_entry_completed(entry, self.actor)

        completed = Notification.objects.get(
            notification_type=NotificationType.GATE_ENTRY_COMPLETED
        )
        self.assertEqual(completed.recipient, self.grpo_user)
        self.assertEqual(completed.click_action_url, f"/grpo/material/preview/{entry.id}")

    def test_qc_notifications_follow_workflow_transitions(self):
        entry = self._entry(status=GateEntryStatus.QC_PENDING)
        _po_receipt, po_item = self._po_item(entry)
        slip = MaterialArrivalSlip.objects.create(
            po_item_receipt=po_item,
            particulars=po_item.item_name,
            arrival_datetime=timezone.now(),
            weighing_required=False,
            party_name=po_item.po_receipt.supplier_name,
            billing_qty=Decimal("10.000"),
            billing_uom=po_item.uom,
            truck_no_as_per_bill=self.vehicle.vehicle_number,
            status=ArrivalSlipStatus.DRAFT,
            is_submitted=False,
            created_by=self.actor,
        )

        with self.captureOnCommitCallbacks(execute=True):
            slip.submit_to_qa(self.actor)

        submitted = Notification.objects.get(
            notification_type=NotificationType.ARRIVAL_SLIP_SUBMITTED
        )
        self.assertEqual(submitted.recipient, self.qc_store_user)
        self.assertEqual(submitted.click_action_url, "/qc/arrival-slips")

        with self.captureOnCommitCallbacks(execute=True):
            slip.send_back_to_gate(self.actor, remarks="Fix quantity")

        sent_back = Notification.objects.get(
            notification_type=NotificationType.ARRIVAL_SLIP_SENT_BACK
        )
        self.assertEqual(sent_back.recipient, self.raw_user)
        self.assertEqual(sent_back.click_action_url, f"/gate/raw-materials/edit/{entry.id}/step4")

        slip.submit_to_qa(self.actor)
        inspection = self._inspection(slip)

        with self.captureOnCommitCallbacks(execute=True):
            inspection.submit_for_approval(user=self.actor)

        submitted_inspection = Notification.objects.get(
            notification_type=NotificationType.QC_INSPECTION_SUBMITTED
        )
        self.assertEqual(submitted_inspection.recipient, self.qc_chemist_user)
        self.assertEqual(submitted_inspection.click_action_url, "/qc/arrival-slips/approvals")

        with self.captureOnCommitCallbacks(execute=True):
            inspection.approve_by_chemist(self.actor)

        chemist = Notification.objects.get(notification_type=NotificationType.QC_CHEMIST_APPROVED)
        self.assertEqual(chemist.recipient, self.qc_manager_user)

        with self.captureOnCommitCallbacks(execute=True):
            inspection.approve_by_qam(self.actor, final_status=InspectionStatus.ACCEPTED)
            update_entry_status(entry)

        qam = Notification.objects.get(notification_type=NotificationType.QC_QAM_APPROVED)
        self.assertEqual(qam.recipient, self.grpo_user)
        self.assertEqual(qam.click_action_url, f"/grpo/material/preview/{entry.id}")

        qc_completed = Notification.objects.get(notification_type=NotificationType.QC_COMPLETED)
        self.assertEqual(qc_completed.recipient, self.raw_user)
        self.assertEqual(qc_completed.click_action_url, f"/gate/raw-materials/edit/{entry.id}/review")

    def test_rejected_qc_requests_factory_head_decision_and_records_outcome(self):
        entry = self._entry(status=GateEntryStatus.QC_PENDING)
        _po_receipt, po_item = self._po_item(entry)
        slip = self._submitted_slip(po_item)
        inspection = self._inspection(slip)

        with self.captureOnCommitCallbacks(execute=True):
            inspection.reject(self.actor, remarks="Failed parameter")

        rejected = Notification.objects.get(notification_type=NotificationType.QC_REJECTED)
        self.assertEqual(rejected.recipient, self.qc_store_user)

        required = Notification.objects.get(
            notification_type=NotificationType.FACTORY_HEAD_DECISION_REQUIRED
        )
        self.assertEqual(required.recipient, self.factory_head_user)

        with self.captureOnCommitCallbacks(execute=True):
            inspection.record_factory_head_decision(
                self.actor,
                FactoryHeadDecision.RETURN_TO_VENDOR,
                remarks="Return it",
            )

        recorded = Notification.objects.get(
            notification_type=NotificationType.FACTORY_HEAD_DECISION_RECORDED
        )
        self.assertEqual(recorded.recipient, self.raw_user)
        self.assertEqual(recorded.click_action_url, "/gate/rejected-qc-return")

    def test_grpo_notifications_cover_posted_and_failed_events(self):
        entry = self._entry(status=GateEntryStatus.COMPLETED)
        po_receipt, _po_item = self._po_item(entry)
        posting = GRPOPosting.objects.create(
            vehicle_entry=entry,
            po_receipt=po_receipt,
            status=GRPOStatus.PENDING,
            posted_by=self.actor,
        )
        posting.po_receipts.set([po_receipt])

        with self.captureOnCommitCallbacks(execute=True):
            posting.sap_doc_entry = 101
            posting.sap_doc_num = 202
            posting.status = GRPOStatus.POSTED
            posting.save()

        posted = Notification.objects.get(notification_type=NotificationType.GRPO_POSTED)
        self.assertEqual(posted.recipient, self.grpo_user)
        self.assertEqual(posted.click_action_url, f"/grpo/material/history/{posting.id}")

        with self.captureOnCommitCallbacks(execute=True):
            notify_material_grpo_failed(
                company=self.company,
                user=self.actor,
                error_message="SAP validation failed",
                vehicle_entry_id=entry.id,
            )

        failed = Notification.objects.get(notification_type=NotificationType.GRPO_FAILED)
        self.assertEqual(failed.recipient, self.grpo_user)
        self.assertEqual(failed.click_action_url, f"/grpo/material/preview/{entry.id}")

        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=5001,
            sap_invoice_doc_num="INV-5001",
            booking_status="BOOKED",
            created_by=self.actor,
            updated_by=self.actor,
        )
        service_posting = ServiceGRPOPosting.objects.create(
            dispatch_plan=dispatch_plan,
            vendor_code="VEND-1",
            vendor_name="Vendor 1",
            status=GRPOStatus.PENDING,
            posted_by=self.actor,
        )

        with self.captureOnCommitCallbacks(execute=True):
            service_posting.sap_doc_entry = 303
            service_posting.sap_doc_num = 404
            service_posting.status = GRPOStatus.POSTED
            service_posting.save()

        service_posted = Notification.objects.get(
            notification_type=NotificationType.SERVICE_GRPO_POSTED
        )
        self.assertEqual(service_posted.recipient, self.grpo_user)
        self.assertEqual(
            service_posted.click_action_url,
            f"/dispatch/bilty-grpo/history/{service_posting.id}",
        )

        with self.captureOnCommitCallbacks(execute=True):
            notify_service_grpo_failed(
                company=self.company,
                user=self.actor,
                error_message="SAP unavailable",
                dispatch_plan_id=dispatch_plan.id,
            )

        service_failed = Notification.objects.get(
            notification_type=NotificationType.SERVICE_GRPO_FAILED
        )
        self.assertEqual(service_failed.recipient, self.grpo_user)
        self.assertEqual(
            service_failed.click_action_url,
            f"/dispatch/bilty-grpo/preview/{dispatch_plan.id}",
        )
