"""
Comprehensive tests for the outbound_dispatch app.
Tests models, services, views, and SAP integration.
"""
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

from django.test import TestCase, TransactionTestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from vehicle_management.models import Vehicle, VehicleType
from driver_management.models import Driver, VehicleEntry

from .models import (
    ShipmentOrder, ShipmentOrderItem, ShipmentStatus, PickStatus,
    PickTask, PickTaskStatus,
    OutboundLoadRecord, TrailerCondition,
    GoodsIssuePosting, GoodsIssueStatus,
)
from .services import OutboundService, SAPSyncService

User = get_user_model()


class TestDataMixin:
    """Mixin to create common test data."""

    def setUp(self):
        super().setUp()
        # Create user
        self.user = User.objects.create_user(
            email="test@example.com",
            password="testpass123",
            full_name="Test User",
            employee_code="EMP001",
        )
        # Give all permissions
        from django.contrib.auth.models import Permission
        all_perms = Permission.objects.filter(
            content_type__app_label="outbound_dispatch"
        )
        self.user.user_permissions.set(all_perms)
        self.user.save()
        # Clear cached permissions
        self.user = User.objects.get(pk=self.user.pk)

        # Create company
        self.company = Company.objects.create(
            name="Test Oil Company",
            code="JIVO_OIL",
            is_active=True,
        )

        # Create user-company link
        self.role, _ = UserRole.objects.get_or_create(
            name="Admin", defaults={"description": "Admin role"}
        )
        self.user_company = UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )

        # API client
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def create_shipment(self, **kwargs):
        """Helper to create a test shipment."""
        defaults = {
            "company": self.company,
            "sap_doc_entry": 100,
            "sap_doc_num": 200,
            "customer_code": "C10001",
            "customer_name": "Test Customer",
            "scheduled_date": date.today() + timedelta(days=1),
            "status": ShipmentStatus.RELEASED,
            "created_by": self.user,
        }
        defaults.update(kwargs)
        return ShipmentOrder.objects.create(**defaults)

    def create_shipment_item(self, shipment, **kwargs):
        """Helper to create a test shipment item."""
        defaults = {
            "shipment_order": shipment,
            "sap_line_num": 0,
            "item_code": "FG-001",
            "item_name": "Test Product",
            "ordered_qty": Decimal("100.000"),
            "uom": "BTL",
            "warehouse_code": "WH01",
        }
        defaults.update(kwargs)
        return ShipmentOrderItem.objects.create(**defaults)


# ====================================================================
# MODEL TESTS
# ====================================================================

class ShipmentOrderModelTest(TestDataMixin, TestCase):

    def test_create_shipment_order(self):
        shipment = self.create_shipment()
        self.assertEqual(shipment.status, ShipmentStatus.RELEASED)
        self.assertEqual(str(shipment), f"SO-{shipment.sap_doc_num} ({shipment.customer_name})")

    def test_unique_together_constraint(self):
        self.create_shipment(sap_doc_entry=999)
        with self.assertRaises(Exception):
            self.create_shipment(sap_doc_entry=999)

    def test_shipment_order_item(self):
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        self.assertEqual(item.pick_status, PickStatus.PENDING)
        self.assertEqual(item.picked_qty, Decimal("0"))
        self.assertEqual(item.packed_qty, Decimal("0"))
        self.assertEqual(item.loaded_qty, Decimal("0"))

    def test_pick_task_creation(self):
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        task = PickTask.objects.create(
            shipment_item=item,
            pick_location="WH01-A1-01",
            pick_qty=Decimal("100.000"),
        )
        self.assertEqual(task.status, PickTaskStatus.PENDING)

    def test_outbound_load_record(self):
        shipment = self.create_shipment()
        record = OutboundLoadRecord.objects.create(
            shipment_order=shipment,
            trailer_condition=TrailerCondition.CLEAN,
        )
        self.assertFalse(record.supervisor_confirmed)

    def test_goods_issue_posting(self):
        shipment = self.create_shipment()
        posting = GoodsIssuePosting.objects.create(
            shipment_order=shipment,
            status=GoodsIssueStatus.PENDING,
        )
        self.assertEqual(posting.retry_count, 0)


