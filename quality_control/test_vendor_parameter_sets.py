"""Which vendor's parameters an inspection is judged against.

The vendor comes from the PO rather than being picked by hand, a vendor with no
set of their own falls back to the material type's default, and inspecting
against someone else's parameters needs both the override permission and a
reason on the record.
"""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import ArrivalSlipStatus, ParameterType
from quality_control.models import (
    MaterialArrivalSlip,
    MaterialType,
    MaterialTypeSAPItem,
    QCParameterMaster,
    QCParameterSet,
    RawMaterialInspection,
)
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Vehicle


class VendorParameterSetTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Vendor Co", code="VEND_CO")
        self.role = UserRole.objects.create(name="QA")
        self.user = User.objects.create_user(
            email="qc-vendor@example.com",
            password="password",
            full_name="QC Vendor User",
            employee_code="QCVEN001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role,
            is_default=True, is_active=True,
        )
        self._grant(
            "add_rawmaterialinspection",
            "change_rawmaterialinspection",
            "view_rawmaterialinspection",
            "can_manage_qc_parameters",
        )
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.material_type = MaterialType.objects.create(
            company=self.company, code="PET", name="PET Bottle",
        )
        MaterialTypeSAPItem.objects.create(
            company=self.company, material_type=self.material_type,
            item_code="ITEM-VEN-1", item_name="PET Bottle 500ml",
        )
        self.default_set = QCParameterSet.objects.create(
            material_type=self.material_type, vendor_code="",
        )
        QCParameterMaster.objects.create(
            parameter_set=self.default_set, parameter_code="WT",
            parameter_name="Weight", standard_value="20+-1.0",
            parameter_type=ParameterType.RANGE, sequence=1,
        )

    def _grant(self, *codenames):
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="quality_control",
            codename__in=codenames,
        ))
        # Re-fetch so the cached permission set is rebuilt.
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_authenticate(self.user)

    def _slip(self, supplier_code, suffix):
        vehicle = Vehicle.objects.create(vehicle_number=f"HR55VEN{suffix}")
        driver = Driver.objects.create(
            name="Driver", mobile_no=f"98000000{suffix}", license_no=f"VEN-DL-{suffix}",
        )
        entry = VehicleEntry.objects.create(
            entry_no=f"VENDOR-{suffix}", company=self.company, vehicle=vehicle,
            driver=driver, entry_type="RAW_MATERIAL",
            status=GateEntryStatus.IN_PROGRESS,
            created_by=self.user, updated_by=self.user,
        )
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry, po_number=f"PO-VEN-{suffix}",
            supplier_code=supplier_code, supplier_name=f"Supplier {supplier_code}",
            created_by=self.user,
        )
        item = POItemReceipt.objects.create(
            po_receipt=po_receipt, po_item_code="ITEM-VEN-1", item_name="PET Bottle",
            sap_line_num=1, ordered_qty=Decimal("10.000"),
            received_qty=Decimal("10.000"), uom="KG", created_by=self.user,
        )
        return MaterialArrivalSlip.objects.create(
            po_item_receipt=item, particulars="Item", arrival_datetime=timezone.now(),
            weighing_required=False, party_name="Supplier",
            billing_qty=Decimal("10.000"), billing_uom="KG",
            truck_no_as_per_bill=vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED, is_submitted=True,
            submitted_at=timezone.now(), submitted_by=self.user, created_by=self.user,
        )

    def _create_inspection(self, slip, suffix, **extra):
        return self.client.post(
            f"/api/v1/quality-control/arrival-slips/{slip.id}/inspection/",
            {
                "report_no": f"RPT-VEN-{suffix}",
                "internal_lot_no": f"LOT-VEN-{suffix}",
                "inspection_date": str(timezone.localdate()),
                "description_of_material": "PET Bottle",
                "supplier_name": "Supplier",
                "sap_code": "ITEM-VEN-1",
                **extra,
            },
            format="json",
        )

    def _vendor_set(self, vendor_code, standard_value="25+-0.5"):
        vendor_set = QCParameterSet.objects.create(
            material_type=self.material_type,
            vendor_code=vendor_code,
            vendor_name=f"Supplier {vendor_code}",
        )
        QCParameterMaster.objects.create(
            parameter_set=vendor_set, parameter_code="WT",
            parameter_name="Weight", standard_value=standard_value,
            parameter_type=ParameterType.RANGE, sequence=1,
        )
        return vendor_set

    # ---- resolution ----

    def test_vendor_with_own_set_is_judged_on_it(self):
        vendor_set = self._vendor_set("V001")
        slip = self._slip("V001", "01")

        response = self._create_inspection(slip, "01")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)
        self.assertEqual(inspection.parameter_set_id, vendor_set.id)
        self.assertEqual(inspection.vendor_code, "V001")
        result = inspection.parameter_results.get(is_active=True)
        self.assertEqual(result.standard_value, "25+-0.5")

    def test_vendor_without_a_set_falls_back_to_the_default(self):
        self._vendor_set("V001")
        slip = self._slip("V002", "02")

        response = self._create_inspection(slip, "02")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)
        self.assertEqual(inspection.parameter_set_id, self.default_set.id)
        self.assertEqual(inspection.vendor_code, "V002")
        result = inspection.parameter_results.get(is_active=True)
        self.assertEqual(result.standard_value, "20+-1.0")

    def test_material_type_with_no_parameters_is_rejected(self):
        self.default_set.delete()
        slip = self._slip("V003", "03")

        response = self._create_inspection(slip, "03")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("parameter_set", response.data)

    # ---- override ----

    def test_vendor_sent_without_the_override_permission_is_refused(self):
        self._vendor_set("V001", standard_value="99+-0.1")
        slip = self._slip("V002", "04")

        response = self._create_inspection(slip, "04", vendor_code="V001")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(RawMaterialInspection.objects.filter(arrival_slip=slip).exists())

    def test_override_needs_a_reason(self):
        self._grant("can_override_qc_vendor")
        self._vendor_set("V001")
        slip = self._slip("V002", "05")

        response = self._create_inspection(slip, "05", vendor_code="V001")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vendor_override_reason", response.data)

    def test_override_with_permission_and_reason_switches_the_set(self):
        self._grant("can_override_qc_vendor")
        vendor_set = self._vendor_set("V001")
        slip = self._slip("V002", "06")

        response = self._create_inspection(
            slip, "06",
            vendor_code="V001",
            vendor_override_reason="Bought through a trader; goods made by V001.",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)
        self.assertEqual(inspection.parameter_set_id, vendor_set.id)
        self.assertTrue(inspection.is_vendor_overridden)
        self.assertEqual(inspection.po_vendor_code, "V002")

    # ---- snapshot ----

    def test_editing_a_spec_does_not_change_an_existing_result(self):
        slip = self._slip("V002", "07")
        self._create_inspection(slip, "07")
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)
        result = inspection.parameter_results.get(is_active=True)

        parameter = self.default_set.parameters.get(parameter_code="WT")
        parameter.standard_value = "40+-2.0"
        parameter.save()

        result.refresh_from_db()
        self.assertEqual(result.standard_value, "20+-1.0")

    # ---- master data API ----

    def test_default_set_cannot_be_deleted(self):
        response = self.client.delete(
            f"/api/v1/quality-control/parameter-sets/{self.default_set.id}/"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.default_set.refresh_from_db()
        self.assertTrue(self.default_set.is_active)

    def test_creating_a_vendor_set_can_seed_from_the_default(self):
        response = self.client.post(
            f"/api/v1/quality-control/material-types/{self.material_type.id}/parameter-sets/",
            {
                "vendor_code": "v009",
                "vendor_name": "Supplier V009",
                "copy_parameters_from_set_id": self.default_set.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["vendor_code"], "V009")  # normalized
        self.assertEqual(response.data["parameter_count"], 1)
        vendor_set = QCParameterSet.objects.get(
            material_type=self.material_type, vendor_code="V009"
        )
        self.assertEqual(
            vendor_set.parameters.get(parameter_code="WT").standard_value, "20+-1.0"
        )

    def test_a_vendor_cannot_have_two_sets_for_one_material_type(self):
        self._vendor_set("V001")

        response = self.client.post(
            f"/api/v1/quality-control/material-types/{self.material_type.id}/parameter-sets/",
            {"vendor_code": "V001", "vendor_name": "Supplier V001"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vendor_code", response.data)

    def test_material_type_parameter_route_still_reads_the_default_set(self):
        self._vendor_set("V001", standard_value="99+-0.1")

        response = self.client.get(
            f"/api/v1/quality-control/material-types/{self.material_type.id}/parameters/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["standard_value"] for row in response.data], ["20+-1.0"])

    # ---- backfill migration ----

    def test_backfill_snapshots_definitions_onto_older_result_rows(self):
        """0038's snapshot pass fills rows written before the columns existed.

        The full migration chain can't be replayed on sqlite (several
        pre-existing migrations are raw Postgres SQL), so the backfill
        functions are exercised directly against rows shaped like the old ones.
        """
        from django.apps import apps

        _0038 = __import__(
            "quality_control.migrations.0038_backfill_default_parameter_sets",
            fromlist=["snapshot_result_definitions"],
        )

        slip = self._slip("V002", "08")
        self._create_inspection(slip, "08")
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)
        result = inspection.parameter_results.get(is_active=True)

        # Reproduce a pre-0038 row: linked to its master, but with no snapshot.
        type(result).objects.filter(pk=result.pk).update(
            parameter_code="", uom="", sequence=0, min_value=None, max_value=None
        )

        _0038.snapshot_result_definitions(apps, None)

        result.refresh_from_db()
        self.assertEqual(result.parameter_code, "WT")
        self.assertEqual(result.sequence, 1)

    def test_backfill_links_older_inspections_to_the_default_set(self):
        from django.apps import apps

        _0038 = __import__(
            "quality_control.migrations.0038_backfill_default_parameter_sets",
            fromlist=["link_inspections_to_default_sets"],
        )

        slip = self._slip("V002", "09")
        self._create_inspection(slip, "09")
        inspection = RawMaterialInspection.objects.get(arrival_slip=slip)

        # Reproduce a pre-0038 inspection: material type known, vendor unknown.
        RawMaterialInspection.objects.filter(pk=inspection.pk).update(
            parameter_set=None, vendor_code="", vendor_name=""
        )

        _0038.link_inspections_to_default_sets(apps, None)

        inspection.refresh_from_db()
        self.assertEqual(inspection.parameter_set_id, self.default_set.id)
        self.assertEqual(inspection.vendor_code, "V002")
        self.assertEqual(inspection.vendor_name, "Supplier V002")
