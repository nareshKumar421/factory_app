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
    AssetCategory,
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

        # Assets/work orders reference the global accounts.Department, not the
        # maintenance AssetDepartment master, so create one to link them to.
        org_department = Department.objects.create(name="Production")

        return category.data, location.data, {"id": org_department.id, "name": org_department.name}

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

    def test_asset_created_without_a_category_falls_back_to_general(self):
        _category, location, department = self.create_master_data()
        payload = {
            "asset_code": "MCH-NO-CAT",
            "name": "Unclassified pump",
            "location": location["id"],
            "department": department["id"],
            "status": "RUNNING",
        }
        first = self.client.post("/api/v1/maintenance/assets/", payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(first.data["category_name"], "General")

        # The bucket is created once and reused by every category-less asset.
        second = self.client.post(
            "/api/v1/maintenance/assets/",
            {**payload, "asset_code": "MCH-NO-CAT-2", "name": "Another pump"},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)
        self.assertEqual(second.data["category"], first.data["category"])
        self.assertEqual(
            AssetCategory.objects.filter(company=self.company, name="General").count(), 1
        )

        # An update that omits the category must not clear it.
        updated = self.client.put(
            f"/api/v1/maintenance/assets/{first.data['id']}/",
            {**payload, "name": "Unclassified pump A"},
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK, updated.data)
        self.assertEqual(updated.data["category"], first.data["category"])

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
            {"assigned_to_text": "Maintenance Technician (MNT002)", "target_date": "2026-06-04"},
            format="json",
        )
        self.assertEqual(assign_response.status_code, status.HTTP_200_OK, assign_response.data)
        self.assertEqual(assign_response.data["status"], "ASSIGNED")
        self.assertEqual(assign_response.data["assigned_to"], self.technician.id)
        self.assertEqual(
            assign_response.data["assigned_to_display"], "Maintenance Technician"
        )

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

    def _grant(self, user, *codenames):
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="maintenance", codename__in=codenames
            )
        )

    def _as_technician(self):
        self.client.force_authenticate(self.technician)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _as_raiser(self):
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def test_assignee_can_be_typed_in_when_nobody_on_the_system_does_the_work(self):
        asset_response = self.create_asset()
        work_order = self.create_work_order(asset_response)
        work_order_id = work_order.data["id"]

        response = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/assign/",
            {"assigned_to_text": "Ramesh (contract fitter)"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "ASSIGNED")
        # Nobody on the system matches, so the name stands on its own.
        self.assertIsNone(response.data["assigned_to"])
        self.assertEqual(response.data["assigned_to_text"], "Ramesh (contract fitter)")
        self.assertEqual(response.data["assigned_to_display"], "Ramesh (contract fitter)")

        blank = self.client.post(
            f"/api/v1/maintenance/work-orders/{work_order_id}/assign/",
            {"assigned_to_text": "   "},
            format="json",
        )
        self.assertEqual(blank.status_code, status.HTTP_400_BAD_REQUEST, blank.data)
        self.assertIn("assigned_to_text", blank.data)

    def test_raiser_verifies_the_work_and_can_send_it_back_with_remarks(self):
        asset_response = self.create_asset()
        work_order = self.create_work_order(asset_response)
        work_order_id = work_order.data["id"]
        url = f"/api/v1/maintenance/work-orders/{work_order_id}"

        assign = self.client.post(
            f"{url}/assign/",
            {"assigned_to_text": "Maintenance Technician (MNT002)"},
            format="json",
        )
        self.assertEqual(assign.status_code, status.HTTP_200_OK, assign.data)
        self.assertEqual(assign.data["assigned_to"], self.technician.id)

        # The technician works the job; they cannot sign it off themselves.
        self._grant(
            self.technician,
            "can_view_work_order",
            "can_start_work_order",
            "can_complete_work_order",
        )
        self._as_technician()
        self.assertEqual(self.client.post(f"{url}/start/").status_code, status.HTTP_200_OK)
        completed = self.client.post(
            f"{url}/complete/",
            {"completion_remarks": "Tightened the coupling."},
            format="json",
        )
        self.assertEqual(completed.status_code, status.HTTP_200_OK, completed.data)
        self.assertEqual(completed.data["status"], "COMPLETED")
        self.assertFalse(completed.data["can_verify"])

        denied = self.client.post(f"{url}/approve/", {}, format="json")
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN, denied.data)
        sent_back_by_worker = self.client.post(
            f"{url}/send-back/", {"remarks": "Looks fine to me"}, format="json"
        )
        self.assertEqual(
            sent_back_by_worker.status_code, status.HTTP_403_FORBIDDEN, sent_back_by_worker.data
        )

        # Back to the raiser, who is not satisfied.
        self._as_raiser()
        pending = self.client.get(f"{url}/")
        self.assertTrue(pending.data["can_verify"])

        no_reason = self.client.post(f"{url}/send-back/", {"remarks": "  "}, format="json")
        self.assertEqual(no_reason.status_code, status.HTTP_400_BAD_REQUEST, no_reason.data)

        sent_back = self.client.post(
            f"{url}/send-back/",
            {"remarks": "Still leaking oil after an hour."},
            format="json",
        )
        self.assertEqual(sent_back.status_code, status.HTTP_200_OK, sent_back.data)
        self.assertEqual(sent_back.data["status"], "REOPENED")
        self.assertEqual(sent_back.data["rework_count"], 1)
        self.assertIsNone(sent_back.data["completed_at"])
        self.assertIsNone(sent_back.data["end_time"])

        # Rework, then the raiser accepts it and closes the job.
        self._as_technician()
        second = self.client.post(
            f"{url}/complete/",
            {"completion_remarks": "Replaced the seal."},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)
        self.assertEqual(second.data["status"], "COMPLETED")

        self._as_raiser()
        approved = self.client.post(
            f"{url}/approve/", {"closure_remarks": "Dry now."}, format="json"
        )
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)
        self.assertEqual(approved.data["status"], "APPROVED")
        closed = self.client.post(f"{url}/close/")
        self.assertEqual(closed.status_code, status.HTTP_200_OK, closed.data)
        self.assertEqual(closed.data["status"], "CLOSED")

        logs = self.client.get(f"{url}/logs/")
        self.assertEqual(logs.status_code, status.HTTP_200_OK, logs.data)
        self.assertEqual(
            [row["action"] for row in logs.data],
            [
                "ASSIGNED",
                "STARTED",
                "COMPLETED",
                "SENT_BACK",
                "COMPLETED",
                "VERIFIED",
                "CLOSED",
            ],
        )
        sent_back_log = next(row for row in logs.data if row["action"] == "SENT_BACK")
        self.assertEqual(sent_back_log["remarks"], "Still leaking oil after an hour.")
        self.assertEqual(sent_back_log["status"], "REOPENED")
        self.assertEqual(sent_back_log["created_by"], self.user.id)
        # Both rounds of completion remarks survive the loop.
        self.assertEqual(
            [row["remarks"] for row in logs.data if row["action"] == "COMPLETED"],
            ["Tightened the coupling.", "Replaced the seal."],
        )

    def test_a_maintenance_head_can_verify_a_job_they_did_not_raise(self):
        asset_response = self.create_asset()
        self._as_technician()
        self._grant(
            self.technician,
            "can_view_work_order",
            "can_create_work_order",
            "add_maintenanceworkorder",
            "can_complete_work_order",
        )
        raised = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "COMPLAINT",
                "priority": "NORMAL",
                "asset": asset_response.data["id"],
                "department": asset_response.data["department"],
                "title": "Stacker oil leakage",
                "problem_statement": "Oil under the stacker.",
                "impact": "NO_IMPACT",
            },
            format="json",
        )
        self.assertEqual(raised.status_code, status.HTTP_201_CREATED, raised.data)
        work_order_id = raised.data["id"]
        url = f"/api/v1/maintenance/work-orders/{work_order_id}"
        self.client.post(
            f"{url}/complete/", {"completion_remarks": "Wiped and re-torqued."}, format="json"
        )

        # The head did not raise it but holds can_approve_work_order.
        self._as_raiser()
        detail = self.client.get(f"{url}/")
        self.assertTrue(detail.data["can_verify"])
        approved = self.client.post(f"{url}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)
        self.assertEqual(approved.data["status"], "APPROVED")

    def test_work_order_accepts_a_typed_in_asset(self):
        _, _, department = self.create_master_data()
        response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "COMPLAINT",
                "priority": "NORMAL",
                "asset_text": "Stacker near dock 3",
                "department": department["id"],
                "title": "Stacker oil leak",
                "problem_statement": "Oil dripping from the mast.",
                "impact": "NO_IMPACT",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["asset"])
        self.assertEqual(response.data["asset_text"], "Stacker near dock 3")
        self.assertEqual(response.data["asset_code"], "")

        listing = self.client.get("/api/v1/maintenance/work-orders/?search=dock 3")
        self.assertEqual(listing.status_code, status.HTTP_200_OK, listing.data)
        self.assertEqual([row["id"] for row in listing.data], [response.data["id"]])

    def test_typed_in_asset_work_order_needs_a_department(self):
        response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "COMPLAINT",
                "priority": "NORMAL",
                "asset_text": "Stacker near dock 3",
                "title": "Stacker oil leak",
                "problem_statement": "Oil dripping from the mast.",
                "impact": "NO_IMPACT",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn("department", response.data)

    def test_work_order_on_a_master_asset_drops_the_typed_in_asset(self):
        asset_response = self.create_asset()
        response = self.client.post(
            "/api/v1/maintenance/work-orders/",
            {
                "work_type": "BREAKDOWN",
                "priority": "HIGH",
                "asset": asset_response.data["id"],
                "asset_text": "typed by mistake",
                "title": "Filler repair",
                "problem_statement": "Filler needs urgent repair.",
                "impact": "DEGRADED",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["asset_text"], "")
        self.assertEqual(response.data["asset_code"], "MCH-001")
        # department comes from the asset when the form does not send one
        self.assertEqual(response.data["department"], asset_response.data["department"])

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

    def test_vendor_visit_invalid_state_transitions_are_rejected(self):
        asset_response = self.create_asset(asset_code="MCH-VEN-GUARD", qr_code="QR-VEN-GUARD")
        work_order_response = self.create_work_order(
            asset_response,
            work_type="AMC_VENDOR",
            title="Guarded vendor visit",
        )

        def _create_visit():
            response = self.client.post(
                "/api/v1/maintenance/vendor-visits/",
                {
                    "work_order": work_order_response.data["id"],
                    "asset": asset_response.data["id"],
                    "vendor_name": "OEM Service Partner",
                    "planned_start": timezone.now().isoformat(),
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
            return response.data["id"]

        # A cancelled visit cannot be started or completed.
        cancelled_id = _create_visit()
        cancel_response = self.client.post(f"/api/v1/maintenance/vendor-visits/{cancelled_id}/cancel/")
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK, cancel_response.data)
        self.assertEqual(cancel_response.data["status"], "CANCELLED")
        self.assertEqual(
            self.client.post(f"/api/v1/maintenance/vendor-visits/{cancelled_id}/start/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/maintenance/vendor-visits/{cancelled_id}/complete/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # A completed visit cannot be cancelled (or re-completed).
        completed_id = _create_visit()
        self.client.post(f"/api/v1/maintenance/vendor-visits/{completed_id}/start/")
        complete_response = self.client.post(f"/api/v1/maintenance/vendor-visits/{completed_id}/complete/")
        self.assertEqual(complete_response.status_code, status.HTTP_200_OK, complete_response.data)
        self.assertEqual(
            self.client.post(f"/api/v1/maintenance/vendor-visits/{completed_id}/cancel/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/maintenance/vendor-visits/{completed_id}/complete/").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_pm_generation_advances_due_date_even_when_executions_exist(self):
        asset_response = self.create_asset(asset_code="PM-ADV-001", qr_code="QR-PM-ADV-001")
        today = timezone.localdate()
        plan_response = self.client.post(
            "/api/v1/maintenance/pm-plans/",
            {
                "title": "Daily advance PM",
                "asset": asset_response.data["id"],
                "frequency": "DAILY",
                "work_type": "PREVENTIVE",
                "priority": "NORMAL",
                "start_date": today.isoformat(),
                "next_due_date": today.isoformat(),
                "advance_days": 0,
                "auto_create_work_order": False,
                "checklist_required": False,
            },
            format="json",
        )
        self.assertEqual(plan_response.status_code, status.HTTP_201_CREATED, plan_response.data)
        plan = PreventiveMaintenancePlan.objects.get(pk=plan_response.data["id"])

        first = self.client.post(
            "/api/v1/maintenance/pm-plans/generate-due/",
            {"due_until": today.isoformat()},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(first.data["generated_count"], 1)
        plan.refresh_from_db()
        self.assertEqual(plan.next_due_date, today + timedelta(days=1))

        # Simulate a plan still pointing at an already-generated due date. Re-running
        # generation must move next_due_date forward even though no new execution is
        # created, otherwise the plan stays "due" forever.
        plan.next_due_date = today
        plan.save(update_fields=["next_due_date"])

        second = self.client.post(
            "/api/v1/maintenance/pm-plans/generate-due/",
            {"due_until": today.isoformat()},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)
        self.assertEqual(second.data["generated_count"], 0)
        plan.refresh_from_db()
        self.assertEqual(plan.next_due_date, today + timedelta(days=1))

    def test_spare_stock_is_ledger_controlled_and_adjustable(self):
        asset_response = self.create_asset(asset_code="MCH-STK-001", qr_code="QR-STK-001")
        spare_response = self.create_spare(
            asset_response.data["id"],
            part_number="STK-CTRL-001",
            current_stock="5.000",
        )
        spare_id = spare_response.data["id"]

        # current_stock cannot be changed via a direct update; it is ignored, so
        # on-hand stock can only move through the movement ledger.
        patch_response = self.client.patch(
            f"/api/v1/maintenance/spares/{spare_id}/",
            {"current_stock": "999.000", "storage_location": "MNT-B2"},
            format="json",
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK, patch_response.data)
        spare = MaintenanceSpare.objects.get(pk=spare_id)
        self.assertEqual(spare.current_stock, Decimal("5.000"))
        self.assertEqual(spare.storage_location, "MNT-B2")

        # A reason is mandatory for an adjustment.
        missing_reason = self.client.post(
            f"/api/v1/maintenance/spares/{spare_id}/adjust-stock/",
            {"new_stock": "8.000"},
            format="json",
        )
        self.assertEqual(missing_reason.status_code, status.HTTP_400_BAD_REQUEST)

        # Adjusting up to a counted value records an ADJUSTMENT movement.
        adjust_up = self.client.post(
            f"/api/v1/maintenance/spares/{spare_id}/adjust-stock/",
            {"new_stock": "8.000", "reason": "Cycle count correction"},
            format="json",
        )
        self.assertEqual(adjust_up.status_code, status.HTTP_200_OK, adjust_up.data)
        spare.refresh_from_db()
        self.assertEqual(spare.current_stock, Decimal("8.000"))
        movement = SpareMovement.objects.get(spare=spare, movement_type="ADJUSTMENT")
        self.assertEqual(movement.quantity, Decimal("3.000"))

        # A no-op adjustment and a negative target are rejected.
        self.assertEqual(
            self.client.post(
                f"/api/v1/maintenance/spares/{spare_id}/adjust-stock/",
                {"new_stock": "8.000", "reason": "again"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/maintenance/spares/{spare_id}/adjust-stock/",
                {"new_stock": "-1.000", "reason": "bad"},
                format="json",
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        # Adjusting down also works and records a second movement.
        adjust_down = self.client.post(
            f"/api/v1/maintenance/spares/{spare_id}/adjust-stock/",
            {"new_stock": "6.000", "reason": "Damaged units scrapped"},
            format="json",
        )
        self.assertEqual(adjust_down.status_code, status.HTTP_200_OK, adjust_down.data)
        spare.refresh_from_db()
        self.assertEqual(spare.current_stock, Decimal("6.000"))
        self.assertEqual(
            SpareMovement.objects.filter(spare=spare, movement_type="ADJUSTMENT").count(),
            2,
        )

    def test_company_context_is_required(self):
        self.client.credentials()
        response = self.client.get("/api/v1/maintenance/assets/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class WorkPermitAPITests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Maintenance Head")
        self.user = get_user_model().objects.create_user(
            email="permit@example.com",
            password="testpass123",
            full_name="Permit User",
            employee_code="MNT-WP-1",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="maintenance")
        )
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _create_permit(self):
        response = self.client.post(
            "/api/v1/maintenance/work-permits/",
            {
                "permit_types": ["HOT_WORK", "HEIGHT"],
                "valid_date": timezone.localdate().isoformat(),
                "time_start": "09:00",
                "time_end": "17:00",
                "job_location": "Plant 1 - Roof",
                "job_description": "Weld a support bracket at height",
                "hazards_identified": ["FLAMMABLES", "HEIGHT_WORK"],
                "ppe": ["HELMET", "FULL_HARNESS_BELT"],
                "precautions": ["FIRE_EQUIP_PROVIDED"],
                "workers_input": [
                    {"name": "Ramesh", "role": "Welder"},
                    {"name": "Suresh"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def test_create_assigns_serial_and_draft_status(self):
        data = self._create_permit()
        self.assertEqual(data["status"], "DRAFT")
        self.assertTrue(data["serial_no"].startswith("D-"))
        self.assertEqual(data["total_workers"], 2)
        self.assertEqual(len(data["workers"]), 2)

    def test_full_lifecycle(self):
        """Maintenance submits -> Fire head approves -> maintenance works -> close."""
        permit_id = self._create_permit()["id"]

        submit = self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/submit/")
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        self.assertEqual(submit.data["status"], "SUBMITTED")
        self.assertEqual(submit.data["submitted_by_name"], "Permit User")

        approve = self.client.post(
            f"/api/v1/maintenance/work-permits/{permit_id}/approve/",
            {"remarks": "Cleared by fire dept", "ppe": ["HELMET", "SAFETY_SHOES"]},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        self.assertEqual(approve.data["status"], "APPROVED")
        self.assertEqual(approve.data["approvals_count"], 1)
        self.assertEqual(approve.data["approved_by_name"], "Permit User")
        # PPE is set by the Fire Head at approval, not by the requester.
        self.assertEqual(approve.data["ppe"], ["HELMET", "SAFETY_SHOES"])

        start = self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/start/")
        self.assertEqual(start.status_code, status.HTTP_200_OK, start.data)
        self.assertEqual(start.data["status"], "IN_PROGRESS")

        complete = self.client.post(
            f"/api/v1/maintenance/work-permits/{permit_id}/complete/",
            {"completion_type": "VERIFIED"},
            format="json",
        )
        self.assertEqual(complete.status_code, status.HTTP_200_OK, complete.data)
        self.assertEqual(complete.data["status"], "COMPLETED")

        close = self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/close/")
        self.assertEqual(close.status_code, status.HTTP_200_OK, close.data)
        self.assertEqual(close.data["status"], "CLOSED")

    def test_cannot_approve_a_draft(self):
        permit_id = self._create_permit()["id"]
        approve = self.client.post(
            f"/api/v1/maintenance/work-permits/{permit_id}/approve/",
            {},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_start_before_approval(self):
        permit_id = self._create_permit()["id"]
        self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/submit/")
        start = self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/start/")
        self.assertEqual(start.status_code, status.HTTP_400_BAD_REQUEST)

    def test_permit_type_is_required(self):
        response = self.client.post(
            "/api/v1/maintenance/work-permits/",
            {
                "permit_types": [],
                "valid_date": timezone.localdate().isoformat(),
                "job_location": "X",
                "job_description": "Y",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_filter(self):
        self._create_permit()
        submitted_id = self._create_permit()["id"]
        self.client.post(f"/api/v1/maintenance/work-permits/{submitted_id}/submit/")

        response = self.client.get("/api/v1/maintenance/work-permits/?status=SUBMITTED")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], submitted_id)

    def test_expiry_command_marks_lapsed_permit_expired(self):
        from datetime import timedelta

        from maintenance.constants import WorkPermitStatus
        from maintenance.models import WorkPermit

        permit_id = self._create_permit()["id"]
        self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/submit/")
        self.client.post(
            f"/api/v1/maintenance/work-permits/{permit_id}/approve/", {}, format="json"
        )
        # Back-date the validity so it is already past.
        WorkPermit.objects.filter(id=permit_id).update(
            valid_date=timezone.localdate() - timedelta(days=1), time_end=None
        )

        from django.core.management import call_command

        call_command("expire_work_permits")

        permit = WorkPermit.objects.get(id=permit_id)
        self.assertEqual(permit.status, WorkPermitStatus.EXPIRED)
        self.assertIsNotNone(permit.expired_at)

    def test_renew_clones_expired_permit_to_new_draft(self):
        from datetime import timedelta

        from maintenance.models import WorkPermit

        permit_id = self._create_permit()["id"]
        WorkPermit.objects.filter(id=permit_id).update(status="EXPIRED")

        renew = self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/renew/")
        self.assertEqual(renew.status_code, status.HTTP_201_CREATED, renew.data)
        self.assertEqual(renew.data["status"], "DRAFT")
        self.assertEqual(renew.data["renewed_from"], permit_id)
        self.assertEqual(renew.data["total_workers"], 2)
        self.assertNotEqual(renew.data["id"], permit_id)

    def test_multi_day_permit_valid_to_before_from_is_rejected(self):
        from datetime import timedelta

        today = timezone.localdate()
        response = self.client.post(
            "/api/v1/maintenance/work-permits/",
            {
                "permit_types": ["GENERAL"],
                "valid_date": today.isoformat(),
                "valid_to": (today - timedelta(days=1)).isoformat(),
                "job_location": "Plant 1",
                "job_description": "Multi-day job",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expiry_respects_multi_day_valid_to(self):
        from datetime import timedelta

        from maintenance.constants import WorkPermitStatus
        from maintenance.models import WorkPermit
        from django.core.management import call_command

        today = timezone.localdate()
        # A 3-day permit that started yesterday and is still valid until tomorrow.
        response = self.client.post(
            "/api/v1/maintenance/work-permits/",
            {
                "permit_types": ["GENERAL"],
                "valid_date": (today - timedelta(days=1)).isoformat(),
                "valid_to": (today + timedelta(days=1)).isoformat(),
                "job_location": "Plant 1",
                "job_description": "Three-day shutdown job",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        permit_id = response.data["id"]
        self.client.post(f"/api/v1/maintenance/work-permits/{permit_id}/submit/")
        self.client.post(
            f"/api/v1/maintenance/work-permits/{permit_id}/approve/", {}, format="json"
        )

        # Still within the range -> must NOT expire.
        call_command("expire_work_permits")
        self.assertNotEqual(
            WorkPermit.objects.get(id=permit_id).status, WorkPermitStatus.EXPIRED
        )

        # Push valid_to into the past -> now it should expire.
        WorkPermit.objects.filter(id=permit_id).update(
            valid_to=today - timedelta(days=1), time_end=None
        )
        call_command("expire_work_permits")
        self.assertEqual(
            WorkPermit.objects.get(id=permit_id).status, WorkPermitStatus.EXPIRED
        )


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class SafetyFineAPITests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Fire Head")
        self.user = get_user_model().objects.create_user(
            email="firehead@example.com",
            password="testpass123",
            full_name="Fire Head",
            employee_code="FH-1",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="maintenance")
        )
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _create_violation_type(self, name="No Helmet", amount="500.00"):
        response = self.client.post(
            "/api/v1/maintenance/safety-violation-types/",
            {"name": name, "default_fine_amount": amount},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def _create_fine(self, violation_type_id, amount=None):
        payload = {
            "violation_type": violation_type_id,
            "offender_name": "Ramesh Kumar",
            "employee_code": "EMP-77",
            "location": "Filling Line 2",
            "ppe_missing": ["HELMET", "SAFETY_SHOES"],
            "description": "Found without helmet on the floor",
        }
        if amount is not None:
            payload["fine_amount"] = amount
        response = self.client.post(
            "/api/v1/maintenance/safety-fines/", payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def test_create_fine_assigns_number_and_defaults_amount_from_type(self):
        vt = self._create_violation_type(amount="500.00")
        fine = self._create_fine(vt["id"])
        self.assertTrue(fine["fine_no"].startswith("SF-"))
        self.assertEqual(fine["status"], "PENDING")
        self.assertEqual(Decimal(fine["fine_amount"]), Decimal("500.00"))
        self.assertEqual(fine["issued_by_name"], "Fire Head")
        self.assertEqual(fine["violation_type_name"], "No Helmet")

    def test_fine_amount_can_be_overridden(self):
        vt = self._create_violation_type(amount="500.00")
        fine = self._create_fine(vt["id"], amount="250.00")
        self.assertEqual(Decimal(fine["fine_amount"]), Decimal("250.00"))

    def test_settle_marks_fine_paid(self):
        vt = self._create_violation_type()
        fine_id = self._create_fine(vt["id"])["id"]
        response = self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "PAID"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "PAID")
        self.assertEqual(response.data["settled_by_name"], "Fire Head")

    def test_waiving_requires_a_reason(self):
        vt = self._create_violation_type()
        fine_id = self._create_fine(vt["id"])["id"]

        no_reason = self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "WAIVED"},
            format="json",
        )
        self.assertEqual(no_reason.status_code, status.HTTP_400_BAD_REQUEST)

        with_reason = self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "WAIVED", "settlement_remarks": "First offence, warned"},
            format="json",
        )
        self.assertEqual(with_reason.status_code, status.HTTP_200_OK, with_reason.data)
        self.assertEqual(with_reason.data["status"], "WAIVED")

    def test_cannot_settle_an_already_settled_fine(self):
        vt = self._create_violation_type()
        fine_id = self._create_fine(vt["id"])["id"]
        self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "PAID"},
            format="json",
        )
        again = self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "PAID"},
            format="json",
        )
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_filter(self):
        vt = self._create_violation_type()
        self._create_fine(vt["id"])
        paid_id = self._create_fine(vt["id"])["id"]
        self.client.post(
            f"/api/v1/maintenance/safety-fines/{paid_id}/settle/",
            {"status": "PAID"},
            format="json",
        )
        response = self.client.get("/api/v1/maintenance/safety-fines/?status=PAID")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], paid_id)

    def _make_view_only_user(self, extra_codenames=()):
        """A user with can_view_safety_fine only (plus any extra codenames)."""
        viewer = get_user_model().objects.create_user(
            email="maint.viewer@example.com",
            password="testpass123",
            full_name="Maintenance Dept User",
            employee_code="MD-1",
        )
        UserCompany.objects.create(
            user=viewer,
            company=self.company,
            role=UserRole.objects.create(name="Maintenance Department"),
            is_default=True,
            is_active=True,
        )
        codenames = ["can_view_safety_fine", *extra_codenames]
        viewer.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="maintenance", codename__in=codenames
            )
        )
        return viewer

    def test_view_only_user_can_list_but_cannot_create_or_settle(self):
        vt = self._create_violation_type()
        fine_id = self._create_fine(vt["id"])["id"]

        viewer = self._make_view_only_user()
        self.client.force_authenticate(viewer)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        listing = self.client.get("/api/v1/maintenance/safety-fines/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK, listing.data)
        self.assertEqual(len(listing.data), 1)

        created = self.client.post(
            "/api/v1/maintenance/safety-fines/",
            {"violation_type": vt["id"], "offender_name": "Someone"},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)

        settled = self.client.post(
            f"/api/v1/maintenance/safety-fines/{fine_id}/settle/",
            {"status": "PAID"},
            format="json",
        )
        self.assertEqual(settled.status_code, status.HTTP_403_FORBIDDEN)

        made_type = self.client.post(
            "/api/v1/maintenance/safety-violation-types/",
            {"name": "No Goggles", "default_fine_amount": "100"},
            format="json",
        )
        self.assertEqual(made_type.status_code, status.HTTP_403_FORBIDDEN)

    def test_model_add_permission_alone_does_not_allow_creating_fines(self):
        """add_safetyfine must NOT stand in for can_manage_safety_fine."""
        vt = self._create_violation_type()

        viewer = self._make_view_only_user(extra_codenames=["add_safetyfine", "change_safetyfine"])
        self.client.force_authenticate(viewer)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        created = self.client.post(
            "/api/v1/maintenance/safety-fines/",
            {"violation_type": vt["id"], "offender_name": "Someone"},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class MaterialIndentAPITests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Store")
        self.user = get_user_model().objects.create_user(
            email="store@example.com",
            password="testpass123",
            full_name="Store User",
            employee_code="ST-1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_default=True, is_active=True,
        )
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label="maintenance")
        )
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _create_indent(self):
        response = self.client.post(
            "/api/v1/maintenance/material-indents/",
            {
                "indent_date": timezone.localdate().isoformat(),
                "purpose": "Stationery",
                "requested_by_name": "Vikram",
                "items_input": [
                    {"particulars": "A4 Paper box", "quantity": "30", "unit": "NOS"},
                    {"particulars": "Pen", "quantity": "40", "unit": "NOS", "specification": "Blue"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def _reviewer(self, *codenames):
        """A separate user with only the given maintenance permissions."""
        u = get_user_model().objects.create_user(
            email=f"role-{codenames[0]}@example.com", password="x",
            full_name=f"Role {codenames[0]}", employee_code=f"R-{codenames[0][:6]}",
        )
        UserCompany.objects.create(
            user=u, company=self.company,
            role=UserRole.objects.create(name=codenames[0]), is_default=True, is_active=True,
        )
        u.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="maintenance",
                codename__in=["can_view_material_indent", *codenames],
            )
        )
        return u

    def test_create_assigns_number_and_draft(self):
        data = self._create_indent()
        self.assertEqual(data["status"], "DRAFT")
        self.assertTrue(data["indent_no"].startswith("MI-"))
        self.assertEqual(data["total_items"], 2)

    def test_create_with_department_and_priority(self):
        department = Department.objects.create(name="Stores")
        response = self.client.post(
            "/api/v1/maintenance/material-indents/",
            {
                "indent_date": timezone.localdate().isoformat(),
                "purpose": "Stationery",
                "department": department.id,
                "items_input": [
                    {"particulars": "Pen marker", "quantity": "1", "unit": "BOX", "priority": "HIGH"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["department"], department.id)
        self.assertEqual(response.data["items"][0]["priority"], "HIGH")

    def test_cannot_submit_without_items(self):
        response = self.client.post(
            "/api/v1/maintenance/material-indents/",
            {"indent_date": timezone.localdate().isoformat(), "purpose": "X"},
            format="json",
        )
        indent_id = response.data["id"]
        submit = self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/submit/")
        self.assertEqual(submit.status_code, status.HTTP_400_BAD_REQUEST)

    def _submit(self, indent):
        indent_id = indent["id"]
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/submit/")
        return indent_id

    def test_store_issues_everything_becomes_issued(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        items = data["items"]

        review = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {
                "items": [
                    {"id": items[0]["id"], "issued_quantity": "30"},
                    {"id": items[1]["id"], "issued_quantity": "40"},
                ],
                "store_remarks": "All in stock",
            },
            format="json",
        )
        self.assertEqual(review.status_code, status.HTTP_200_OK, review.data)
        self.assertEqual(review.data["status"], "ISSUED")
        self.assertFalse(review.data["has_shortfall"])

    def test_store_shortfall_forwards_for_purchase_then_purchased(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        items = data["items"]

        # Store issues 30/30 of item 1 but only 10/40 of item 2 -> shortfall 30.
        review = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {
                "items": [
                    {"id": items[0]["id"], "issued_quantity": "30"},
                    {"id": items[1]["id"], "issued_quantity": "10"},
                ],
                "store_remarks": "Pens short",
            },
            format="json",
        )
        self.assertEqual(review.status_code, status.HTTP_200_OK, review.data)
        self.assertEqual(review.data["status"], "PENDING_APPROVAL")
        self.assertTrue(review.data["has_shortfall"])
        pen = next(i for i in review.data["items"] if i["id"] == items[1]["id"])
        self.assertEqual(Decimal(pen["issued_quantity"]), Decimal("10"))
        self.assertEqual(Decimal(pen["shortfall_quantity"]), Decimal("30"))

        approve = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/approve/",
            {"decision_remarks": "Buy the rest"},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        self.assertEqual(approve.data["status"], "APPROVED")

        purchase = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/purchase/",
            {"purchase_remarks": "PO-123 to ABC Traders"},
            format="json",
        )
        self.assertEqual(purchase.status_code, status.HTTP_200_OK, purchase.data)
        self.assertEqual(purchase.data["status"], "PURCHASED")
        self.assertEqual(purchase.data["purchase_remarks"], "PO-123 to ABC Traders")

    def test_review_requires_review_permission(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        requester = self._reviewer("can_manage_material_indent")  # no review perm
        self.client.force_authenticate(requester)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        review = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {"items": []},
            format="json",
        )
        self.assertEqual(review.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_approve_before_store_forwards(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        # Still SUBMITTED (not reviewed) -> approve must fail.
        approve = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/approve/", {}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_400_BAD_REQUEST)

    def test_purchase_requires_purchase_permission(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        items = data["items"]
        self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {"items": [{"id": items[0]["id"], "issued_quantity": "0"},
                       {"id": items[1]["id"], "issued_quantity": "0"}]},
            format="json",
        )
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/approve/", {}, format="json")
        buyer = self._reviewer("can_manage_material_indent")  # no purchase perm
        self.client.force_authenticate(buyer)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        purchase = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/purchase/", {}, format="json"
        )
        self.assertEqual(purchase.status_code, status.HTTP_403_FORBIDDEN)

    def test_status_filter(self):
        self._create_indent()
        submitted = self._create_indent()
        self._submit(submitted)
        response = self.client.get("/api/v1/maintenance/material-indents/?status=SUBMITTED")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], submitted["id"])

    def test_purchase_to_gate_in_to_stock_flow(self):
        from decimal import Decimal

        from maintenance.models import MaintenanceSpare, SpareMovement

        data = self._create_indent()
        indent_id = self._submit(data)
        items = data["items"]  # [A4 Paper box qty 30, Pen qty 40]

        # Store issues all A4 but none of the Pens -> Pen shortfall 40 to purchase.
        self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {"items": [{"id": items[0]["id"], "issued_quantity": "30"},
                       {"id": items[1]["id"], "issued_quantity": "0"}]},
            format="json",
        )
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/approve/", {}, format="json")
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/purchase/", {}, format="json")

        gate_in = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/gate-in/",
            {"vehicle_number": "HR55-1234", "driver_name": "Ravi"},
            format="json",
        )
        self.assertEqual(gate_in.status_code, status.HTTP_200_OK, gate_in.data)
        self.assertEqual(gate_in.data["status"], "GATE_IN")
        self.assertEqual(gate_in.data["gatein_vehicle_number"], "HR55-1234")

        receive = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/receive/", {}, format="json"
        )
        self.assertEqual(receive.status_code, status.HTTP_200_OK, receive.data)
        self.assertEqual(receive.data["status"], "RECEIVED")

        # Pen shortfall (40) is now a Store/Spares part with 40 in stock + a RECEIPT ledger row.
        pen = MaintenanceSpare.objects.get(company=self.company, name__iexact="Pen")
        self.assertEqual(pen.current_stock, Decimal("40.000"))
        self.assertTrue(
            SpareMovement.objects.filter(spare=pen, movement_type="RECEIPT", quantity=Decimal("40.000")).exists()
        )
        pen_item = next(i for i in receive.data["items"] if i["id"] == items[1]["id"])
        self.assertEqual(Decimal(pen_item["received_quantity"]), Decimal("40"))
        self.assertEqual(pen_item["received_spare"], pen.id)

    def test_cannot_gate_in_before_purchased(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        gate_in = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/gate-in/", {}, format="json"
        )
        self.assertEqual(gate_in.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gate_in_requires_permission(self):
        data = self._create_indent()
        indent_id = self._submit(data)
        items = data["items"]
        self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/review/",
            {"items": [{"id": items[0]["id"], "issued_quantity": "0"},
                       {"id": items[1]["id"], "issued_quantity": "0"}]},
            format="json",
        )
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/approve/", {}, format="json")
        self.client.post(f"/api/v1/maintenance/material-indents/{indent_id}/purchase/", {}, format="json")
        gate = self._reviewer("can_manage_material_indent")  # no gate-in perm
        self.client.force_authenticate(gate)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        gate_in = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/gate-in/", {}, format="json"
        )
        self.assertEqual(gate_in.status_code, status.HTTP_403_FORBIDDEN)

    # ---- Quotation round: purchaser quotes -> approver picks -> purchaser buys ----

    def _approved_indent(self):
        """An indent approved for purchase with both lines still to buy.

        submit() sends the indent straight to PENDING_APPROVAL — the store step
        is skipped — so nothing is issued and every line is a full shortfall.
        """
        data = self._create_indent()
        indent_id = self._submit(data)
        approve = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/approve/", {}, format="json"
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK, approve.data)
        return indent_id, data["items"]

    def _quote(self, indent_id, items, company_name, rates, other_charges="0.00"):
        response = self.client.post(
            "/api/v1/maintenance/material-indent-quotations/",
            {
                "indent": indent_id,
                "company_name": company_name,
                "other_charges": other_charges,
                "lines_input": [
                    {"item": items[0]["id"], "unit_price": rates[0]},
                    {"item": items[1]["id"], "unit_price": rates[1]},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def test_quotation_totals_use_shortfall_quantity(self):
        indent_id, items = self._approved_indent()
        # A4 Paper box 30 @ 10 + Pen 40 @ 5 = 300 + 200, plus 50 freight.
        quote = self._quote(indent_id, items, "Alpha Traders", ["10.00", "5.00"], "50.00")
        self.assertEqual(Decimal(quote["lines_total"]), Decimal("500.00"))
        self.assertEqual(Decimal(quote["total_amount"]), Decimal("550.00"))
        self.assertEqual(Decimal(quote["lines"][0]["quantity"]), Decimal("30.000"))

    def test_full_quotation_flow(self):
        indent_id, items = self._approved_indent()
        cheap = self._quote(indent_id, items, "Alpha Traders", ["10.00", "5.00"])
        self._quote(indent_id, items, "Beta Supplies", ["12.00", "6.00"])

        submit = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/submit-quotations/",
            {}, format="json",
        )
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        self.assertEqual(submit.data["status"], "PENDING_QUOTATION_SELECTION")
        self.assertEqual(len(submit.data["quotations"]), 2)

        select = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/select-quotation/",
            {"quotation": cheap["id"], "quotation_remarks": "Lowest total"},
            format="json",
        )
        self.assertEqual(select.status_code, status.HTTP_200_OK, select.data)
        self.assertEqual(select.data["status"], "QUOTATION_SELECTED")
        self.assertEqual(select.data["selected_company_name"], "Alpha Traders")

        purchase = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/purchase/",
            {"purchase_remarks": "PO placed"}, format="json",
        )
        self.assertEqual(purchase.status_code, status.HTTP_200_OK, purchase.data)
        self.assertEqual(purchase.data["status"], "PURCHASED")

    def test_cannot_submit_quotations_without_any(self):
        indent_id, _ = self._approved_indent()
        response = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/submit-quotations/",
            {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_purchase_blocked_while_quotations_await_selection(self):
        indent_id, items = self._approved_indent()
        self._quote(indent_id, items, "Alpha Traders", ["10.00", "5.00"])
        # Quotes exist but no company chosen yet -> the purchaser must not skip ahead.
        blocked = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/purchase/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)

    def test_purchase_without_quotations_still_allowed(self):
        """Small buys, and indents raised before this round existed."""
        indent_id, _ = self._approved_indent()
        response = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/purchase/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "PURCHASED")

    def test_return_quotations_sends_back_to_purchaser(self):
        indent_id, items = self._approved_indent()
        self._quote(indent_id, items, "Alpha Traders", ["10.00", "5.00"])
        self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/submit-quotations/",
            {}, format="json",
        )
        response = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/return-quotations/",
            {"quotation_remarks": "Get one more quote"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], "APPROVED")
        self.assertEqual(response.data["quotation_remarks"], "Get one more quote")

    def test_quotation_requires_purchase_permission(self):
        indent_id, items = self._approved_indent()
        stranger = self._reviewer("can_manage_material_indent")  # no purchase perm
        self.client.force_authenticate(stranger)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        response = self.client.post(
            "/api/v1/maintenance/material-indent-quotations/",
            {
                "indent": indent_id,
                "company_name": "Alpha Traders",
                "lines_input": [{"item": items[0]["id"], "unit_price": "10.00"}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_selection_requires_approve_permission(self):
        indent_id, items = self._approved_indent()
        quote = self._quote(indent_id, items, "Alpha Traders", ["10.00", "5.00"])
        self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/submit-quotations/",
            {}, format="json",
        )
        buyer = self._reviewer("can_purchase_material_indent")  # no approve perm
        self.client.force_authenticate(buyer)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)
        response = self.client.post(
            f"/api/v1/maintenance/material-indents/{indent_id}/select-quotation/",
            {"quotation": quote["id"]}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class DailyRegisterAPITests(APITestCase):
    """Daily Electricity + Daily Wastage registers (global, no company scope)."""

    METERS_URL = "/api/v1/maintenance/electricity-meters/"
    READINGS_URL = "/api/v1/maintenance/daily-electricity-readings/"
    WASTAGE_URL = "/api/v1/maintenance/daily-wastage-logs/"

    def _user(self, email, *codenames):
        user = get_user_model().objects.create_user(
            email=email,
            password="testpass123",
            full_name="Daily Register User",
            employee_code=email.split("@")[0].upper(),
        )
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="maintenance", codename__in=codenames
            )
        )
        return user

    def setUp(self):
        self.manager = self._user(
            "dailymgr@example.com",
            "can_manage_daily_electricity",
            "can_manage_daily_wastage",
        )
        self.viewer = self._user(
            "dailyview@example.com",
            "can_view_daily_electricity",
            "can_view_daily_wastage",
        )
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.beverages = Company.objects.create(
            name="Jivo Beverages", code="JIVO_BEVERAGES"
        )
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.client.force_authenticate(self.manager)

    def test_meter_company_tagging_and_filter(self):
        shared = self.client.post(
            self.METERS_URL,
            {
                "name": "Campus Incomer",
                "rate_per_unit": "8.5",
                "company_codes": ["JIVO_OIL", "JIVO_BEVERAGES"],
            },
            format="json",
        )
        self.assertEqual(shared.status_code, status.HTTP_201_CREATED)
        self.assertCountEqual(
            shared.data["company_codes"], ["JIVO_OIL", "JIVO_BEVERAGES"]
        )
        mart = self.client.post(
            self.METERS_URL,
            {"name": "Mart Incomer", "company_codes": ["JIVO_MART"]},
            format="json",
        )
        self.assertEqual(mart.status_code, status.HTTP_201_CREATED)
        untagged = self.client.post(
            self.METERS_URL, {"name": "Spare Feeder"}, format="json"
        )
        self.assertEqual(untagged.data["company_codes"], [])

        # A shared meter answers to either of its companies; Mart stays separate.
        for code, expected in [
            ("JIVO_OIL", ["Campus Incomer"]),
            ("JIVO_BEVERAGES", ["Campus Incomer"]),
            ("JIVO_MART", ["Mart Incomer"]),
        ]:
            listed = self.client.get(self.METERS_URL, {"company": code})
            self.assertEqual(
                [row["name"] for row in listed.data], expected, msg=code
            )

        self.assertEqual(
            len(self.client.get(self.METERS_URL).data), 3
        )  # unfiltered keeps the untagged meter

        # Retagging replaces the set rather than adding to it.
        retagged = self.client.patch(
            f"{self.METERS_URL}{shared.data['id']}/",
            {"company_codes": ["JIVO_OIL"]},
            format="json",
        )
        self.assertEqual(retagged.data["company_codes"], ["JIVO_OIL"])
        self.assertEqual(retagged.data["companies_display"], "Jivo Oil")

        # Readings inherit the meter's companies for display and filtering.
        reading = self.client.post(
            self.READINGS_URL,
            {
                "meter": mart.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "40",
            },
            format="json",
        )
        self.assertEqual(reading.status_code, status.HTTP_201_CREATED)
        self.assertEqual(reading.data["meter_companies_display"], "Jivo Mart")
        self.assertEqual(
            len(self.client.get(self.READINGS_URL, {"company": "JIVO_MART"}).data), 1
        )
        self.assertEqual(
            len(self.client.get(self.READINGS_URL, {"company": "JIVO_OIL"}).data), 0
        )

    def test_multiplying_factor_scales_the_days_units(self):
        # The dial under-reads, so the grid gives the factory an MF of 40.
        meter = self.client.post(
            self.METERS_URL,
            {"name": "HT Incomer", "rate_per_unit": "8", "multiplying_factor": "40"},
            format="json",
        )
        self.assertEqual(meter.status_code, status.HTTP_201_CREATED)
        meter_id = meter.data["id"]

        day1 = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter_id,
                "date": "2026-08-01",
                "opening_reading": "100",
                "closing_reading": "110",
            },
            format="json",
        )
        self.assertEqual(day1.status_code, status.HTTP_201_CREATED)
        # MF snapshotted from the master; 10 on the dial is 400 billed units.
        self.assertEqual(Decimal(day1.data["multiplying_factor"]), Decimal("40"))
        self.assertEqual(Decimal(day1.data["dial_difference"]), Decimal("10"))
        self.assertEqual(Decimal(day1.data["units_consumed"]), Decimal("400"))
        self.assertEqual(Decimal(day1.data["total_cost"]), Decimal("3200"))

        # A per-entry override wins over the master.
        day2 = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter_id,
                "date": "2026-08-02",
                "closing_reading": "120",
                "multiplying_factor": "20",
            },
            format="json",
        )
        self.assertEqual(day2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(day2.data["units_consumed"]), Decimal("200"))

        # Changing the master MF does not reprice the days already entered.
        self.client.patch(
            f"{self.METERS_URL}{meter_id}/", {"multiplying_factor": "1"}, format="json"
        )
        unchanged = self.client.get(f"{self.READINGS_URL}{day1.data['id']}/")
        self.assertEqual(Decimal(unchanged.data["units_consumed"]), Decimal("400"))

        # A zero MF would wipe out the consumption — refuse it.
        zero = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter_id,
                "date": "2026-08-03",
                "closing_reading": "130",
                "multiplying_factor": "0",
            },
            format="json",
        )
        self.assertEqual(zero.status_code, status.HTTP_400_BAD_REQUEST)

    def test_meter_defaults_to_a_factor_of_one(self):
        meter = self.client.post(
            self.METERS_URL, {"name": "Office Meter"}, format="json"
        )
        self.assertEqual(Decimal(meter.data["multiplying_factor"]), Decimal("1"))
        reading = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "35",
            },
            format="json",
        )
        self.assertEqual(Decimal(reading.data["units_consumed"]), Decimal("35"))

    def test_meter_and_reading_flow(self):
        meter = self.client.post(
            self.METERS_URL,
            {"name": "Main Incomer", "meter_number": "MI-01", "rate_per_unit": "8.5"},
            format="json",
        )
        self.assertEqual(meter.status_code, status.HTTP_201_CREATED)
        meter_id = meter.data["id"]

        # First reading must supply an opening (nothing to carry forward).
        missing_opening = self.client.post(
            self.READINGS_URL,
            {"meter": meter_id, "date": "2026-08-01", "closing_reading": "150"},
            format="json",
        )
        self.assertEqual(missing_opening.status_code, status.HTTP_400_BAD_REQUEST)

        day1 = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter_id,
                "date": "2026-08-01",
                "opening_reading": "100",
                "closing_reading": "150",
            },
            format="json",
        )
        self.assertEqual(day1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(day1.data["units_consumed"]), Decimal("50"))
        # Rate snapshotted from the meter master.
        self.assertEqual(Decimal(day1.data["rate_per_unit"]), Decimal("8.5"))
        self.assertEqual(Decimal(day1.data["total_cost"]), Decimal("425.00"))

        # Next day: opening auto-carried from the previous closing.
        day2 = self.client.post(
            self.READINGS_URL,
            {"meter": meter_id, "date": "2026-08-02", "closing_reading": "230"},
            format="json",
        )
        self.assertEqual(day2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(day2.data["opening_reading"]), Decimal("150"))
        self.assertEqual(Decimal(day2.data["units_consumed"]), Decimal("80"))

        duplicate = self.client.post(
            self.READINGS_URL,
            {"meter": meter_id, "date": "2026-08-02", "closing_reading": "240"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)

        backwards = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter_id,
                "date": "2026-08-03",
                "opening_reading": "230",
                "closing_reading": "200",
            },
            format="json",
        )
        self.assertEqual(backwards.status_code, status.HTTP_400_BAD_REQUEST)

        # Meter list exposes the latest closing for UI prefill.
        meters = self.client.get(self.METERS_URL)
        self.assertEqual(meters.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(meters.data[0]["last_closing_reading"]), Decimal("230"))
        self.assertEqual(meters.data[0]["last_reading_date"], "2026-08-02")

        # A meter with readings cannot be deleted.
        delete = self.client.delete(f"{self.METERS_URL}{meter_id}/")
        self.assertEqual(delete.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reading_permissions(self):
        meter = self.client.post(
            self.METERS_URL, {"name": "DG Set", "rate_per_unit": "12"}, format="json"
        )
        self.client.force_authenticate(self.viewer)
        listed = self.client.get(self.READINGS_URL)
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        created = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "10",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_403_FORBIDDEN)

    def test_reading_operator_can_add_but_not_edit_or_delete(self):
        meter = self.client.post(
            self.METERS_URL, {"name": "Boiler", "rate_per_unit": "9"}, format="json"
        )
        operator = self._user(
            "elecop@example.com",
            "can_view_daily_electricity",
            "can_add_daily_electricity",
        )
        self.client.force_authenticate(operator)

        created = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "40",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        edited = self.client.patch(
            f"{self.READINGS_URL}{created.data['id']}/",
            {"closing_reading": "45"},
            format="json",
        )
        self.assertEqual(edited.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.delete(f"{self.READINGS_URL}{created.data['id']}/").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        # No meter-master rights either, but the master stays readable for prefills.
        self.assertEqual(self.client.get(self.METERS_URL).status_code, status.HTTP_200_OK)
        denied_meter = self.client.post(
            self.METERS_URL, {"name": "Chiller", "rate_per_unit": "7"}, format="json"
        )
        self.assertEqual(denied_meter.status_code, status.HTTP_403_FORBIDDEN)

    def test_meter_manager_manages_master_but_not_readings(self):
        meter_manager = self._user(
            "elecmeter@example.com",
            "can_view_daily_electricity",
            "can_view_electricity_meter",
            "can_manage_electricity_meter",
        )
        self.client.force_authenticate(meter_manager)

        created = self.client.post(
            self.METERS_URL, {"name": "Compressor", "rate_per_unit": "10"}, format="json"
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        renamed = self.client.patch(
            f"{self.METERS_URL}{created.data['id']}/",
            {"rate_per_unit": "11"},
            format="json",
        )
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)

        reading = self.client.post(
            self.READINGS_URL,
            {
                "meter": created.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "5",
            },
            format="json",
        )
        self.assertEqual(reading.status_code, status.HTTP_403_FORBIDDEN)

    def test_legacy_manage_permission_still_grants_everything(self):
        """Existing users hold only can_manage_daily_electricity — it must keep
        covering the meter master and every reading operation."""
        meter = self.client.post(
            self.METERS_URL, {"name": "Legacy", "rate_per_unit": "6"}, format="json"
        )
        self.assertEqual(meter.status_code, status.HTTP_201_CREATED)
        reading = self.client.post(
            self.READINGS_URL,
            {
                "meter": meter.data["id"],
                "date": "2026-08-01",
                "opening_reading": "0",
                "closing_reading": "20",
            },
            format="json",
        )
        self.assertEqual(reading.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.client.patch(
                f"{self.READINGS_URL}{reading.data['id']}/",
                {"closing_reading": "25"},
                format="json",
            ).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.delete(f"{self.READINGS_URL}{reading.data['id']}/").status_code,
            status.HTTP_204_NO_CONTENT,
        )

    def test_wastage_log_crud_and_permissions(self):
        created = self.client.post(
            self.WASTAGE_URL,
            {
                "date": "2026-08-01",
                "material_name": "Damaged shrink film",
                "qty": "12.5",
                "uom": "KG",
                "reason": "Roller misalignment",
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(created.data["created_by_name"], "Daily Register User")

        other_day = self.client.post(
            self.WASTAGE_URL,
            {"date": "2026-08-02", "material_name": "Broken preforms", "qty": "300", "uom": "PCS"},
            format="json",
        )
        self.assertEqual(other_day.status_code, status.HTTP_201_CREATED)

        filtered = self.client.get(self.WASTAGE_URL, {"date": "2026-08-01"})
        self.assertEqual(len(filtered.data), 1)
        self.assertEqual(filtered.data[0]["material_name"], "Damaged shrink film")

        searched = self.client.get(self.WASTAGE_URL, {"search": "preform"})
        self.assertEqual(len(searched.data), 1)

        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(self.WASTAGE_URL).status_code, status.HTTP_200_OK)
        denied = self.client.post(
            self.WASTAGE_URL,
            {"date": "2026-08-03", "material_name": "X", "qty": "1"},
            format="json",
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)