# ====================================================================
# SERVICE TESTS
# ====================================================================

class OutboundServiceTest(TestDataMixin, TestCase):

    def get_service(self):
        return OutboundService(company_code="JIVO_OIL")

    def test_get_shipments(self):
        service = self.get_service()
        self.create_shipment()
        self.create_shipment(sap_doc_entry=101, sap_doc_num=201)
        shipments = service.get_shipments()
        self.assertEqual(len(shipments), 2)

    def test_get_shipments_with_status_filter(self):
        service = self.get_service()
        self.create_shipment(status=ShipmentStatus.RELEASED)
        self.create_shipment(
            sap_doc_entry=101, sap_doc_num=201,
            status=ShipmentStatus.DISPATCHED
        )
        shipments = service.get_shipments({"status": "RELEASED"})
        self.assertEqual(len(shipments), 1)

    def test_get_shipment_detail(self):
        service = self.get_service()
        shipment = self.create_shipment()
        result = service.get_shipment_detail(shipment.id)
        self.assertEqual(result.id, shipment.id)

    def test_get_shipment_detail_not_found(self):
        service = self.get_service()
        with self.assertRaises(ValueError):
            service.get_shipment_detail(99999)

    def test_assign_dock_bay_valid(self):
        service = self.get_service()
        shipment = self.create_shipment()
        result = service.assign_dock_bay(shipment.id, "22")
        self.assertEqual(result.dock_bay, "22")

    def test_assign_dock_bay_invalid_zone(self):
        service = self.get_service()
        shipment = self.create_shipment()
        with self.assertRaises(ValueError, msg="Outbound dock bay must be in Zone C"):
            service.assign_dock_bay(shipment.id, "5")

    def test_generate_pick_tasks(self):
        service = self.get_service()
        shipment = self.create_shipment()
        self.create_shipment_item(shipment, sap_line_num=0)
        self.create_shipment_item(
            shipment, sap_line_num=1,
            item_code="FG-002", item_name="Product 2",
            ordered_qty=Decimal("50.000")
        )
        tasks = service.generate_pick_tasks(shipment.id)
        self.assertEqual(len(tasks), 2)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, ShipmentStatus.PICKING)

    def test_generate_pick_tasks_wrong_status(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.PACKED)
        with self.assertRaises(ValueError):
            service.generate_pick_tasks(shipment.id)

    def test_update_pick_task_complete(self):
        service = self.get_service()
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        task = PickTask.objects.create(
            shipment_item=item,
            pick_location="WH01",
            pick_qty=Decimal("100.000"),
        )
        result = service.update_pick_task(task.id, {
            "status": "COMPLETED",
            "actual_qty": "98.000",
        })
        self.assertEqual(result.status, PickTaskStatus.COMPLETED)
        self.assertIsNotNone(result.picked_at)

        item.refresh_from_db()
        self.assertEqual(item.picked_qty, Decimal("98.000"))
        self.assertEqual(item.pick_status, PickStatus.SHORT)

    def test_confirm_pack(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.PICKING)
        item = self.create_shipment_item(shipment)
        item.pick_status = PickStatus.PICKED
        item.picked_qty = Decimal("100.000")
        item.save()

        result = service.confirm_pack(shipment.id)
        self.assertEqual(result.status, ShipmentStatus.PACKED)
        item.refresh_from_db()
        self.assertEqual(item.packed_qty, Decimal("100.000"))

    def test_confirm_pack_pending_items(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.PICKING)
        self.create_shipment_item(shipment)  # pick_status = PENDING
        with self.assertRaises(ValueError, msg="has not been picked"):
            service.confirm_pack(shipment.id)

    def test_stage_shipment(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.PACKED)
        result = service.stage_shipment(shipment.id)
        self.assertEqual(result.status, ShipmentStatus.STAGED)

    def test_stage_shipment_wrong_status(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.RELEASED)
        with self.assertRaises(ValueError):
            service.stage_shipment(shipment.id)

    def test_inspect_trailer_no_vehicle(self):
        service = self.get_service()
        shipment = self.create_shipment()
        with self.assertRaises(ValueError, msg="Vehicle must be linked"):
            service.inspect_trailer(shipment.id, {"trailer_condition": "CLEAN"})

    def test_record_loading_rejected_trailer(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.STAGED)
        self.create_shipment_item(shipment)
        OutboundLoadRecord.objects.create(
            shipment_order=shipment,
            trailer_condition=TrailerCondition.REJECTED,
        )
        with self.assertRaises(ValueError, msg="rejected"):
            service.record_loading(shipment.id, [{"item_id": 1, "loaded_qty": "50"}])

    def test_record_loading_exceeds_packed(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.STAGED)
        item = self.create_shipment_item(shipment)
        item.packed_qty = Decimal("50.000")
        item.save()
        OutboundLoadRecord.objects.create(
            shipment_order=shipment,
            trailer_condition=TrailerCondition.CLEAN,
        )
        with self.assertRaises(ValueError, msg="cannot exceed"):
            service.record_loading(
                shipment.id,
                [{"item_id": item.id, "loaded_qty": "100.000"}]
            )

    def test_supervisor_confirm(self):
        service = self.get_service()
        shipment = self.create_shipment()
        OutboundLoadRecord.objects.create(shipment_order=shipment)
        result = service.supervisor_confirm(shipment.id, user=self.user)
        self.assertTrue(result.supervisor_confirmed)
        self.assertIsNotNone(result.confirmed_at)

    def test_generate_bol(self):
        service = self.get_service()
        shipment = self.create_shipment()
        self.create_shipment_item(shipment)
        bol_data = service.generate_bol(shipment.id)
        self.assertIn("BOL-", bol_data["bol_number"])
        self.assertEqual(len(bol_data["items"]), 1)

    def test_dispatch_validations(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.RELEASED)
        with self.assertRaises(ValueError, msg="must be in LOADING"):
            service.dispatch(shipment.id, "SEAL-001")

    def test_dispatch_no_supervisor_confirm(self):
        service = self.get_service()
        shipment = self.create_shipment(status=ShipmentStatus.LOADING)
        shipment.bill_of_lading_no = "BOL-001"
        shipment.save()
        OutboundLoadRecord.objects.create(
            shipment_order=shipment,
            supervisor_confirmed=False,
        )
        with self.assertRaises(ValueError, msg="Supervisor confirmation required"):
            service.dispatch(shipment.id, "SEAL-001")

    def test_dashboard(self):
        service = self.get_service()
        self.create_shipment(status=ShipmentStatus.RELEASED)
        self.create_shipment(
            sap_doc_entry=101, sap_doc_num=201,
            status=ShipmentStatus.DISPATCHED
        )
        dashboard = service.get_dashboard()
        self.assertEqual(dashboard["total_shipments"], 2)
        self.assertEqual(dashboard["by_status"]["RELEASED"], 1)
        self.assertEqual(dashboard["by_status"]["DISPATCHED"], 1)


