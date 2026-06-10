from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from barcode.models import Box
from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    BSTGateIn,
    EmptyVehicleGateIn,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
    SalesDispatchGatepassPrintLog,
    SalesDispatchGatepassPrintType,
    SalesDispatchLock,
)
from vehicle_management.models import Transporter, Vehicle
from weighment.models import Weighment


SALES_DISPATCH_PERMISSION_CODENAMES = [
    "can_view_sales_dispatch_out",
    "can_create_sales_dispatch_out",
    "can_edit_sales_dispatch_out",
    "can_upload_sales_dispatch_photo",
    "can_print_sales_dispatch_gatepass",
    "can_reprint_sales_dispatch_gatepass",
    "can_commit_sales_dispatch_print",
    "can_reject_sales_dispatch_out",
    "can_cancel_sales_dispatch_out",
    "can_dispatch_sales_dispatch_out",
    "can_view_sales_dispatch_reports",
    "can_manage_sales_dispatch_lock",
]


class SalesDispatchAPITests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="dock.test@example.com",
            password="testpass",
            full_name="Dock Test User",
            employee_code="DCK001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Admin")
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
        )
        self.grant_sales_dispatch_permissions(self.user)
        self.transporter = Transporter.objects.create(name="Test Transporter")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01AC0001",
            transporter=self.transporter,
        )
        self.driver = Driver.objects.create(
            name="Test Driver",
            mobile_no="9999999999",
            license_no="DL-TEST-001",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    def grant_sales_dispatch_permissions(self, user):
        permissions = Permission.objects.filter(
            content_type__app_label="gate_core",
            codename__in=SALES_DISPATCH_PERMISSION_CODENAMES,
        )
        user.user_permissions.add(*permissions)

    def create_company_user_without_permissions(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(
            email="dock.viewer@example.com",
            password="testpass",
            full_name="Dock No Permission",
            employee_code="DCK002",
        )
        UserCompany.objects.create(
            user=user,
            company=self.company,
            role=self.role,
            is_default=True,
        )
        return user

    def sap_document(
        self,
        doc_entry,
        *,
        doc_num=None,
        branch_id=1,
        card_code=None,
        card_name=None,
        eway_bill="EWB-1",
        doc_total=Decimal("100.00"),
    ):
        return {
            "document_type": SalesDispatchDocumentType.INVOICE,
            "doc_entry": doc_entry,
            "doc_num": doc_num or str(doc_entry),
            "doc_date": timezone.localdate(),
            "doc_total": doc_total,
            "branch_id": branch_id,
            "branch_name": "Jivo Oil",
            "card_code": card_code or f"CUST{doc_entry}",
            "card_name": card_name or f"Customer {doc_entry}",
            "ship_to_code": "SHIP",
            "ship_to_address": "Test Address",
            "place_of_supply": "HR",
            "bp_gstin": "GSTIN",
            "eway_bill": eway_bill,
            "vehicle_no": self.vehicle.vehicle_number,
            "transporter_name": self.transporter.name,
            "bilty_no": "BLT-1",
            "bilty_date": timezone.localdate(),
            "from_warehouse": "",
            "to_warehouse": "",
            "warehouses": "FG0000318",
            "item_summary": f"ITEM-{doc_entry} - Test Item",
            "base_refs": str(doc_entry),
            "total_quantity": Decimal("10.000"),
            "total_litres": Decimal("5.000"),
            "total_boxes": Decimal("1.000"),
            "total_weight": Decimal("80.000"),
            "items": [
                {
                    "line_num": 0,
                    "item_code": f"ITEM-{doc_entry}",
                    "item_name": "Test Item",
                    "quantity": Decimal("10.000"),
                    "uom": "BOX",
                    "warehouse_code": "FG0000318",
                    "total_weight": Decimal("80.000"),
                }
            ],
            "plan": None,
        }

    def create_sales_dispatch(
        self,
        suffix,
        *,
        status_value=SalesDispatchGateOutStatus.DOCKED,
        document_type=SalesDispatchDocumentType.INVOICE,
        dispatch_plan=None,
        with_photo=False,
        with_item=False,
        with_weighment=False,
    ):
        vehicle_entry = VehicleEntry.objects.create(
            entry_no=f"DOCKV-TEST-{suffix}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status="IN_PROGRESS",
            created_by=self.user,
            updated_by=self.user,
        )
        entry = SalesDispatchGateOut.objects.create(
            company=self.company,
            entry_no=f"DOCK-TEST-{suffix}",
            vehicle_entry=vehicle_entry,
            dispatch_plan=dispatch_plan,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            document_type=document_type,
            sap_doc_entry=1000 + int(suffix),
            sap_doc_num=f"INV-{suffix}",
            customer_code=f"C{suffix}",
            customer_name=f"Customer {suffix}",
            vehicle_no=self.vehicle.vehicle_number,
            transporter_name=self.transporter.name,
            driver_name=self.driver.name,
            driver_mobile_no=self.driver.mobile_no,
            status=status_value,
            truck_photo=f"sales_dispatch/truck_photos/{suffix}.jpg" if with_photo else None,
            photo_latitude=Decimal("28.613900") if with_photo else None,
            photo_longitude=Decimal("77.209000") if with_photo else None,
            created_by=self.user,
            updated_by=self.user,
        )
        if with_item:
            SalesDispatchGateOutItem.objects.create(
                sales_dispatch=entry,
                line_num=0,
                item_code=f"ITEM-{suffix}",
                item_name="Test Item",
                quantity=Decimal("10.000"),
                uom="BOX",
                created_by=self.user,
                updated_by=self.user,
            )
        if with_weighment:
            Weighment.objects.create(
                vehicle_entry=vehicle_entry,
                gross_weight=Decimal("1000.000"),
                tare_weight=Decimal("250.000"),
                created_by=self.user,
                updated_by=self.user,
            )
        return entry

    def create_barcode_box(self, suffix, *, item_code=None):
        box_barcode = f"BOX-20260527-L1-{int(suffix):04d}"
        return Box.objects.create(
            company=self.company,
            box_barcode=box_barcode,
            item_code=item_code or f"ITEM-{suffix}",
            item_name="Test Item",
            batch_number=f"BATCH-{suffix}",
            qty=Decimal("10.00"),
            uom="BOX",
            mfg_date=timezone.localdate(),
            exp_date=timezone.localdate() + timedelta(days=180),
            current_warehouse="FG0000318",
            created_by=self.user,
        )

    def create_box_scan(self, entry, suffix="1"):
        box = self.create_barcode_box(suffix)
        return SalesDispatchBoxScan.objects.create(
            company=self.company,
            sales_dispatch=entry,
            box=box,
            box_barcode=box.box_barcode,
            barcode_raw=box.box_barcode,
            item_code=box.item_code,
            item_name=box.item_name,
            batch_number=box.batch_number,
            quantity=box.qty,
            uom=box.uom,
            box_status=box.status,
            warehouse_code=box.current_warehouse,
            scanned_by=self.user,
            created_by=self.user,
            updated_by=self.user,
        )

    def create_dispatched_stock_transfer(self, suffix="70"):
        entry = self.create_sales_dispatch(
            suffix,
            status_value=SalesDispatchGateOutStatus.DISPATCHED,
            document_type=SalesDispatchDocumentType.STOCK_TRANSFER,
            with_item=True,
        )
        entry.sap_doc_num = f"BST-{suffix}"
        entry.from_warehouse = "SRC-WH"
        entry.to_warehouse = "DST-WH"
        entry.gate_out_date = timezone.localdate()
        entry.out_time = timezone.localtime().time().replace(microsecond=0)
        entry.save(
            update_fields=[
                "sap_doc_num",
                "from_warehouse",
                "to_warehouse",
                "gate_out_date",
                "out_time",
                "updated_at",
            ]
        )
        entry.items.update(from_warehouse="SRC-WH", to_warehouse="DST-WH")
        return entry

    def test_sales_dispatch_actions_require_docking_permissions(self):
        entry = self.create_sales_dispatch(
            "90",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
        )
        no_permission_client = APIClient()
        no_permission_client.force_authenticate(self.create_company_user_without_permissions())

        requests = [
            ("get", "/api/v1/gate-core/sales-dispatch/lock/", None),
            ("patch", "/api/v1/gate-core/sales-dispatch/lock/", {"is_locked": True, "reason": "Hold"}),
            ("get", "/api/v1/gate-core/sales-dispatch/reports/", None),
            ("get", "/api/v1/gate-core/sales-dispatch/pending-bookings/", None),
            ("get", "/api/v1/gate-core/sales-dispatch/documents/", None),
            ("get", "/api/v1/gate-core/sales-dispatch/", None),
            ("post", "/api/v1/gate-core/sales-dispatch/", {}),
            ("get", f"/api/v1/gate-core/sales-dispatch/{entry.id}/", None),
            ("patch", f"/api/v1/gate-core/sales-dispatch/{entry.id}/", {"remarks": "No"}),
            ("get", f"/api/v1/gate-core/sales-dispatch/by-vehicle-entry/{entry.vehicle_entry_id}/", None),
            ("get", f"/api/v1/gate-core/sales-dispatch/{entry.id}/attachments/", None),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/attachments/", {}),
            ("get", f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/", None),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/", {}),
            ("delete", f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/1/", None),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/preview/", {}),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/", {}),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/reprint/", {"reprint_reason": "Copy"}),
            ("get", f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/prints/", None),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/commit-print/", {}),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/dispatch/", {}),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/reject/", {"reason": "No"}),
            ("post", f"/api/v1/gate-core/sales-dispatch/{entry.id}/cancel/", {"reason": "No"}),
        ]

        for method, url, payload in requests:
            with self.subTest(method=method, url=url):
                client_method = getattr(no_permission_client, method)
                if payload is None:
                    response = client_method(url, **self.company_header)
                else:
                    response = client_method(
                        url,
                        payload,
                        format="json",
                        **self.company_header,
                    )
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_lock_endpoint_creates_default_unlocked_state(self):
        response = self.client.get(
            "/api/v1/gate-core/sales-dispatch/lock/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_locked"])
        self.assertEqual(response.data["reason"], "")
        self.assertTrue(SalesDispatchLock.objects.filter(company=self.company).exists())

    def test_lock_requires_reason_when_locking(self):
        response = self.client.patch(
            "/api/v1/gate-core/sales-dispatch/lock/",
            {"is_locked": True, "reason": ""},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)

    def test_locked_docking_blocks_gatepass_print_and_commit(self):
        print_entry = self.create_sales_dispatch(
            "1",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
        )
        commit_entry = self.create_sales_dispatch(
            "2",
            status_value=SalesDispatchGateOutStatus.GATEPASS_PRINTED,
        )
        commit_entry.gatepass_no = "DCK/JIVO_OIL/2026-27/000001"
        commit_entry.save(update_fields=["gatepass_no"])
        SalesDispatchLock.objects.create(
            company=self.company,
            is_locked=True,
            reason="Monthly close",
            changed_by=self.user,
        )

        print_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{print_entry.id}/gatepass/print/",
            {},
            format="json",
            **self.company_header,
        )
        commit_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{commit_entry.id}/commit-print/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(print_response.status_code, 423)
        self.assertEqual(commit_response.status_code, 423)
        print_entry.refresh_from_db()
        commit_entry.refresh_from_db()
        self.assertIsNone(print_entry.gatepass_no)
        self.assertEqual(print_entry.status, SalesDispatchGateOutStatus.READY_FOR_GATEPASS)
        self.assertEqual(commit_entry.status, SalesDispatchGateOutStatus.GATEPASS_PRINTED)

    def test_gatepass_original_print_is_logged_once(self):
        entry = self.create_sales_dispatch(
            "3",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
            with_photo=True,
            with_item=True,
            with_weighment=True,
        )
        self.create_box_scan(entry, "3")

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/",
            {"uom": "LTR", "physical_quantity": "80.000", "printer_name": "Dock Printer"},
            format="json",
            **self.company_header,
        )
        second_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry.refresh_from_db()
        self.assertEqual(entry.status, SalesDispatchGateOutStatus.GATEPASS_PRINTED)
        self.assertTrue(entry.gatepass_no)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reprint workflow", second_response.data["detail"])
        self.assertEqual(entry.gatepass_print_logs.count(), 1)
        log = entry.gatepass_print_logs.get()
        self.assertEqual(log.print_type, SalesDispatchGatepassPrintType.ORIGINAL)
        self.assertEqual(log.copy_number, 1)
        self.assertEqual(log.gatepass_no, entry.gatepass_no)
        self.assertEqual(log.printer_name, "Dock Printer")

    def test_gatepass_reprint_requires_reason_and_is_logged(self):
        entry = self.create_sales_dispatch(
            "4",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
            with_photo=True,
            with_item=True,
            with_weighment=True,
        )
        self.create_box_scan(entry, "4")
        self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/",
            {},
            format="json",
            **self.company_header,
        )

        missing_reason_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/reprint/",
            {"reprint_reason": ""},
            format="json",
            **self.company_header,
        )
        reprint_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/reprint/",
            {"reprint_reason": "Original copy damaged", "printer_name": "Security Printer"},
            format="json",
            **self.company_header,
        )

        self.assertEqual(missing_reason_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(reprint_response.status_code, status.HTTP_200_OK)
        logs = list(entry.gatepass_print_logs.order_by("copy_number"))
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].print_type, SalesDispatchGatepassPrintType.ORIGINAL)
        self.assertEqual(logs[1].print_type, SalesDispatchGatepassPrintType.REPRINT)
        self.assertEqual(logs[1].copy_number, 2)
        self.assertEqual(logs[1].reprint_reason, "Original copy damaged")
        self.assertEqual(logs[1].printer_name, "Security Printer")

    def test_box_scan_endpoint_records_box_for_docking(self):
        entry = self.create_sales_dispatch("6", with_item=True)
        box = self.create_barcode_box("6", item_code="ITEM-6")

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/",
            {"barcode_raw": box.box_barcode},
            format="json",
            **self.company_header,
        )
        duplicate_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/",
            {"barcode_raw": box.box_barcode},
            format="json",
            **self.company_header,
        )
        list_response = self.client.get(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/box-scans/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["box_barcode"], box.box_barcode)
        self.assertFalse(response.data["duplicate"])
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        self.assertTrue(duplicate_response.data["duplicate"])
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)
        self.assertEqual(SalesDispatchBoxScan.objects.filter(sales_dispatch=entry).count(), 1)

    def test_gatepass_print_requires_box_scans_not_weighment(self):
        entry = self.create_sales_dispatch(
            "7",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
            with_photo=True,
            with_item=True,
            with_weighment=False,
        )

        missing_scan_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/",
            {},
            format="json",
            **self.company_header,
        )
        self.create_box_scan(entry, "7")
        print_response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/print/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(missing_scan_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("box_scans", missing_scan_response.data["detail"])
        self.assertEqual(print_response.status_code, status.HTTP_200_OK)
        entry.refresh_from_db()
        self.assertTrue(entry.gatepass_no)

    def test_gatepass_print_history_endpoint_returns_logs(self):
        entry = self.create_sales_dispatch(
            "5",
            status_value=SalesDispatchGateOutStatus.GATEPASS_PRINTED,
        )
        entry.gatepass_no = "DCK/JIVO_OIL/2026-27/000005"
        entry.printed_by = self.user
        entry.printed_at = timezone.now()
        entry.save(update_fields=["gatepass_no", "printed_by", "printed_at"])
        SalesDispatchGatepassPrintLog.record_print(
            sales_dispatch=entry,
            print_type=SalesDispatchGatepassPrintType.ORIGINAL,
            user=self.user,
        )

        response = self.client.get(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/gatepass/prints/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["gatepass_no"], entry.gatepass_no)

    def test_reports_return_operational_counts(self):
        self.create_sales_dispatch("10", status_value=SalesDispatchGateOutStatus.DOCKED)
        self.create_sales_dispatch(
            "11",
            status_value=SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
            with_photo=True,
        )
        self.create_sales_dispatch(
            "12",
            status_value=SalesDispatchGateOutStatus.GATEPASS_PRINTED,
            with_photo=True,
        )
        self.create_sales_dispatch(
            "13",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
            with_photo=True,
        )
        self.create_sales_dispatch("14", status_value=SalesDispatchGateOutStatus.DISPATCHED)
        self.create_sales_dispatch("15", status_value=SalesDispatchGateOutStatus.CANCELLED)
        self.create_sales_dispatch("16", status_value=SalesDispatchGateOutStatus.REJECTED)

        response = self.client.get(
            "/api/v1/gate-core/sales-dispatch/reports/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["counts"],
            {
                "total": 7,
                "waiting_inside": 4,
                "missing_photo": 1,
                "gatepass_pending": 2,
                "printed_not_committed": 1,
                "ready_for_dispatch": 1,
                "dispatched": 1,
                "rejected_cancelled": 2,
                "truck_with_photo": 3,
            },
        )
        self.assertEqual(len(response.data["waiting_inside"]), 4)
        self.assertEqual(len(response.data["missing_photo"]), 1)
        self.assertEqual(len(response.data["printed_not_committed"]), 1)
        self.assertEqual(len(response.data["ready_for_dispatch"]), 1)
        self.assertEqual(len(response.data["dispatched"]), 1)
        self.assertEqual(len(response.data["rejected_cancelled"]), 2)
        self.assertEqual(len(response.data["truck_vs_invoices_with_photo"]), 3)
        self.assertEqual(len(response.data["truck_status_with_photo"]), 3)

    def test_pending_bookings_endpoint_groups_booked_dispatch_plans(self):
        linked_vehicle_entry = VehicleEntry.objects.create(
            entry_no="VEH-LINK-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no=linked_vehicle_entry.entry_no,
            vehicle_entry=linked_vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            created_by=self.user,
            updated_by=self.user,
        )
        first_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=80001,
            sap_invoice_doc_num="80001",
            invoice_number="INV-80001",
            invoice_weight=Decimal("10.000"),
            invoice_amount=Decimal("100.00"),
            product_variety="Oil",
            total_litres=Decimal("40.000"),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            linked_vehicle_entry=linked_vehicle_entry,
            bilty_no="BLT-BOOKED",
            bilty_date=timezone.localdate(),
            freight=Decimal("50.00"),
            total_freight=Decimal("50.00"),
            created_by=self.user,
            updated_by=self.user,
        )
        second_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=80002,
            sap_invoice_doc_num="80002",
            invoice_number="INV-80002",
            invoice_weight=Decimal("20.000"),
            invoice_amount=Decimal("200.00"),
            product_variety="Oil",
            total_litres=Decimal("60.000"),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            linked_vehicle_entry=linked_vehicle_entry,
            bilty_no="BLT-BOOKED",
            bilty_date=timezone.localdate(),
            freight=Decimal("75.00"),
            total_freight=Decimal("75.00"),
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.get(
            "/api/v1/gate-core/sales-dispatch/pending-bookings/",
            {
                "from_date": timezone.localdate().isoformat(),
                "to_date": timezone.localdate().isoformat(),
            },
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        group = response.data[0]
        self.assertEqual(group["row_type"], "PENDING_BOOKING")
        self.assertEqual(group["status"], "PENDING_DOCKING")
        self.assertEqual(group["dispatch_plan_ids"], [first_plan.id, second_plan.id])
        self.assertEqual(group["document_numbers"], ["80001", "80002"])
        self.assertEqual(group["vehicle"], self.vehicle.id)
        self.assertEqual(group["driver"], self.driver.id)
        self.assertEqual(group["vehicle_entry"], linked_vehicle_entry.id)
        self.assertEqual(group["total_litres"], Decimal("100.000"))
        self.assertEqual(group["total_weight"], Decimal("30.000"))
        self.assertEqual(len(group["documents"]), 2)

    def test_pending_bookings_use_empty_vehicle_gate_in_driver(self):
        planning_driver = Driver.objects.create(
            name="Planning Driver",
            mobile_no="9888888888",
            license_no="DL-PLAN-001",
        )
        gate_driver = Driver.objects.create(
            name="Gate Driver",
            mobile_no="9777777777",
            license_no="DL-GATE-001",
            id_proof_type="Aadhaar",
            id_proof_number="1234",
        )
        linked_vehicle_entry = VehicleEntry.objects.create(
            entry_no="EVGI-LINK-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=gate_driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no=linked_vehicle_entry.entry_no,
            vehicle_entry=linked_vehicle_entry,
            vehicle=self.vehicle,
            driver=gate_driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            created_by=self.user,
            updated_by=self.user,
        )
        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=80003,
            sap_invoice_doc_num="80003",
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=planning_driver,
            driver_name="Planning Driver Snapshot",
            driver_mobile_no="9666666666",
            linked_vehicle_entry=linked_vehicle_entry,
            bilty_no="BLT-GATE-DRIVER",
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.get(
            "/api/v1/gate-core/sales-dispatch/pending-bookings/",
            {"dispatch_plan_ids": str(plan.id)},
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        group = response.data[0]
        self.assertEqual(group["driver"], gate_driver.id)
        self.assertEqual(group["driver_name"], "Gate Driver")
        self.assertEqual(group["driver_mobile_no"], "9777777777")
        self.assertEqual(group["driver_license_no"], "DL-GATE-001")
        self.assertEqual(group["driver_id_proof_type"], "Aadhaar")
        self.assertEqual(group["driver_id_proof_number"], "1234")

    def test_pending_bookings_excludes_already_docked_plans(self):
        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=81001,
            sap_invoice_doc_num="81001",
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            bilty_no="BLT-DOCKED",
            created_by=self.user,
            updated_by=self.user,
        )
        entry = self.create_sales_dispatch(
            "23",
            status_value=SalesDispatchGateOutStatus.DOCKED,
            dispatch_plan=plan,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry,
            company=self.company,
            dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=81001,
            sap_doc_num="81001",
        )

        response = self.client.get(
            "/api/v1/gate-core/sales-dispatch/pending-bookings/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_mark_dispatched_completes_vehicle_entry_and_dispatch_plan(self):
        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=99001,
            sap_invoice_doc_num="99001",
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            created_by=self.user,
            updated_by=self.user,
        )
        entry = self.create_sales_dispatch(
            "20",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
            dispatch_plan=plan,
            with_photo=True,
            with_item=True,
            with_weighment=True,
        )
        entry.gatepass_no = "DCK/JIVO_OIL/2026-27/000020"
        entry.print_committed_at = timezone.now()
        entry.save(update_fields=["gatepass_no", "print_committed_at"])

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/dispatch/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry.refresh_from_db()
        entry.vehicle_entry.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(entry.status, SalesDispatchGateOutStatus.DISPATCHED)
        self.assertEqual(entry.vehicle_entry.status, "COMPLETED")
        self.assertEqual(plan.booking_status, DispatchPlanStatus.DISPATCHED)

    def test_mark_dispatched_requires_committed_gatepass_audit(self):
        entry = self.create_sales_dispatch(
            "21",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
        )

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/dispatch/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Gatepass number", response.data["detail"])
        entry.refresh_from_db()
        self.assertEqual(entry.status, SalesDispatchGateOutStatus.PRINT_COMMITTED)

    def test_mark_dispatched_requires_gross_weighment(self):
        entry = self.create_sales_dispatch(
            "24",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
        )
        entry.gatepass_no = "DCK/JIVO_OIL/2026-27/000024"
        entry.print_committed_at = timezone.now()
        entry.save(update_fields=["gatepass_no", "print_committed_at"])
        Weighment.objects.create(
            vehicle_entry=entry.vehicle_entry,
            tare_weight=Decimal("250.000"),
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/dispatch/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Gross weight", response.data["detail"])
        entry.refresh_from_db()
        self.assertEqual(entry.status, SalesDispatchGateOutStatus.PRINT_COMMITTED)

    @patch("gate_core.views_sales_dispatch.SalesDispatchDocumentService.get_document")
    def test_create_sales_dispatch_keeps_single_document_payload_compatible(self, get_document):
        get_document.return_value = self.sap_document(626050342, doc_num="626050342")

        response = self.client.post(
            "/api/v1/gate-core/sales-dispatch/",
            {
                "document_type": SalesDispatchDocumentType.INVOICE,
                "sap_doc_entry": 626050342,
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["document_count"], 1)
        self.assertEqual(len(response.data["documents"]), 1)
        entry = SalesDispatchGateOut.objects.get(id=response.data["id"])
        document = entry.documents.get()
        self.assertEqual(document.sap_doc_entry, 626050342)
        self.assertEqual(entry.items.get().document_id, document.id)

    @patch("gate_core.views_sales_dispatch.SalesDispatchDocumentService.get_document")
    def test_create_sales_dispatch_copies_linked_empty_vehicle_tare(self, get_document):
        get_document.return_value = self.sap_document(626060173, doc_num="626060173")
        linked_vehicle_entry = VehicleEntry.objects.create(
            entry_no="EVGI-LINK-TARE",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=linked_vehicle_entry,
            tare_weight=Decimal("11.000"),
            weighbridge_slip_no="WB-EMPTY",
            created_by=self.user,
            updated_by=self.user,
        )
        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626060173,
            sap_invoice_doc_num="626060173",
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            linked_vehicle_entry=linked_vehicle_entry,
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.post(
            "/api/v1/gate-core/sales-dispatch/",
            {
                "documents": [
                    {
                        "document_type": SalesDispatchDocumentType.INVOICE,
                        "sap_doc_entry": 626060173,
                        "dispatch_plan_id": dispatch_plan.id,
                    }
                ],
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        entry = SalesDispatchGateOut.objects.get(id=response.data["id"])
        self.assertEqual(entry.vehicle_entry.weighment.tare_weight, Decimal("11.000"))
        self.assertEqual(entry.vehicle_entry.weighment.weighbridge_slip_no, "WB-EMPTY")

    @patch("gate_core.views_sales_dispatch.SalesDispatchDocumentService.get_document")
    def test_create_sales_dispatch_accepts_multi_invoice_documents(self, get_document):
        docs = {
            626050342: self.sap_document(
                626050342,
                doc_num="626050342",
                card_code="CUST-A",
                card_name="Customer A",
                eway_bill="EWB-A",
                doc_total=Decimal("100.00"),
            ),
            1808192112: self.sap_document(
                1808192112,
                doc_num="1808192112",
                card_code="CUST-B",
                card_name="Customer B",
                eway_bill="EWB-B",
                doc_total=Decimal("250.00"),
            ),
        }
        get_document.side_effect = lambda document_type, doc_entry: docs[doc_entry]

        response = self.client.post(
            "/api/v1/gate-core/sales-dispatch/",
            {
                "documents": [
                    {
                        "document_type": SalesDispatchDocumentType.INVOICE,
                        "sap_doc_entry": 626050342,
                    },
                    {
                        "document_type": SalesDispatchDocumentType.INVOICE,
                        "sap_doc_entry": 1808192112,
                    },
                ],
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["document_count"], 2)
        self.assertEqual(response.data["document_numbers"], ["626050342", "1808192112"])
        self.assertEqual(response.data["sap_doc_total"], "350.00")
        self.assertEqual(response.data["total_weight"], "160.000")
        self.assertCountEqual(
            [warning["code"] for warning in response.data["warnings"]],
            ["MULTIPLE_CUSTOMERS", "MULTIPLE_EWAY_BILLS"],
        )
        entry = SalesDispatchGateOut.objects.get(id=response.data["id"])
        self.assertEqual(entry.documents.count(), 2)
        self.assertEqual(entry.items.count(), 2)
        self.assertEqual(
            list(entry.items.order_by("line_num").values_list("line_num", flat=True)),
            [0, 1],
        )

    @patch("gate_core.views_sales_dispatch.SalesDispatchDocumentService.get_document")
    def test_create_sales_dispatch_blocks_multi_invoice_branch_mismatch(self, get_document):
        docs = {
            1: self.sap_document(1, branch_id=1),
            2: self.sap_document(2, branch_id=2),
        }
        get_document.side_effect = lambda document_type, doc_entry: docs[doc_entry]

        response = self.client.post(
            "/api/v1/gate-core/sales-dispatch/",
            {
                "documents": [
                    {
                        "document_type": SalesDispatchDocumentType.INVOICE,
                        "sap_doc_entry": 1,
                    },
                    {
                        "document_type": SalesDispatchDocumentType.INVOICE,
                        "sap_doc_entry": 2,
                    },
                ],
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("same SAP branch", response.data["detail"])
        self.assertEqual(SalesDispatchGateOut.objects.count(), 0)

    def test_mark_dispatched_updates_all_document_dispatch_plans(self):
        first_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=70001,
            sap_invoice_doc_num="70001",
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            created_by=self.user,
            updated_by=self.user,
        )
        second_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=70002,
            sap_invoice_doc_num="70002",
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            created_by=self.user,
            updated_by=self.user,
        )
        entry = self.create_sales_dispatch(
            "22",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
            dispatch_plan=first_plan,
            with_photo=True,
            with_item=True,
            with_weighment=True,
        )
        entry.gatepass_no = "DCK/JIVO_OIL/2026-27/000022"
        entry.print_committed_at = timezone.now()
        entry.save(update_fields=["gatepass_no", "print_committed_at"])
        first_document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry,
            company=self.company,
            dispatch_plan=first_plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=70001,
            sap_doc_num="70001",
        )
        second_document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry,
            company=self.company,
            dispatch_plan=second_plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=70002,
            sap_doc_num="70002",
        )
        entry.items.update(document=first_document)
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry,
            document=second_document,
            line_num=1,
            item_code="ITEM-2",
            item_name="Second Item",
            quantity=Decimal("1.000"),
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{entry.id}/dispatch/",
            {},
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        first_plan.refresh_from_db()
        second_plan.refresh_from_db()
        self.assertEqual(first_plan.booking_status, DispatchPlanStatus.DISPATCHED)
        self.assertEqual(second_plan.booking_status, DispatchPlanStatus.DISPATCHED)

    def test_bst_in_eligible_outs_use_dispatched_docking_stock_transfers(self):
        eligible_entry = self.create_dispatched_stock_transfer("80")
        self.create_sales_dispatch(
            "81",
            status_value=SalesDispatchGateOutStatus.DISPATCHED,
            document_type=SalesDispatchDocumentType.INVOICE,
        )
        self.create_sales_dispatch(
            "82",
            status_value=SalesDispatchGateOutStatus.PRINT_COMMITTED,
            document_type=SalesDispatchDocumentType.STOCK_TRANSFER,
        )

        response = self.client.get(
            "/api/v1/gate-core/bst-ins/eligible-outs/",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], eligible_entry.id)
        self.assertEqual(response.data[0]["source_type"], "DOCKING_STOCK_TRANSFER")
        self.assertEqual(response.data[0]["sap_doc_num"], "BST-80")
        self.assertEqual(response.data[0]["sap_from_warehouse"], "SRC-WH")
        self.assertEqual(response.data[0]["sap_to_warehouse"], "DST-WH")
        self.assertEqual(response.data[0]["items"][0]["actual_quantity"], "10.000")

    def test_bst_in_create_links_to_docking_stock_transfer_source(self):
        source_entry = self.create_dispatched_stock_transfer("83")
        source_item = source_entry.items.first()

        response = self.client.post(
            "/api/v1/gate-core/bst-ins/",
            {
                "sales_dispatch_gate_out_id": source_entry.id,
                "gate_in_date": str(timezone.localdate()),
                "in_time": "10:30",
                "sap_receipt_doc_num": "GR-BST-83",
                "items": [
                    {
                        "line_num": source_item.line_num,
                        "receiving_quantity": "8.000",
                    }
                ],
            },
            format="json",
            **self.company_header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["source_type"], "DOCKING_STOCK_TRANSFER")
        self.assertIsNone(response.data["bst_gate_out"])
        self.assertEqual(response.data["sales_dispatch_gate_out"], source_entry.id)

        bst_in = BSTGateIn.objects.get(id=response.data["id"])
        self.assertIsNone(bst_in.bst_gate_out_id)
        self.assertEqual(bst_in.sales_dispatch_gate_out_id, source_entry.id)
        self.assertEqual(bst_in.items.count(), 1)
        bst_in_item = bst_in.items.first()
        self.assertIsNone(bst_in_item.bst_gate_out_item_id)
        self.assertEqual(bst_in_item.sales_dispatch_gate_out_item_id, source_item.id)
        self.assertEqual(bst_in_item.receiving_quantity, Decimal("8.000"))

        eligible_response = self.client.get(
            "/api/v1/gate-core/bst-ins/eligible-outs/",
            **self.company_header,
        )
        self.assertEqual(eligible_response.status_code, status.HTTP_200_OK)
        self.assertEqual(eligible_response.data, [])
