from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import ArrivalSlipStatus, InspectionStatus, InspectionWorkflowStatus, ParameterType
from quality_control.models import (
    MaterialArrivalSlip,
    MaterialType,
    MaterialTypeSAPItem,
    QCParameterMaster,
    QCPrintDocument,
    RawMaterialInspection,
)
from quality_control.services.rules import can_complete_gate, compute_entry_status
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Vehicle


class RawMaterialQCEntryStatusRuleTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="QC Status Co", code="QC_STATUS")
        self.user = User.objects.create_user(
            email="qc-status@example.com",
            password="password",
            full_name="QC Status User",
            employee_code="QCSTATUS001",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR55QC0001")
        self.driver = Driver.objects.create(
            name="QC Status Driver",
            mobile_no="9888888888",
            license_no="QC-STATUS-DL",
        )
        self.sequence = 0

    def _next(self):
        self.sequence += 1
        return self.sequence

    def _create_entry(self, item_count=1, status=GateEntryStatus.IN_PROGRESS):
        sequence = self._next()
        entry = VehicleEntry.objects.create(
            entry_no=f"QC-STATUS-{sequence:03d}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=status,
            created_by=self.user,
            updated_by=self.user,
        )
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number=f"PO-QC-STATUS-{sequence:03d}",
            supplier_code="SUP-QC",
            supplier_name="QC Supplier",
            created_by=self.user,
        )
        items = [
            POItemReceipt.objects.create(
                po_receipt=po_receipt,
                po_item_code=f"ITEM-QC-{sequence:03d}-{item_index}",
                item_name=f"QC Test Item {sequence}-{item_index}",
                sap_line_num=item_index,
                ordered_qty=Decimal("10.000"),
                received_qty=Decimal("10.000"),
                uom="KG",
                created_by=self.user,
            )
            for item_index in range(1, item_count + 1)
        ]
        return entry, po_receipt, items

    def _attach_slip(self, item, submitted=True):
        return MaterialArrivalSlip.objects.create(
            po_item_receipt=item,
            particulars=item.item_name,
            arrival_datetime=timezone.now(),
            weighing_required=False,
            party_name=item.po_receipt.supplier_name,
            billing_qty=Decimal("10.000"),
            billing_uom=item.uom,
            truck_no_as_per_bill=self.vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED if submitted else ArrivalSlipStatus.DRAFT,
            is_submitted=submitted,
            submitted_at=timezone.now() if submitted else None,
            submitted_by=self.user if submitted else None,
            created_by=self.user,
        )

    def _attach_inspection(
        self,
        item,
        workflow_status=InspectionWorkflowStatus.DRAFT,
        final_status=InspectionStatus.PENDING,
    ):
        slip = getattr(item, "arrival_slip", None) or self._attach_slip(item)
        sequence = self._next()
        return RawMaterialInspection.objects.create(
            arrival_slip=slip,
            report_no=f"RPT-QC-STATUS-{sequence:04d}",
            internal_lot_no=f"LOT-QC-STATUS-{sequence:04d}",
            inspection_date=timezone.localdate(),
            description_of_material=item.item_name,
            sap_code=item.po_item_code,
            supplier_name=item.po_receipt.supplier_name,
            purchase_order_no=item.po_receipt.po_number,
            vehicle_no=self.vehicle.vehicle_number,
            workflow_status=workflow_status,
            final_status=final_status,
            is_locked=final_status != InspectionStatus.PENDING,
            created_by=self.user,
        )

    def test_no_po_items_keep_current_entry_status(self):
        entry, po_receipt, _items = self._create_entry(item_count=0)
        po_receipt.delete()

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.IN_PROGRESS)

    def test_missing_arrival_slip_is_qc_pending(self):
        entry, _po_receipt, _items = self._create_entry()

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_PENDING)

    def test_submitted_slip_without_inspection_is_qc_pending(self):
        entry, _po_receipt, items = self._create_entry()
        self._attach_slip(items[0])

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_PENDING)
        self.assertFalse(can_complete_gate(items))

    def test_draft_inspection_is_qc_pending(self):
        entry, _po_receipt, items = self._create_entry()
        self._attach_inspection(items[0], InspectionWorkflowStatus.DRAFT)

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_PENDING)

    def test_submitted_inspection_is_qc_in_review(self):
        entry, _po_receipt, items = self._create_entry()
        self._attach_inspection(items[0], InspectionWorkflowStatus.SUBMITTED)

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_IN_REVIEW)

    def test_chemist_approved_inspection_is_awaiting_qam(self):
        entry, _po_receipt, items = self._create_entry()
        self._attach_inspection(items[0], InspectionWorkflowStatus.QA_CHEMIST_APPROVED)

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_AWAITING_QAM)

    def test_all_accepted_items_are_qc_completed(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        for item in items:
            self._attach_inspection(
                item,
                InspectionWorkflowStatus.QAM_APPROVED,
                InspectionStatus.ACCEPTED,
            )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_COMPLETED)
        self.assertTrue(can_complete_gate(items))

    def test_all_rejected_items_are_qc_completed_with_final_status_separate(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        for item in items:
            self._attach_inspection(
                item,
                InspectionWorkflowStatus.REJECTED,
                InspectionStatus.REJECTED,
            )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_COMPLETED)
        self.assertTrue(can_complete_gate(items))

    def test_mixed_accepted_and_rejected_terminal_items_are_qc_completed(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.ACCEPTED,
        )
        self._attach_inspection(
            items[1],
            InspectionWorkflowStatus.REJECTED,
            InspectionStatus.REJECTED,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_COMPLETED)

    def test_rejected_item_with_non_terminal_inspection_is_qc_rejected(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.REJECTED,
            InspectionStatus.REJECTED,
        )
        self._attach_inspection(items[1], InspectionWorkflowStatus.DRAFT)

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_REJECTED)

    def test_rejected_item_with_missing_slip_stays_visible_as_qc_rejected(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.REJECTED,
            InspectionStatus.REJECTED,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_REJECTED)

    def test_hold_item_is_qc_hold_and_not_gate_completable(self):
        entry, _po_receipt, items = self._create_entry()
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.HOLD,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_HOLD)
        self.assertFalse(can_complete_gate(items))

    def test_hold_item_with_accepted_item_is_qc_hold(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.ACCEPTED,
        )
        self._attach_inspection(
            items[1],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.HOLD,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_HOLD)

    def test_hold_item_with_missing_slip_stays_visible_as_qc_hold(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.HOLD,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_HOLD)

    def test_rejected_item_takes_priority_over_hold_item(self):
        entry, _po_receipt, items = self._create_entry(item_count=2)
        self._attach_inspection(
            items[0],
            InspectionWorkflowStatus.REJECTED,
            InspectionStatus.REJECTED,
        )
        self._attach_inspection(
            items[1],
            InspectionWorkflowStatus.QAM_APPROVED,
            InspectionStatus.HOLD,
        )

        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_REJECTED)


class MaterialTypeCopyParametersAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", code="TEST_CO")
        self.role = UserRole.objects.create(name="QC Manager")
        self.user = User.objects.create_user(
            email="qc@example.com",
            password="password",
            full_name="QC Manager",
            employee_code="QC001",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        permissions = Permission.objects.filter(
            content_type__app_label="quality_control",
            codename__in=["can_manage_material_types", "can_manage_qc_parameters"],
        )
        self.user.user_permissions.add(*permissions)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.source_type = MaterialType.objects.create(
            company=self.company,
            code="OLD_TYPE",
            name="Old Type",
        )
        MaterialTypeSAPItem.objects.create(
            company=self.company,
            material_type=self.source_type,
            item_code="SAP-OLD",
            item_name="Old SAP Item",
        )
        QCParameterMaster.objects.create(
            material_type=self.source_type,
            parameter_code="WEIGHT",
            parameter_name="Weight",
            standard_value="10",
            parameter_type=ParameterType.RANGE,
            min_value="9.0000",
            max_value="11.0000",
            uom="KG",
            sequence=1,
            is_mandatory=True,
        )
        QCParameterMaster.objects.create(
            material_type=self.source_type,
            parameter_code="COLOR",
            parameter_name="Color",
            standard_value="Blue",
            parameter_type=ParameterType.TEXT,
            sequence=2,
            is_mandatory=False,
        )

    def test_create_material_type_can_copy_only_parameters_from_source_material_type(self):
        response = self.client.post(
            "/api/v1/quality-control/material-types/",
            {
                "code": "NEW_TYPE",
                "name": "New Type",
                "description": "Created from old type parameters",
                "copy_parameters_from_material_type_id": self.source_type.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        target_type = MaterialType.objects.get(code="NEW_TYPE", company=self.company)

        target_parameters = list(
            QCParameterMaster.objects.filter(
                material_type=target_type,
                is_active=True,
            ).order_by("sequence")
        )
        self.assertEqual([param.parameter_code for param in target_parameters], ["WEIGHT", "COLOR"])
        self.assertEqual(target_parameters[0].parameter_name, "Weight")
        self.assertEqual(target_parameters[0].standard_value, "10")
        self.assertEqual(target_parameters[0].parameter_type, ParameterType.RANGE)
        self.assertEqual(target_parameters[0].uom, "KG")
        self.assertEqual(target_parameters[1].is_mandatory, False)
        self.assertEqual(target_type.sap_items.filter(is_active=True).count(), 0)

    def test_update_material_type_does_not_copy_parameters(self):
        target_type = MaterialType.objects.create(
            company=self.company,
            code="NEW_TYPE",
            name="New Type",
        )

        response = self.client.put(
            f"/api/v1/quality-control/material-types/{target_type.id}/",
            {
                "code": "NEW_TYPE",
                "name": "Updated New Type",
                "description": "",
                "copy_parameters_from_material_type_id": self.source_type.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(target_type.qc_parameters.filter(is_active=True).count(), 0)


class QCPrintDocumentAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", code="TEST_CO")
        self.role = UserRole.objects.create(name="QC Manager")
        self.user = User.objects.create_user(
            email="qc-docs@example.com",
            password="password",
            full_name="QC Manager",
            employee_code="QC002",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        permission = Permission.objects.get(
            content_type__app_label="quality_control",
            codename="can_manage_qc_parameters",
        )
        self.user.user_permissions.add(permission)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def test_create_or_update_print_document_by_document_key(self):
        payload = {
            "document_key": QCPrintDocument.DocumentKey.RAW_MATERIAL_INSPECTION,
            "document_id": "QC-FRM-001",
            "notes": "Inspection footer",
        }

        response = self.client.post(
            "/api/v1/quality-control/print-documents/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["document_id"], "QC-FRM-001")

        response = self.client.post(
            "/api/v1/quality-control/print-documents/",
            {**payload, "document_id": "QC-FRM-002"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["document_id"], "QC-FRM-002")
        self.assertEqual(
            QCPrintDocument.objects.filter(
                company=self.company,
                document_key=QCPrintDocument.DocumentKey.RAW_MATERIAL_INSPECTION,
            ).count(),
            1,
        )