# ====================================================================
# SAP SYNC SERVICE TESTS
# ====================================================================

class SAPSyncServiceTest(TestDataMixin, TestCase):

    @patch("outbound_dispatch.services.sap_sync_service.SAPClient")
    def test_sync_creates_shipments(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.get_open_sales_orders.return_value = [
            {
                "doc_entry": 500,
                "doc_num": 600,
                "customer_code": "C10001",
                "customer_name": "Test Customer",
                "ship_to_address": "123 Street",
                "due_date": "2026-04-01",
                "branch_id": 1,
                "comments": "",
                "items": [
                    {
                        "line_num": 0,
                        "item_code": "FG-001",
                        "item_name": "Product 1",
                        "ordered_qty": 100.0,
                        "delivered_qty": 0.0,
                        "remaining_qty": 100.0,
                        "uom": "BTL",
                        "warehouse_code": "WH01",
                        "batch_number": "",
                    }
                ],
            }
        ]

        service = SAPSyncService(company_code="JIVO_OIL")
        result = service.sync_sales_orders(user=self.user)

        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["total_from_sap"], 1)
        self.assertEqual(ShipmentOrder.objects.count(), 1)
        self.assertEqual(ShipmentOrderItem.objects.count(), 1)

    @patch("outbound_dispatch.services.sap_sync_service.SAPClient")
    def test_sync_skips_existing_non_released(self, MockSAPClient):
        # Create existing shipment in PICKING status
        self.create_shipment(
            sap_doc_entry=500, sap_doc_num=600,
            status=ShipmentStatus.PICKING
        )

        mock_client = MockSAPClient.return_value
        mock_client.get_open_sales_orders.return_value = [
            {
                "doc_entry": 500,
                "doc_num": 600,
                "customer_code": "C10001",
                "customer_name": "Test Customer",
                "due_date": "2026-04-01",
                "items": [],
            }
        ]

        service = SAPSyncService(company_code="JIVO_OIL")
        result = service.sync_sales_orders(user=self.user)

        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["created_count"], 0)


