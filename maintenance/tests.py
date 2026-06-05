import shutil
import tempfile
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Department
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from gate_core.models import UnitChoice
from maintenance_gatein.models import MaintenanceType
from notifications.models import Notification
from person_gatein.models import EntryLog, Gate, PersonType, Visitor
from production_execution.models import (
    BreakdownCategory,
    Machine,
    MachineBreakdown,
    ProductionLine,
    ProductionRun,
    ProductionSegment,
    RunStatus,
)

from .models import (
    Asset,
    AssetDocument,
    AssetPhoto,
    MaintenanceGateLink,
    MaintenanceChecklistResult,
    MaintenanceChecklistTemplateItem,
    MaintenanceSpare,
    MaintenanceSpareReceipt,
    MaintenanceVendorVisit,
    MaintenanceWorkOrder,
    MaintenanceWorkOrderPhoto,
    PreventiveMaintenanceExecution,
    PreventiveMaintenancePlan,
    SpareMovement,
    SpareRequest,
)
from vehicle_management.models import Vehicle, VehicleType

TEST_MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class MaintenanceAssetAPITests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Maintenance Head")
        self.user = get_user_model().objects.create_user(
            email="maintenance@example.com",
            password="testpass123",
            full_name="Maintenance User",
            employee_code="MNT001",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )
        self.technician = get_user_model().objects.create_user(
            email="technician@example.com",
            password="testpass123",
            full_name="Maintenance Technician",
            employee_code="MNT002",
        )
        UserCompany.objects.create(
            user=self.technician,
            company=self.company,
            role=role,
            is_default=False,
            is_active=True,
        )
        self.user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=[
                    "gate_core",
                    "maintenance",
                    "maintenance_gatein",
                    "production_execution",
                ]
            )
        )
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def create_master_data(self):
        category = self.client.post(
            "/api/v1/maintenance/asset-categories/",
            {"name": "Filling Machine", "description": "Filler assets"},
            format="json",
        )
        self.assertEqual(category.status_code, status.HTTP_201_CREATED, category.data)

        location = self.client.post(
            "/api/v1/maintenance/asset-locations/",
            {"name": "Plant 1", "area": "Packing", "line": "Line 1"},
            format="json",
        )
        self.assertEqual(location.status_code, status.HTTP_201_CREATED, location.data)

        department = self.client.post(
            "/api/v1/maintenance/asset-departments/",
            {"name": "Production", "department_code": "PROD"},
            format="json",
        )
        self.assertEqual(department.status_code, status.HTTP_201_CREATED, department.data)

        return category.data, location.data, department.data

    def create_asset(self, **overrides):
        category, location, department = self.create_master_data()
        payload = {
            "asset_code": "MCH-001",
            "name": "Filler 1",
            "category": category["id"],
            "location": location["id"],
            "department": department["id"],
            "hierarchy_level": "MACHINE",
            "area": "Packing",
            "line": "Line 1",
            "status": "RUNNING",
            "make": "Acme",
            "model": "F100",
            "serial_number": "SN-001",
            "qr_code": "QR-MCH-001",
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/maintenance/assets/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response

    def create_work_order(self, asset_response, **overrides):
        payload = {
            "work_type": "BREAKDOWN",
            "priority": "HIGH",
            "asset": asset_response.data["id"],
            "department": asset_response.data["department"],
            "title": "Filler repair",
            "problem_statement": "Filler needs urgent repair.",
            "impact": "DEGRADED",
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/maintenance/work-orders/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response

    def create_spare(self, asset_id, **overrides):
        category_response = self.client.post(
            "/api/v1/maintenance/spare-categories/",
            {"name": f"Gate Spares {asset_id}", "description": "Gate receipt spares"},
            format="json",
        )
        self.assertEqual(category_response.status_code, status.HTTP_201_CREATED, category_response.data)
        payload = {
            "category": category_response.data["id"],
            "name": "Critical proximity sensor",
            "part_number": "SEN-001",
            "sap_item_code": "SAP-SEN-001",
            "uom": "NOS",
            "compatible_assets": [asset_id],
            "is_critical": True,
            "minimum_stock": "1.000",
            "reorder_level": "2.000",
            "current_stock": "0.000",
            "unit_cost": "250.00",
            "storage_location": "MNT-A1",
        }
        payload.update(overrides)
        response = self.client.post("/api/v1/maintenance/spares/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response

    def create_vehicle_entry(self, entry_no="MNT-GATE-001"):
        vehicle_type = VehicleType.objects.create(name=f"TRUCK-{entry_no}")
        vehicle = Vehicle.objects.create(
            vehicle_number=f"HR55{entry_no[-3:]}",
            vehicle_type=vehicle_type,
        )
        driver = Driver.objects.create(
            name=f"Driver {entry_no}",
            mobile_no="9999999999",
            license_no=f"DL-{entry_no}",
        )
        return VehicleEntry.objects.create(
            entry_no=entry_no,
            company=self.company,
            vehicle=vehicle,
            driver=driver,
            entry_type="MAINTENANCE",
            status=GateEntryStatus.DRAFT,
            created_by=self.user,
        )

    def create_maintenance_gate_entry(self, asset_response, work_order_response, spare_response):
        maintenance_type = MaintenanceType.objects.create(type_name="Mechanical")
        unit = UnitChoice.objects.create(name="NOS")
        department = Department.objects.create(name="Maintenance Store")
        vehicle_entry = self.create_vehicle_entry()
        response = self.client.post(
            f"/api/v1/maintenance-gatein/gate-entries/{vehicle_entry.id}/maintenance/",
            {
                "maintenance_type": maintenance_type.id,
                "maintenance_work_order": work_order_response.data["id"],
                "supplier_name": "ABC Engineering",
                "material_description": "Critical proximity sensor for filler",
                "part_number": str(spare_response.data["part_number"]).lower(),
                "quantity": "2.00",
                "unit": unit.id,
                "invoice_number": "INV-MNT-001",
                "equipment_id": asset_response.data["asset_code"].lower(),
                "receiving_department": department.id,
                "urgency_level": "CRITICAL",
                "remarks": "Gate receipt for maintenance repair",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return vehicle_entry, response

    def create_production_breakdown_context(self):
        line = ProductionLine.objects.create(company=self.company, name="Line 1")
        machine = Machine.objects.create(
            company=self.company,
            name="Filler 1",
            machine_type="FILLER",
            line=line,
        )
        category = BreakdownCategory.objects.create(company=self.company, name="Machine")
        run = ProductionRun.objects.create(
            company=self.company,
            run_number=1,
            date=timezone.localdate(),
            line=line,
            product="Sunflower Oil 1L",
            warehouse_approval_status="APPROVED",
            status=RunStatus.IN_PROGRESS,
            created_by=self.user,
        )
        run.machines.add(machine)
        ProductionSegment.objects.create(
            production_run=run,
            start_time=timezone.now() - timedelta(minutes=10),
            is_active=True,
        )
        asset_response = self.create_asset(
            asset_code="MCH-FILLER-001",
            name="Filler 1 Asset",
            line=line.name,
            production_machine=machine.id,
        )
        return {
            "line": line,
            "machine": machine,
            "category": category,
            "run": run,
            "asset_id": asset_response.data["id"],
        }

    def test_asset_crud_dashboard_and_options(self):
        asset_response = self.create_asset()
        asset_id = asset_response.data["id"]

        detail = self.client.get(f"/api/v1/maintenance/assets/{asset_id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        self.assertEqual(detail.data["asset_code"], "MCH-001")
        self.assertEqual(detail.data["category_name"], "Filling Machine")

        filtered = self.client.get(
            "/api/v1/maintenance/assets/",
            {"status": "RUNNING", "department": detail.data["department"], "line": "Line 1"},
        )
        self.assertEqual(filtered.status_code, status.HTTP_200_OK, filtered.data)
        self.assertEqual(len(filtered.data), 1)

        dashboard = self.client.get("/api/v1/maintenance/dashboard/")
        self.assertEqual(dashboard.status_code, status.HTTP_200_OK, dashboard.data)
        self.assertEqual(dashboard.data["assets"]["active"], 1)
        self.assertEqual(dashboard.data["assets"]["by_status"]["RUNNING"], 1)

        options = self.client.get("/api/v1/maintenance/options/")
        self.assertEqual(options.status_code, status.HTTP_200_OK, options.data)
        self.assertIn({"value": "RUNNING", "label": "Running"}, options.data["statuses"])
        self.assertIn({"value": "BREAKDOWN", "label": "Breakdown"}, options.data["work_types"])
        self.assertIn({"value": "IN_PROGRESS", "label": "In Progress"}, options.data["work_statuses"])
        self.assertEqual(options.data["categories"][0]["name"], "Filling Machine")
        self.assertEqual(len(options.data["users"]), 2)

    def test_phase4_pm_plan_generates_execution_and_completes_checklist(self):
        asset_response = self.create_asset(
            asset_code="PM-MCH-001",
            name="PM Filler",
            qr_code="QR-PM-MCH-001",
        )
        today = timezone.localdate()

        plan_response = self.client.post(
            "/api/v1/maintenance/pm-plans/",
            {
                "title": "Daily filler PM",
                "asset": asset_response.data["id"],
                "frequency": "DAILY",
                "work_type": "PREVENTIVE",
                "priority": "NORMAL",
                "assigned_to": self.technician.id,
                "start_date": today.isoformat(),
                "next_due_date": today.isoformat(),
                "advance_days": 0,
                "auto_create_work_order": True,
                "checklist_required": True,
                "description": "Daily lubrication and safety check.",
            },
            format="json",
        )
        self.assertEqual(plan_response.status_code, status.HTTP_201_CREATED, plan_response.data)
        plan = PreventiveMaintenancePlan.objects.get(pk=plan_response.data["id"])
        self.assertTrue(plan.plan_code.startswith("PM-"))

        item_response = self.client.post(
            "/api/v1/maintenance/pm-checklist-items/",
            {
                "pm_plan": plan.id,
                "task": "Check guards and oil leakage",
                "input_type": "PASS_FAIL",
                "is_required": True,
                "safety_critical": True,
                "sort_order": 1,
            },
            format="json",
        )
        self.assertEqual(item_response.status_code, status.HTTP_201_CREATED, item_response.data)
        checklist_item = MaintenanceChecklistTemplateItem.objects.get(pk=item_response.data["id"])

        generate_response = self.client.post(
            "/api/v1/maintenance/pm-plans/generate-due/",
            {"due_until": today.isoformat()},
            format="json",
        )
        self.assertEqual(generate_response.status_code, status.HTTP_201_CREATED, generate_response.data)
        self.assertEqual(generate_response.data["generated_count"], 1)

        execution = PreventiveMaintenanceExecution.objects.select_related("work_order", "asset").get(
            pm_plan=plan,
            due_date=today,
        )
        self.assertEqual(execution.status, "PENDING")
        self.assertIsNotNone(execution.work_order)
        self.assertEqual(execution.work_order.work_type, "PREVENTIVE")
        self.assertEqual(execution.work_order.status, "ASSIGNED")
        self.assertEqual(MaintenanceChecklistResult.objects.filter(execution=execution).count(), 1)

        start_response = self.client.post(f"/api/v1/maintenance/pm-executions/{execution.id}/start/")
        self.assertEqual(start_response.status_code, status.HTTP_200_OK, start_response.data)
        execution.refresh_from_db()
        self.assertEqual(execution.status, "IN_PROGRESS")
        execution.asset.refresh_from_db()
        self.assertEqual(execution.asset.status, "UNDER_PM")

        complete_response = self.client.post(
            f"/api/v1/maintenance/pm-executions/{execution.id}/complete/",
            {
                "remarks": "Daily PM completed.",
                "checklist_results": [
                    {
                        "template_item": checklist_item.id,
                        "value_text": "Pass",
                        "is_ok": True,
                        "remarks": "No leakage found.",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK, complete_response.data)
        execution.refresh_from_db()
        execution.work_order.refresh_from_db()
        execution.asset.refresh_from_db()
        self.assertEqual(execution.status, "COMPLETED")
        self.assertEqual(execution.work_order.status, "COMPLETED")
        self.assertEqual(execution.asset.status, "RUNNING")
        result = MaintenanceChecklistResult.objects.get(execution=execution, template_item=checklist_item)
        self.assertTrue(result.is_ok)
        self.assertEqual(result.value_text, "Pass")

        options = self.client.get("/api/v1/maintenance/options/")
        self.assertEqual(options.status_code, status.HTTP_200_OK, options.data)
        self.assertIn({"value": "DAILY", "label": "Daily"}, options.data["pm_frequencies"])
        self.assertIn({"value": "PASS_FAIL", "label": "Pass / Fail"}, options.data["checklist_input_types"])

    def test_phase8_dashboard_reports_filtered_work_pressure(self):
        context = self.create_production_breakdown_context()
        asset = Asset.objects.get(id=context["asset_id"])
        today = timezone.localdate()
        asset.status = "BREAKDOWN"
        asset.amc_vendor = "ABC Engineering"
        asset.amc_end_date = today + timedelta(days=15)
        asset.save(update_fields=["status", "amc_vendor", "amc_end_date", "updated_at"])

        breakdown = MachineBreakdown.objects.create(
            production_run=context["run"],
            machine=context["machine"],
            start_time=timezone.now() - timedelta(minutes=45),
            breakdown_minutes=45,
            breakdown_category=context["category"],
            is_active=True,
            reason="Filler chain jam",
        )
        critical_work_order = MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-DASH-001",
            work_type="BREAKDOWN",
            status="IN_PROGRESS",
            priority="CRITICAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Filler chain jam",
            problem_statement="Line stopped due to chain jam.",
            impact="STOPPAGE",
            production_run=context["run"],
            production_breakdown=breakdown,
            target_date=today,
            reported_by=self.user,
            assigned_to=self.technician,
        )
        MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-DASH-002",
            work_type="PREVENTIVE",
            status="OPEN",
            priority="NORMAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Weekly lubrication PM",
            problem_statement="Scheduled PM task.",
            impact="NO_IMPACT",
            target_date=today - timedelta(days=1),
            reported_by=self.user,
        )
        MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-DASH-003",
            work_type="PREVENTIVE",
            status="CLOSED",
            priority="NORMAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Completed PM",
            problem_statement="Completed PM task.",
            impact="NO_IMPACT",
            target_date=today - timedelta(days=2),
            reported_by=self.user,
        )
        spare_response = self.create_spare(asset.id)
        MaintenanceVendorVisit.objects.create(
            company=self.company,
            work_order=critical_work_order,
            asset=asset,
            vendor_code="VEND-ABC",
            vendor_name="ABC Engineering",
            status="PLANNED",
            planned_start=timezone.now(),
            planned_end=timezone.now() + timedelta(hours=2),
            created_by=self.user,
            updated_by=self.user,
        )

        dashboard = self.client.get("/api/v1/maintenance/dashboard/")
        self.assertEqual(dashboard.status_code, status.HTTP_200_OK, dashboard.data)
        self.assertEqual(dashboard.data["breakdowns"]["open"], 1)
        self.assertEqual(dashboard.data["breakdowns"]["critical"], 1)
        self.assertEqual(dashboard.data["today_tasks"]["total"], 1)
        self.assertEqual(dashboard.data["today_tasks"]["items"][0]["id"], critical_work_order.id)
        self.assertEqual(dashboard.data["pm"]["overdue"], 1)
        self.assertEqual(dashboard.data["pm"]["due_total"], 2)
        self.assertEqual(dashboard.data["pm"]["completed_due"], 1)
        self.assertEqual(dashboard.data["pm"]["compliance_percent"], 50.0)
        self.assertEqual(dashboard.data["production_downtime"]["total_minutes"], 45)
        self.assertEqual(dashboard.data["production_downtime"]["active_breakdowns"], 1)
        self.assertEqual(dashboard.data["production_downtime"]["impacted_runs"], 1)
        self.assertEqual(dashboard.data["spare_risk"]["critical_shortage"], 1)
        self.assertEqual(dashboard.data["spare_risk"]["items"][0]["id"], spare_response.data["id"])
        self.assertEqual(dashboard.data["vendor_amc"]["due_visits"], 1)
        self.assertEqual(dashboard.data["vendor_amc"]["amc_due"], 1)

        filtered = self.client.get(
            "/api/v1/maintenance/dashboard/",
            {
                "department": asset.department_id,
                "line": asset.line,
                "priority": "CRITICAL",
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
            },
        )
        self.assertEqual(filtered.status_code, status.HTTP_200_OK, filtered.data)
        self.assertEqual(filtered.data["filters"]["department"], asset.department_id)
        self.assertEqual(filtered.data["filters"]["line"], asset.line)
        self.assertEqual(filtered.data["filters"]["priority"], "CRITICAL")
        self.assertEqual(filtered.data["work_orders"]["total"], 1)
        self.assertEqual(filtered.data["open_breakdowns"][0]["id"], critical_work_order.id)
        self.assertEqual(filtered.data["pm"]["overdue"], 0)
        self.assertEqual(filtered.data["production_downtime"]["total_minutes"], 45)

    def test_phase9_reports_module_returns_report_rows_and_exports(self):
        asset_response = self.create_asset()
        asset = Asset.objects.get(pk=asset_response.data["id"])
        today = timezone.localdate()
        start_time = timezone.now() - timedelta(hours=2)
        end_time = timezone.now() - timedelta(minutes=30)

        breakdown = MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-RPT-001",
            work_type="BREAKDOWN",
            status="CLOSED",
            priority="CRITICAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Filler belt snapped",
            problem_statement="Line stopped due to snapped belt.",
            impact="STOPPAGE",
            downtime_reason="Belt failure",
            root_cause="Belt worn out",
            corrective_action="Replaced belt",
            target_date=today,
            start_time=start_time,
            end_time=end_time,
            completed_at=end_time,
            closed_at=end_time,
            reported_by=self.user,
            assigned_to=self.technician,
            created_by=self.user,
            updated_by=self.user,
        )
        MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-RPT-002",
            work_type="PREVENTIVE",
            status="CLOSED",
            priority="NORMAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Daily PM checklist",
            problem_statement="Scheduled PM.",
            impact="NO_IMPACT",
            target_date=today,
            completed_at=end_time,
            closed_at=end_time,
            reported_by=self.user,
            created_by=self.user,
            updated_by=self.user,
        )
        spare_response = self.create_spare(asset.id, current_stock="1.000", unit_cost="125.00")
        spare = MaintenanceSpare.objects.get(pk=spare_response.data["id"])
        spare_request = SpareRequest.objects.create(
            company=self.company,
            work_order=breakdown,
            spare=spare,
            status="CLOSED",
            requested_qty=Decimal("1.000"),
            issued_qty=Decimal("1.000"),
            consumed_qty=Decimal("1.000"),
            requested_by=self.user,
            created_by=self.user,
            updated_by=self.user,
        )
        SpareMovement.objects.create(
            company=self.company,
            spare_request=spare_request,
            work_order=breakdown,
            spare=spare,
            movement_type="CONSUME",
            quantity=Decimal("1.000"),
            unit_cost=Decimal("125.00"),
            remarks="Consumed for belt replacement",
            performed_by=self.user,
            created_by=self.user,
            updated_by=self.user,
        )
        MaintenanceVendorVisit.objects.create(
            company=self.company,
            work_order=breakdown,
            asset=asset,
            vendor_code="VEND-MECH",
            vendor_name="Mechanical Services",
            status="COMPLETED",
            planned_start=start_time,
            actual_start=start_time,
            actual_end=end_time,
            invoice_number="INV-SVC-001",
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.get(
            "/api/v1/maintenance/reports/",
            {
                "report_type": "breakdown",
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "priority": "CRITICAL",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["report_type"], "breakdown")
        self.assertEqual(response.data["summary"]["total_work_orders"], 1)
        self.assertEqual(response.data["summary"]["breakdowns"], 1)
        self.assertEqual(response.data["summary"]["spare_consumed_cost"], "125.00")
        self.assertEqual(response.data["rows"][0]["work_order_no"], "MWO-RPT-001")
        self.assertEqual(response.data["rows"][0]["spare_consumed_cost"], "125.00")

        spare_report = self.client.get(
            "/api/v1/maintenance/reports/",
            {
                "report_type": "spare_consumption",
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
            },
        )
        self.assertEqual(spare_report.status_code, status.HTTP_200_OK, spare_report.data)
        self.assertEqual(spare_report.data["rows"][0]["spare_part_number"], "SEN-001")
        self.assertEqual(spare_report.data["rows"][0]["line_total"], "125.00")

        excel_export = self.client.get(
            "/api/v1/maintenance/reports/",
            {
                "report_type": "vendor_visit",
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "export": "excel",
            },
        )
        self.assertEqual(excel_export.status_code, status.HTTP_200_OK)
        self.assertIn("attachment;", excel_export["Content-Disposition"])
        self.assertIn(b"Mechanical Services", excel_export.content)

        pdf_export = self.client.get(
            "/api/v1/maintenance/reports/",
            {
                "report_type": "critical_spare",
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "export": "pdf",
            },
        )
        self.assertEqual(pdf_export.status_code, status.HTTP_200_OK)
        self.assertEqual(pdf_export["Content-Type"], "application/pdf")
        self.assertTrue(pdf_export.content.startswith(b"%PDF-1.4"))

        invalid = self.client.get("/api/v1/maintenance/reports/", {"report_type": "unknown"})
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_phase10_scan_stock_and_alert_automation(self):
        asset_response = self.create_asset()
        asset = Asset.objects.get(pk=asset_response.data["id"])
        today = timezone.localdate()

        qr_response = self.client.post(
            f"/api/v1/maintenance/assets/{asset.id}/qr/",
            {"qr_code": "QR-FILLER-001"},
            format="json",
        )
        self.assertEqual(qr_response.status_code, status.HTTP_200_OK, qr_response.data)
        self.assertEqual(qr_response.data["qr_code"], "QR-FILLER-001")
        self.assertEqual(qr_response.data["asset_url"], f"/maintenance/assets/{asset.id}")

        lookup = self.client.get("/api/v1/maintenance/scan/lookup/", {"code": "QR-FILLER-001"})
        self.assertEqual(lookup.status_code, status.HTTP_200_OK, lookup.data)
        self.assertTrue(lookup.data["found"])
        self.assertEqual(lookup.data["type"], "asset")
        self.assertEqual(lookup.data["asset"]["id"], asset.id)
        self.assertTrue(lookup.data["actions"]["create_work_order"])

        created = self.client.post(
            "/api/v1/maintenance/scan/work-order/",
            {
                "code": "QR-FILLER-001",
                "title": "Mobile scan complaint",
                "problem_statement": "Abnormal vibration noticed during mobile inspection.",
                "priority": "HIGH",
                "impact": "DEGRADED",
                "target_date": today.isoformat(),
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data["asset"], asset.id)
        self.assertEqual(created.data["work_type"], "COMPLAINT")
        asset.refresh_from_db()
        self.assertEqual(asset.status, "UNDER_REPAIR")

        spare_response = self.create_spare(asset.id, current_stock="0.000", reorder_level="2.000")
        spare = MaintenanceSpare.objects.get(pk=spare_response.data["id"])
        spare_lookup = self.client.get(
            "/api/v1/maintenance/scan/lookup/",
            {"code": spare.sap_item_code},
        )
        self.assertEqual(spare_lookup.status_code, status.HTTP_200_OK, spare_lookup.data)
        self.assertEqual(spare_lookup.data["type"], "spare")
        self.assertEqual(spare_lookup.data["barcode"], spare.sap_item_code)

        with patch(
            "maintenance.views._fetch_sap_spare_stock",
            return_value={
                "available": True,
                "source": "sap",
                "message": "",
                "rows": [
                    {
                        "item_code": spare.sap_item_code,
                        "item_name": spare.name,
                        "uom": spare.uom,
                        "warehouse": "MNT",
                        "warehouse_name": "Maintenance Store",
                        "on_hand": "8.000",
                        "committed": "1.000",
                        "on_order": "2.000",
                        "available_qty": "7.000",
                    }
                ],
            },
        ) as stock_reader:
            stock = self.client.get(
                "/api/v1/maintenance/spares/stock/",
                {"spare": spare.id, "warehouse": "MNT"},
            )
        self.assertEqual(stock.status_code, status.HTTP_200_OK, stock.data)
        stock_reader.assert_called_once_with(self.company.code, spare.sap_item_code, "MNT")
        self.assertEqual(stock.data["local"]["current_stock"], "0.000")
        self.assertEqual(stock.data["sap"]["source"], "sap")
        self.assertEqual(stock.data["sap"]["total_available_qty"], "7.000")

        MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-P10-PM",
            work_type="PREVENTIVE",
            status="OPEN",
            priority="NORMAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="PM due alert",
            problem_statement="PM due",
            impact="NO_IMPACT",
            target_date=today,
            created_by=self.user,
            updated_by=self.user,
        )
        MaintenanceWorkOrder.objects.create(
            company=self.company,
            work_order_no="MWO-P10-BRK",
            work_type="BREAKDOWN",
            status="OPEN",
            priority="CRITICAL",
            asset=asset,
            department=asset.department,
            line=asset.line,
            title="Critical breakdown alert",
            problem_statement="Critical breakdown",
            impact="STOPPAGE",
            target_date=today,
            created_by=self.user,
            updated_by=self.user,
        )
        asset.amc_end_date = today + timedelta(days=10)
        asset.save(update_fields=["amc_end_date", "updated_at"])

        alerts = self.client.get("/api/v1/maintenance/alerts/")
        self.assertEqual(alerts.status_code, status.HTTP_200_OK, alerts.data)
        self.assertGreaterEqual(alerts.data["counts"]["PM_DUE"], 1)
        self.assertGreaterEqual(alerts.data["counts"]["BREAKDOWN_ESCALATION"], 1)
        self.assertGreaterEqual(alerts.data["counts"]["LOW_CRITICAL_SPARE"], 1)
        self.assertGreaterEqual(alerts.data["counts"]["AMC_WARRANTY_EXPIRY"], 1)

        notification_response = self.client.post(
            "/api/v1/maintenance/alerts/",
            {"alert_types": ["LOW_CRITICAL_SPARE"], "limit": 1},
            format="json",
        )
        self.assertEqual(
            notification_response.status_code,
            status.HTTP_201_CREATED,
            notification_response.data,
        )
        self.assertEqual(notification_response.data["notifications_sent"], 1)
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 1)

    def test_asset_code_is_unique_per_company(self):
        first = self.create_asset()
        duplicate = self.client.post(
            "/api/v1/maintenance/assets/",
            {
                "asset_code": first.data["asset_code"].lower(),
                "name": "Duplicate",
                "category": first.data["category"],
                "location": first.data["location"],
                "department": first.data["department"],
                "status": "IDLE",
            },
            format="json",
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("asset_code", duplicate.data)

    def test_asset_can_be_deactivated(self):
        asset_response = self.create_asset(status="UNDER_REPAIR")
        response = self.client.post(
            f"/api/v1/maintenance/assets/{asset_response.data['id']}/deactivate/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(response.data["status"], "RETIRED")

        asset = Asset.objects.get(pk=asset_response.data["id"])
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, "RETIRED")
        self.assertIsNotNone(asset.deactivated_at)

    def test_asset_photo_and_document_uploads(self):
        asset_response = self.create_asset()
        asset_id = asset_response.data["id"]

        photo = SimpleUploadedFile("front-view.jpg", b"photo-bytes", content_type="image/jpeg")
        photo_response = self.client.post(
            "/api/v1/maintenance/asset-photos/",
            {
                "asset": asset_id,
                "photo": photo,
                "caption": "Front view",
                "taken_on": "2026-06-02",
                "is_monthly_photo": "true",
            },
            format="multipart",
        )
        self.assertEqual(photo_response.status_code, status.HTTP_201_CREATED, photo_response.data)
        self.assertEqual(photo_response.data["asset"], asset_id)
        self.assertEqual(photo_response.data["caption"], "Front view")

        document = SimpleUploadedFile("manual.pdf", b"manual-bytes", content_type="application/pdf")
        document_response = self.client.post(
            "/api/v1/maintenance/asset-documents/",
            {
                "asset": asset_id,
                "document_type": "MANUAL",
                "title": "Pump Manual",
                "document": document,
                "document_date": "2026-06-02",
                "notes": "OEM manual",
            },
            format="multipart",
        )
        self.assertEqual(
            document_response.status_code,
            status.HTTP_201_CREATED,
            document_response.data,
        )
        self.assertEqual(document_response.data["asset"], asset_id)
        self.assertEqual(document_response.data["document_type"], "MANUAL")

        photos = self.client.get("/api/v1/maintenance/asset-photos/", {"asset": asset_id})
        documents = self.client.get("/api/v1/maintenance/asset-documents/", {"asset": asset_id})
        detail = self.client.get(f"/api/v1/maintenance/assets/{asset_id}/")

        self.assertEqual(photos.status_code, status.HTTP_200_OK, photos.data)
        self.assertEqual(documents.status_code, status.HTTP_200_OK, documents.data)
        self.assertEqual(len(photos.data), 1)
        self.assertEqual(len(documents.data), 1)
        self.assertEqual(detail.data["photos_count"], 1)
        self.assertEqual(detail.data["documents_count"], 1)
        self.assertEqual(AssetPhoto.objects.filter(asset_id=asset_id).count(), 1)
        self.assertEqual(AssetDocument.objects.filter(asset_id=asset_id).count(), 1)

    def test_work_order_lifecycle_and_asset_history(self):
        asset_response = self.create_asset()
        asset_id = asset_response.data["id"]
        department_id = asset_response.data["department"]

        create_response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "BREAKDOWN",
                "priority": "CRITICAL",
                "asset": asset_id,
                "department": department_id,
                "title": "Filler stopped during shift",
                "problem_statement": "Main filler is not rotating.",
                "impact": "STOPPAGE",
                "impact_notes": "Line 1 stopped.",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        self.assertTrue(create_response.data["work_order_no"].startswith("MWO-"))
        self.assertEqual(create_response.data["status"], "OPEN")
        self.assertEqual(create_response.data["reported_by"], self.user.id)

        asset = Asset.objects.get(pk=asset_id)
        self.assertEqual(asset.status, "BREAKDOWN")

        work_order_id = create_response.data["id"]
        assign_response = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/assign/",
            {"assigned_to": self.technician.id, "target_date": "2026-06-04"},
            format="json",
        )
        self.assertEqual(assign_response.status_code, status.HTTP_200_OK, assign_response.data)
        self.assertEqual(assign_response.data["status"], "ASSIGNED")
        self.assertEqual(assign_response.data["assigned_to"], self.technician.id)

        start_response = self.client.post(f"/api/v1/maintenance/work-orders/{work_order_id}/start/")
        self.assertEqual(start_response.status_code, status.HTTP_200_OK, start_response.data)
        self.assertEqual(start_response.data["status"], "IN_PROGRESS")
        self.assertIsNotNone(start_response.data["start_time"])
        asset.refresh_from_db()
        self.assertEqual(asset.status, "UNDER_REPAIR")

        complete_response = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/complete/",
            {
                "technician_remarks": "Motor coupling checked.",
                "completion_remarks": "Coupling tightened and trial completed.",
                "root_cause": "Loose coupling",
                "corrective_action": "Tightened coupling",
                "preventive_action": "Add coupling check to PM",
                "downtime_reason": "Mechanical coupling loose",
            },
            format="json",
        )
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK, complete_response.data)
        self.assertEqual(complete_response.data["status"], "COMPLETED")
        self.assertIsNotNone(complete_response.data["end_time"])

        approve_response = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/approve/",
            {"closure_remarks": "Verified by maintenance head."},
            format="json",
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK, approve_response.data)
        self.assertEqual(approve_response.data["status"], "APPROVED")
        self.assertEqual(approve_response.data["approved_by"], self.user.id)

        close_response = self.client.post(f"/api/v1/maintenance/work-orders/{work_order_id}/close/")
        self.assertEqual(close_response.status_code, status.HTTP_200_OK, close_response.data)
        self.assertEqual(close_response.data["status"], "CLOSED")
        self.assertEqual(close_response.data["closed_by"], self.user.id)

        asset.refresh_from_db()
        self.assertEqual(asset.status, "RUNNING")

        history = self.client.get("/api/v1/maintenance/work-orders/", {"asset": asset_id})
        self.assertEqual(history.status_code, status.HTTP_200_OK, history.data)
        self.assertEqual(len(history.data), 1)
        self.assertEqual(history.data[0]["id"], work_order_id)

        dashboard = self.client.get("/api/v1/maintenance/dashboard/")
        self.assertEqual(dashboard.status_code, status.HTTP_200_OK, dashboard.data)
        self.assertEqual(dashboard.data["work_orders"]["total"], 1)
        self.assertEqual(dashboard.data["work_orders"]["by_status"]["CLOSED"], 1)
        self.assertEqual(dashboard.data["recent_work_orders"][0]["id"], work_order_id)
        self.assertEqual(MaintenanceWorkOrder.objects.filter(company=self.company).count(), 1)

    def test_work_order_before_after_photo_uploads(self):
        asset_response = self.create_asset()
        create_response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "GENERAL",
                "priority": "HIGH",
                "asset": asset_response.data["id"],
                "department": asset_response.data["department"],
                "title": "Panel cleaning",
                "problem_statement": "Dust buildup in panel.",
                "impact": "DEGRADED",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        work_order_id = create_response.data["id"]

        before_photo = SimpleUploadedFile("before.jpg", b"before-bytes", content_type="image/jpeg")
        photo_response = self.client.post(
            "/api/v1/maintenance/work-order-photos/",
            {
                "work_order": work_order_id,
                "photo_type": "BEFORE",
                "photo": before_photo,
                "caption": "Before panel cleaning",
            },
            format="multipart",
        )
        self.assertEqual(photo_response.status_code, status.HTTP_201_CREATED, photo_response.data)
        self.assertEqual(photo_response.data["work_order"], work_order_id)
        self.assertEqual(photo_response.data["photo_type"], "BEFORE")

        photos = self.client.get(
            "/api/v1/maintenance/work-order-photos/",
            {"work_order": work_order_id},
        )
        detail = self.client.get(f"/api/v1/maintenance/work-orders/{work_order_id}/")

        self.assertEqual(photos.status_code, status.HTTP_200_OK, photos.data)
        self.assertEqual(len(photos.data), 1)
        self.assertEqual(detail.data["photos_count"], 1)
        self.assertEqual(MaintenanceWorkOrderPhoto.objects.filter(work_order_id=work_order_id).count(), 1)

    def test_production_breakdown_creates_maintenance_work_order(self):
        context = self.create_production_breakdown_context()

        response = self.client.post(
            f"/api/v1/production-execution/runs/{context['run'].id}/add-breakdown/",
            {
                "breakdown_category_id": context["category"].id,
                "machine_id": context["machine"].id,
                "maintenance_asset_id": context["asset_id"],
                "create_maintenance_work_order": True,
                "maintenance_priority": "CRITICAL",
                "reason": "Filler motor tripped",
                "produced_cases": "42",
                "remarks": "Raised from production",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNotNone(response.data["maintenance_work_order_id"])
        self.assertTrue(response.data["maintenance_work_order_no"].startswith("MWO-"))

        work_order = MaintenanceWorkOrder.objects.get(
            production_breakdown_id=response.data["id"]
        )
        self.assertEqual(work_order.production_run_id, context["run"].id)
        self.assertEqual(work_order.asset_id, context["asset_id"])
        self.assertEqual(work_order.work_type, "BREAKDOWN")
        self.assertEqual(work_order.status, "OPEN")
        self.assertEqual(work_order.priority, "CRITICAL")

        asset = Asset.objects.get(pk=context["asset_id"])
        self.assertEqual(asset.status, "BREAKDOWN")

        work_queue = self.client.get(
            "/api/v1/maintenance/work-orders/",
            {"work_type": "BREAKDOWN", "production_run": context["run"].id},
        )
        self.assertEqual(work_queue.status_code, status.HTTP_200_OK, work_queue.data)
        self.assertEqual(len(work_queue.data), 1)
        self.assertEqual(work_queue.data[0]["id"], work_order.id)

        detail = self.client.get(f"/api/v1/production-execution/runs/{context['run'].id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK, detail.data)
        self.assertEqual(
            detail.data["breakdowns"][0]["maintenance_work_order_id"],
            work_order.id,
        )

        resolve = self.client.post(
            f"/api/v1/production-execution/runs/{context['run'].id}/breakdowns/{response.data['id']}/resolve/",
            {"action": "start_production"},
            format="json",
        )
        self.assertEqual(resolve.status_code, status.HTTP_200_OK, resolve.data)
        self.assertFalse(resolve.data["is_active"])
        self.assertEqual(resolve.data["maintenance_work_order_status"], "COMPLETED")

        work_order.refresh_from_db()
        context["run"].refresh_from_db()
        self.assertEqual(work_order.status, "COMPLETED")
        self.assertIsNotNone(work_order.end_time)
        self.assertGreaterEqual(context["run"].total_breakdown_time, 0)

    def test_maintenance_completion_stops_active_production_breakdown_timer(self):
        context = self.create_production_breakdown_context()
        create_breakdown = self.client.post(
            f"/api/v1/production-execution/runs/{context['run'].id}/add-breakdown/",
            {
                "breakdown_category_id": context["category"].id,
                "machine_id": context["machine"].id,
                "maintenance_asset_id": context["asset_id"],
                "create_maintenance_work_order": True,
                "reason": "Original production reason",
                "produced_cases": "15",
            },
            format="json",
        )
        self.assertEqual(create_breakdown.status_code, status.HTTP_201_CREATED, create_breakdown.data)
        work_order = MaintenanceWorkOrder.objects.get(
            pk=create_breakdown.data["maintenance_work_order_id"]
        )

        completion_time = timezone.now() + timedelta(minutes=7)
        complete = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order.id}/complete/",
            {
                "completion_remarks": "Motor overload reset and trial completed.",
                "downtime_reason": "Motor overload",
                "end_time": completion_time.isoformat(),
            },
            format="json",
        )
        self.assertEqual(complete.status_code, status.HTTP_200_OK, complete.data)
        self.assertEqual(complete.data["status"], "COMPLETED")

        breakdown = context["run"].breakdowns.get(pk=create_breakdown.data["id"])
        self.assertFalse(breakdown.is_active)
        self.assertEqual(breakdown.reason, "Motor overload")
        self.assertGreaterEqual(breakdown.breakdown_minutes, 6)

        context["run"].refresh_from_db()
        self.assertEqual(context["run"].total_breakdown_time, breakdown.breakdown_minutes)

    def test_spare_request_issue_consume_return_and_low_stock_alerts(self):
        asset_response = self.create_asset()
        asset_id = asset_response.data["id"]
        department_id = asset_response.data["department"]

        category_response = self.client.post(
            "/api/v1/maintenance/spare-categories/",
            {"name": "Bearings", "description": "Rotary spares"},
            format="json",
        )
        self.assertEqual(category_response.status_code, status.HTTP_201_CREATED, category_response.data)

        spare_response = self.client.post(
            "/api/v1/maintenance/spares/",
            {
                "category": category_response.data["id"],
                "name": "Filler shaft bearing",
                "part_number": "brg-6205",
                "sap_item_code": "SAP-BRG-6205",
                "uom": "NOS",
                "compatible_assets": [asset_id],
                "is_critical": True,
                "minimum_stock": "2.000",
                "reorder_level": "5.000",
                "current_stock": "8.000",
                "unit_cost": "125.50",
                "storage_location": "Store Rack A1",
            },
            format="json",
        )
        self.assertEqual(spare_response.status_code, status.HTTP_201_CREATED, spare_response.data)
        spare_id = spare_response.data["id"]
        self.assertEqual(spare_response.data["part_number"], "BRG-6205")
        self.assertFalse(spare_response.data["is_low_stock"])

        work_order_response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "BREAKDOWN",
                "priority": "HIGH",
                "asset": asset_id,
                "department": department_id,
                "title": "Bearing noise",
                "problem_statement": "Filler shaft bearing is noisy.",
                "impact": "DEGRADED",
            },
            format="json",
        )
        self.assertEqual(work_order_response.status_code, status.HTTP_201_CREATED, work_order_response.data)
        work_order_id = work_order_response.data["id"]

        request_response = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/request-spare/",
            {
                "spare": spare_id,
                "requested_qty": "4.000",
                "purpose": "Replace worn shaft bearings",
            },
            format="json",
        )
        self.assertEqual(request_response.status_code, status.HTTP_201_CREATED, request_response.data)
        spare_request_id = request_response.data["id"]
        self.assertEqual(request_response.data["status"], "REQUESTED")
        self.assertEqual(request_response.data["asset"], asset_id)

        issue_response = self.client.post(
            f"/api/v1/maintenance/spare-requests/{spare_request_id}/issue/",
            {"quantity": "4.000", "remarks": "Issued by store"},
            format="json",
        )
        self.assertEqual(issue_response.status_code, status.HTTP_200_OK, issue_response.data)
        self.assertEqual(issue_response.data["status"], "ISSUED")
        self.assertEqual(Decimal(issue_response.data["issued_qty"]), Decimal("4.000"))

        spare = MaintenanceSpare.objects.get(pk=spare_id)
        self.assertEqual(spare.current_stock, Decimal("4.000"))

        low_stock = self.client.get("/api/v1/maintenance/spares/low-stock/")
        self.assertEqual(low_stock.status_code, status.HTTP_200_OK, low_stock.data)
        self.assertEqual(len(low_stock.data), 1)
        self.assertEqual(low_stock.data[0]["id"], spare_id)
        self.assertTrue(low_stock.data[0]["is_low_stock"])

        consume_response = self.client.post(
            f"/api/v1/maintenance/spare-requests/{spare_request_id}/consume/",
            {"quantity": "3.000", "remarks": "Three fitted on machine"},
            format="json",
        )
        self.assertEqual(consume_response.status_code, status.HTTP_200_OK, consume_response.data)
        self.assertEqual(Decimal(consume_response.data["consumed_qty"]), Decimal("3.000"))
        self.assertEqual(Decimal(consume_response.data["available_to_consume_qty"]), Decimal("1.000"))

        return_response = self.client.post(
            f"/api/v1/maintenance/spare-requests/{spare_request_id}/return-unused/",
            {"quantity": "1.000", "remarks": "One returned unused"},
            format="json",
        )
        self.assertEqual(return_response.status_code, status.HTTP_200_OK, return_response.data)
        self.assertEqual(return_response.data["status"], "CLOSED")
        self.assertEqual(Decimal(return_response.data["returned_qty"]), Decimal("1.000"))

        spare.refresh_from_db()
        self.assertEqual(spare.current_stock, Decimal("5.000"))

        work_order_detail = self.client.get(f"/api/v1/maintenance/work-orders/{work_order_id}/")
        self.assertEqual(work_order_detail.status_code, status.HTTP_200_OK, work_order_detail.data)
        self.assertEqual(work_order_detail.data["spare_requests_count"], 1)
        self.assertEqual(Decimal(str(work_order_detail.data["spare_consumed_qty"])), Decimal("3.000"))
        self.assertEqual(Decimal(str(work_order_detail.data["spare_consumed_cost"])), Decimal("376.50"))

        movements = self.client.get(
            "/api/v1/maintenance/spare-movements/",
            {"work_order": work_order_id},
        )
        self.assertEqual(movements.status_code, status.HTTP_200_OK, movements.data)
        self.assertEqual(len(movements.data), 3)
        self.assertEqual(
            {movement["movement_type"] for movement in movements.data},
            {"ISSUE", "CONSUME", "RETURN"},
        )

        dashboard = self.client.get("/api/v1/maintenance/dashboard/")
        self.assertEqual(dashboard.status_code, status.HTTP_200_OK, dashboard.data)
        self.assertEqual(dashboard.data["spares"]["critical"], 1)
        self.assertEqual(dashboard.data["spares"]["low_stock"], 1)
        self.assertEqual(dashboard.data["spares"]["critical_shortage"], 1)
        self.assertEqual(SpareRequest.objects.filter(work_order_id=work_order_id).count(), 1)
        self.assertEqual(SpareMovement.objects.filter(work_order_id=work_order_id).count(), 3)

    def test_gate_entry_auto_links_spare_and_receives_stock_after_qc(self):
        asset_response = self.create_asset(asset_code="MCH-GATE-001")
        work_order_response = self.create_work_order(asset_response)
        spare_response = self.create_spare(asset_response.data["id"])
        vehicle_entry, _gate_response = self.create_maintenance_gate_entry(
            asset_response,
            work_order_response,
            spare_response,
        )

        gate_detail = self.client.get(
            f"/api/v1/maintenance-gatein/gate-entries/{vehicle_entry.id}/maintenance/"
        )
        self.assertEqual(gate_detail.status_code, status.HTTP_200_OK, gate_detail.data)
        link_payload = gate_detail.data["maintenance_link"]
        self.assertEqual(link_payload["asset"], asset_response.data["id"])
        self.assertEqual(link_payload["work_order"], work_order_response.data["id"])
        self.assertEqual(link_payload["spare"], spare_response.data["id"])
        self.assertTrue(link_payload["qc_required"])
        self.assertEqual(link_payload["qc_status"], "PENDING")

        full_view = self.client.get(f"/api/v1/gate-core/maintenance-gate-entry/{vehicle_entry.id}/")
        self.assertEqual(full_view.status_code, status.HTTP_200_OK, full_view.data)
        self.assertEqual(
            full_view.data["maintenance_details"]["maintenance_link"]["asset_code"],
            asset_response.data["asset_code"],
        )
        self.assertEqual(
            full_view.data["maintenance_details"]["maintenance_link"]["spare_part_number"],
            spare_response.data["part_number"],
        )

        blocked_receipt = self.client.post(
            f"/api/v1/maintenance-gatein/gate-entries/{vehicle_entry.id}/maintenance/receive-spare/",
            {"remarks": "QC still pending"},
            format="json",
        )
        self.assertEqual(blocked_receipt.status_code, status.HTTP_400_BAD_REQUEST)
        link = MaintenanceGateLink.objects.get(gate_entry__vehicle_entry=vehicle_entry)
        self.assertEqual(link.receipt_status, "BLOCKED")
        self.assertEqual(link.qc_status, "PENDING")

        receipt_response = self.client.post(
            f"/api/v1/maintenance-gatein/gate-entries/{vehicle_entry.id}/maintenance/receive-spare/",
            {
                "quantity": "2.000",
                "unit_cost": "275.00",
                "qc_status": "ACCEPTED",
                "grpo_reference": "GRPO-MNT-001",
                "grpo_doc_entry": 3456,
                "grpo_doc_num": "700001",
                "remarks": "Accepted by maintenance store",
            },
            format="json",
        )
        self.assertEqual(receipt_response.status_code, status.HTTP_201_CREATED, receipt_response.data)
        self.assertEqual(receipt_response.data["spare"], spare_response.data["id"])
        self.assertEqual(receipt_response.data["grpo_reference"], "GRPO-MNT-001")
        self.assertEqual(receipt_response.data["grpo_doc_num"], "700001")

        spare = MaintenanceSpare.objects.get(pk=spare_response.data["id"])
        self.assertEqual(spare.current_stock, Decimal("2.000"))
        link.refresh_from_db()
        self.assertEqual(link.receipt_status, "RECEIVED")
        self.assertEqual(link.qc_status, "ACCEPTED")
        self.assertEqual(link.received_quantity, Decimal("2.000"))
        self.assertEqual(MaintenanceSpareReceipt.objects.filter(gate_link=link).count(), 1)
        receipt_movement = SpareMovement.objects.get(
            spare_id=spare.id,
            movement_type="RECEIPT",
        )
        self.assertIsNone(receipt_movement.spare_request)
        self.assertEqual(receipt_movement.work_order_id, work_order_response.data["id"])
        self.assertEqual(receipt_movement.quantity, Decimal("2.000"))

    def test_vendor_visit_tracks_gate_person_and_attachments(self):
        asset_response = self.create_asset(asset_code="MCH-VENDOR-001")
        work_order_response = self.create_work_order(
            asset_response,
            work_type="AMC_VENDOR",
            title="AMC service visit",
        )
        spare_response = self.create_spare(asset_response.data["id"], part_number="VEN-SEN-001")
        vehicle_entry, _gate_response = self.create_maintenance_gate_entry(
            asset_response,
            work_order_response,
            spare_response,
        )
        person_type = PersonType.objects.create(name="Visitor")
        gate = Gate.objects.create(name="Main Gate")
        visitor = Visitor.objects.create(name="Vendor Engineer", mobile="8888888888", company_name="OEM")
        person_entry = EntryLog.objects.create(
            person_type=person_type,
            visitor=visitor,
            name_snapshot=visitor.name,
            gate_in=gate,
            purpose="AMC service visit",
            approved_by=self.user,
            status="IN",
            created_by=self.user,
        )

        service_report = SimpleUploadedFile(
            "service-report.pdf",
            b"service-report",
            content_type="application/pdf",
        )
        invoice = SimpleUploadedFile("vendor-invoice.pdf", b"invoice", content_type="application/pdf")
        response = self.client.post(
            "/api/v1/maintenance/vendor-visits/",
            {
                "work_order": work_order_response.data["id"],
                "asset": asset_response.data["id"],
                "vendor_code": "VENDA0001",
                "vendor_name": "OEM Service Partner",
                "contact_person": "Vendor Engineer",
                "contact_phone": "8888888888",
                "planned_start": timezone.now().isoformat(),
                "person_gate_entry": person_entry.id,
                "material_gate_entry": vehicle_entry.maintenance_entry.id,
                "service_report_attachment": service_report,
                "invoice_number": "AMC-INV-001",
                "invoice_attachment": invoice,
                "remarks": "Linked vendor visit from gate-in to work order",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["work_order"], work_order_response.data["id"])
        self.assertEqual(response.data["asset"], asset_response.data["id"])
        self.assertEqual(response.data["person_gate_entry"], person_entry.id)
        self.assertEqual(response.data["material_gate_entry"], vehicle_entry.maintenance_entry.id)
        self.assertTrue(response.data["service_report_attachment"])
        self.assertTrue(response.data["invoice_attachment"])

        work_order = MaintenanceWorkOrder.objects.get(pk=work_order_response.data["id"])
        self.assertEqual(work_order.status, "WAITING_VENDOR")
        visit = MaintenanceVendorVisit.objects.get(pk=response.data["id"])
        self.assertEqual(visit.material_gate_entry_id, vehicle_entry.maintenance_entry.id)
        self.assertEqual(visit.person_gate_entry_id, person_entry.id)

        start_response = self.client.post(f"/api/v1/maintenance/vendor-visits/{visit.id}/start/")
        self.assertEqual(start_response.status_code, status.HTTP_200_OK, start_response.data)
        self.assertEqual(start_response.data["status"], "IN_PROGRESS")
        complete_response = self.client.post(f"/api/v1/maintenance/vendor-visits/{visit.id}/complete/")
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK, complete_response.data)
        self.assertEqual(complete_response.data["status"], "COMPLETED")

    def test_company_context_is_required(self):
        self.client.credentials()
        response = self.client.get("/api/v1/maintenance/assets/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