# ====================================================================
# VIEW / API TESTS
# ====================================================================

class ShipmentAPITest(TestDataMixin, TestCase):

    def test_list_shipments(self):
        self.create_shipment()
        response = self.client.get("/api/v1/outbound/shipments/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_list_shipments_filter_status(self):
        self.create_shipment(status=ShipmentStatus.RELEASED)
        self.create_shipment(
            sap_doc_entry=101, sap_doc_num=201,
            status=ShipmentStatus.DISPATCHED
        )
        response = self.client.get("/api/v1/outbound/shipments/?status=RELEASED")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_shipment_detail(self):
        shipment = self.create_shipment()
        self.create_shipment_item(shipment)
        response = self.client.get(f"/api/v1/outbound/shipments/{shipment.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sap_doc_num"], 200)
        self.assertEqual(len(response.data["items"]), 1)

    def test_shipment_detail_not_found(self):
        response = self.client.get("/api/v1/outbound/shipments/99999/")
        self.assertEqual(response.status_code, 404)

    def test_assign_bay(self):
        shipment = self.create_shipment()
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/assign-bay/",
            {"dock_bay": "25"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["dock_bay"], "25")

    def test_assign_bay_invalid_zone(self):
        shipment = self.create_shipment()
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/assign-bay/",
            {"dock_bay": "5"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_generate_picks(self):
        shipment = self.create_shipment()
        self.create_shipment_item(shipment)
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/generate-picks/",
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(PickTask.objects.count(), 1)

    def test_pick_task_list(self):
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        PickTask.objects.create(
            shipment_item=item, pick_location="WH01", pick_qty=Decimal("100"),
        )
        response = self.client.get(
            f"/api/v1/outbound/shipments/{shipment.id}/pick-tasks/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_update_pick_task(self):
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        task = PickTask.objects.create(
            shipment_item=item, pick_location="WH01", pick_qty=Decimal("100"),
        )
        response = self.client.patch(
            f"/api/v1/outbound/pick-tasks/{task.id}/",
            {"status": "COMPLETED", "actual_qty": "100.000"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "COMPLETED")

    def test_scan_barcode(self):
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        task = PickTask.objects.create(
            shipment_item=item, pick_location="WH01", pick_qty=Decimal("100"),
        )
        response = self.client.post(
            f"/api/v1/outbound/pick-tasks/{task.id}/scan/",
            {"barcode": "FG-001-BATCH1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["scanned_barcode"], "FG-001-BATCH1")
        self.assertEqual(response.data["status"], "IN_PROGRESS")

    def test_confirm_pack(self):
        shipment = self.create_shipment(status=ShipmentStatus.PICKING)
        item = self.create_shipment_item(shipment)
        item.pick_status = PickStatus.PICKED
        item.picked_qty = Decimal("100")
        item.save()

        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/confirm-pack/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "PACKED")

    def test_stage_shipment(self):
        shipment = self.create_shipment(status=ShipmentStatus.PACKED)
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/stage/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "STAGED")

    def test_inspect_trailer(self):
        shipment = self.create_shipment()
        # Need an outbound vehicle entry
        from outbound_gatein.models import OutboundGateEntry
        vtype = VehicleType.objects.create(name="TRUCK")
        vehicle = Vehicle.objects.create(
            vehicle_number="KA01AB1234",
            vehicle_type=vtype,
        )
        driver = Driver.objects.create(
            name="Test Driver",
            mobile_no="9999999999",
            license_no="DL123456",
        )
        ve = VehicleEntry.objects.create(
            company=self.company,
            vehicle=vehicle,
            driver=driver,
            entry_type="OUTBOUND",
            entry_no="GE-TEST-001",
        )
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        shipment.vehicle_entry = ve
        shipment.save()

        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/inspect-trailer/",
            {"trailer_condition": "CLEAN", "trailer_temp_ok": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["trailer_condition"], "CLEAN")

    def test_supervisor_confirm(self):
        shipment = self.create_shipment()
        OutboundLoadRecord.objects.create(shipment_order=shipment)
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/supervisor-confirm/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["supervisor_confirmed"])

    def test_generate_bol(self):
        shipment = self.create_shipment()
        self.create_shipment_item(shipment)
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/generate-bol/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("BOL-", response.data["bol_number"])

    @patch("outbound_dispatch.services.outbound_service.SAPClient")
    def test_dispatch(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.create_goods_issue.return_value = {
            "DocEntry": 777,
            "DocNum": 888,
            "DocTotal": 50000.0,
        }

        shipment = self.create_shipment(status=ShipmentStatus.LOADING)
        shipment.bill_of_lading_no = "BOL-TEST-001"
        shipment.save()
        item = self.create_shipment_item(shipment)
        item.loaded_qty = Decimal("100")
        item.save()
        OutboundLoadRecord.objects.create(
            shipment_order=shipment,
            supervisor_confirmed=True,
        )

        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/dispatch/",
            {"seal_number": "SEAL-001", "branch_id": 1},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "DISPATCHED")

        # Verify Goods Issue posting
        gi = GoodsIssuePosting.objects.get(shipment_order=shipment)
        self.assertEqual(gi.status, GoodsIssueStatus.POSTED)
        self.assertEqual(gi.sap_doc_num, 888)

    def test_dispatch_missing_seal(self):
        shipment = self.create_shipment(status=ShipmentStatus.LOADING)
        response = self.client.post(
            f"/api/v1/outbound/shipments/{shipment.id}/dispatch/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_goods_issue_status(self):
        shipment = self.create_shipment()
        GoodsIssuePosting.objects.create(
            shipment_order=shipment,
            sap_doc_num=888,
            status=GoodsIssueStatus.POSTED,
        )
        response = self.client.get(
            f"/api/v1/outbound/shipments/{shipment.id}/goods-issue/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sap_doc_num"], 888)

    def test_goods_issue_not_found(self):
        shipment = self.create_shipment()
        response = self.client.get(
            f"/api/v1/outbound/shipments/{shipment.id}/goods-issue/"
        )
        self.assertEqual(response.status_code, 404)

    def test_dashboard(self):
        self.create_shipment()
        response = self.client.get("/api/v1/outbound/dashboard/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("total_shipments", response.data)
        self.assertIn("by_status", response.data)

    @patch("outbound_dispatch.services.sap_sync_service.SAPClient")
    def test_sync_api(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.get_open_sales_orders.return_value = []

        response = self.client.post(
            "/api/v1/outbound/shipments/sync/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total_from_sap"], 0)

    def test_unauthenticated_access(self):
        client = APIClient()  # No auth
        response = client.get("/api/v1/outbound/shipments/")
        self.assertEqual(response.status_code, 401)

    def test_missing_company_header(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        # No Company-Code header
        response = client.get("/api/v1/outbound/shipments/")
        self.assertEqual(response.status_code, 403)


# ====================================================================
# WORKFLOW INTEGRATION TEST
# ====================================================================

class FullWorkflowTest(TestDataMixin, TestCase):
    """Test the complete outbound workflow from RELEASED to DISPATCHED."""

    @patch("outbound_dispatch.services.outbound_service.SAPClient")
    def test_full_workflow(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.create_goods_issue.return_value = {
            "DocEntry": 999, "DocNum": 1000, "DocTotal": 25000.0,
        }

        service = OutboundService(company_code="JIVO_OIL")

        # 1. Create shipment (simulating SAP sync)
        shipment = self.create_shipment()
        item = self.create_shipment_item(shipment)
        self.assertEqual(shipment.status, ShipmentStatus.RELEASED)

        # 2. Assign dock bay
        shipment = service.assign_dock_bay(shipment.id, "25")
        self.assertEqual(shipment.dock_bay, "25")

        # 3. Generate picks
        tasks = service.generate_pick_tasks(shipment.id)
        self.assertEqual(len(tasks), 1)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, ShipmentStatus.PICKING)

        # 4. Complete picking
        service.update_pick_task(tasks[0].id, {
            "status": "COMPLETED", "actual_qty": "100.000"
        })

        # 5. Confirm pack
        shipment = service.confirm_pack(shipment.id)
        self.assertEqual(shipment.status, ShipmentStatus.PACKED)

        # 6. Stage
        shipment = service.stage_shipment(shipment.id)
        self.assertEqual(shipment.status, ShipmentStatus.STAGED)

        # 7. Link vehicle (create outbound vehicle entry)
        from outbound_gatein.models import OutboundGateEntry
        vtype = VehicleType.objects.create(name="TRUCK")
        vehicle = Vehicle.objects.create(
            vehicle_number="KA01AB5678", vehicle_type=vtype,
        )
        driver = Driver.objects.create(
            name="Driver", mobile_no="8888888888", license_no="DL999",
        )
        ve = VehicleEntry.objects.create(
            company=self.company, vehicle=vehicle, driver=driver,
            entry_type="OUTBOUND", entry_no="GE-FLOW-001",
        )
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        shipment = service.link_vehicle(shipment.id, ve.id)
        self.assertIsNotNone(shipment.vehicle_entry)

        # 8. Inspect trailer
        record = service.inspect_trailer(shipment.id, {
            "trailer_condition": "CLEAN",
            "trailer_temp_ok": True,
        }, user=self.user)
        self.assertEqual(record.trailer_condition, TrailerCondition.CLEAN)

        # 9. Load truck
        item.refresh_from_db()
        shipment = service.record_loading(shipment.id, [
            {"item_id": item.id, "loaded_qty": "100.000"}
        ], user=self.user)
        self.assertEqual(shipment.status, ShipmentStatus.LOADING)

        # 10. Supervisor confirm
        record = service.supervisor_confirm(shipment.id, user=self.user)
        self.assertTrue(record.supervisor_confirmed)

        # 11. Generate BOL
        bol = service.generate_bol(shipment.id)
        self.assertIn("BOL-", bol["bol_number"])

        # 12. Dispatch
        shipment = service.dispatch(
            shipment.id, seal_number="SEAL-FLOW-001",
            user=self.user, branch_id=1,
        )
        self.assertEqual(shipment.status, ShipmentStatus.DISPATCHED)
        self.assertEqual(shipment.seal_number, "SEAL-FLOW-001")

        # Verify Goods Issue was posted
        gi = GoodsIssuePosting.objects.get(shipment_order=shipment)
        self.assertEqual(gi.status, GoodsIssueStatus.POSTED)
        self.assertEqual(gi.sap_doc_num, 1000)
