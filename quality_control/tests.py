from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import (
    ArrivalSlipStatus,
    InspectionDecision,
    InspectionStatus,
    InspectionWorkflowStatus,
    ParameterType,
)
from quality_control.models import (
    InspectionParameterResult,
    MaterialArrivalSlip,
    MaterialType,
    MaterialTypeSAPItem,
    QCParameterMaster,
    QCPrintDocument,
    RawMaterialInspection,
)
from quality_control.serializers import InspectionListItemSerializer
from quality_control.services.rules import can_complete_gate, compute_entry_status
from quality_control.services.spec_evaluation import (
    evaluate_within_spec,
    extract_result_number,
    parse_spec_range,
)
from quality_control.views import _resolve_material_type_for_inspection
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

    def test_inspection_list_serializer_includes_sap_material_code(self):
        _entry, _po_receipt, items = self._create_entry()
        slip = self._attach_slip(items[0])

        data = InspectionListItemSerializer(slip).data

        self.assertEqual(data["po_item_code"], items[0].po_item_code)
        self.assertEqual(data["item_name"], items[0].item_name)
        self.assertEqual(data["party_name"], items[0].po_receipt.supplier_name)

    def test_chemist_hold_is_checkpoint_not_gate_final_decision(self):
        entry, _po_receipt, items = self._create_entry()
        inspection = self._attach_inspection(items[0], InspectionWorkflowStatus.SUBMITTED)

        inspection.approve_by_chemist(
            user=self.user,
            remarks="Needs manager review",
            decision=InspectionDecision.HOLD,
        )

        inspection.refresh_from_db()
        self.assertEqual(inspection.qa_chemist_decision, InspectionDecision.HOLD)
        self.assertEqual(inspection.workflow_status, InspectionWorkflowStatus.QA_CHEMIST_APPROVED)
        self.assertEqual(inspection.final_status, InspectionStatus.PENDING)
        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_AWAITING_QAM)

        data = InspectionListItemSerializer(inspection.arrival_slip).data
        self.assertEqual(data["chemist_decision"]["decision"], InspectionDecision.HOLD)
        self.assertIsNone(data["manager_decision"]["decision"])
        self.assertIsNone(data["qc_decision"])

    def test_manager_reject_sets_manager_decision_and_terminal_final_status(self):
        entry, _po_receipt, items = self._create_entry()
        inspection = self._attach_inspection(items[0], InspectionWorkflowStatus.SUBMITTED)
        inspection.approve_by_chemist(
            user=self.user,
            remarks="Chemist approves parameters",
            decision=InspectionDecision.APPROVED,
        )
        inspection.approve_by_qam(
            user=self.user,
            remarks="Manager rejects material",
            decision=InspectionDecision.REJECTED,
        )

        inspection.refresh_from_db()
        self.assertEqual(inspection.qam_decision, InspectionDecision.REJECTED)
        self.assertEqual(inspection.final_status, InspectionStatus.REJECTED)
        self.assertEqual(inspection.workflow_status, InspectionWorkflowStatus.QAM_APPROVED)
        self.assertTrue(inspection.is_locked)
        self.assertEqual(compute_entry_status(entry), GateEntryStatus.QC_COMPLETED)

        data = InspectionListItemSerializer(inspection.arrival_slip).data
        self.assertEqual(data["manager_decision"]["decision"], InspectionDecision.REJECTED)
        self.assertEqual(data["qc_decision"], InspectionDecision.REJECTED)

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


class MaterialTypeSAPItemLinkAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company", code="TEST_CO")
        self.role = UserRole.objects.create(name="QC Chemist")
        self.user = User.objects.create_user(
            email="qc-link@example.com",
            password="password",
            full_name="QC Link User",
            employee_code="QCLINK001",
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
            codename="add_rawmaterialinspection",
        )
        self.user.user_permissions.add(permission)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.material_type = MaterialType.objects.create(
            company=self.company,
            code="PACK_LABEL",
            name="Packing Label",
        )

    def test_link_sap_item_to_material_type_from_inspection_flow(self):
        list_response = self.client.get("/api/v1/quality-control/material-types/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data[0]["id"], self.material_type.id)

        response = self.client.post(
            "/api/v1/quality-control/material-types/link-sap-item/",
            {
                "material_type_id": self.material_type.id,
                "item_code": " pm0000865 ",
                "item_name": "Label 200 ML CP Groundnut Oil Back",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        link = MaterialTypeSAPItem.objects.get(
            company=self.company,
            item_code="PM0000865",
        )
        self.assertEqual(link.material_type, self.material_type)
        self.assertEqual(link.item_name, "Label 200 ML CP Groundnut Oil Back")
        self.assertTrue(link.is_active)
        self.assertEqual(response.data["id"], self.material_type.id)
        self.assertEqual(response.data["sap_items"][0]["item_code"], "PM0000865")

    def test_material_type_can_have_multiple_sap_items(self):
        MaterialTypeSAPItem.objects.create(
            company=self.company,
            material_type=self.material_type,
            item_code="PM0000001",
            item_name="Existing Item",
        )

        response = self.client.post(
            "/api/v1/quality-control/material-types/link-sap-item/",
            {
                "material_type_id": self.material_type.id,
                "item_code": "PM0000865",
                "item_name": "New Item",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        active_codes = set(
            MaterialTypeSAPItem.objects.filter(
                company=self.company,
                material_type=self.material_type,
                is_active=True,
            ).values_list("item_code", flat=True)
        )
        self.assertEqual(active_codes, {"PM0000001", "PM0000865"})

    def test_sap_code_can_link_to_multiple_material_types(self):
        second_type = MaterialType.objects.create(
            company=self.company,
            code="PACK_CARTON",
            name="Packing Carton",
        )

        first = self.client.post(
            "/api/v1/quality-control/material-types/link-sap-item/",
            {
                "material_type_id": self.material_type.id,
                "item_code": "PM0000865",
                "item_name": "Shared Item",
            },
            format="json",
        )
        # Linking the same code to a second material type must not steal it from
        # the first — both mappings stay active.
        second = self.client.post(
            "/api/v1/quality-control/material-types/link-sap-item/",
            {
                "material_type_id": second_type.id,
                "item_code": "PM0000865",
                "item_name": "Shared Item",
            },
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        linked_type_ids = set(
            MaterialTypeSAPItem.objects.filter(
                company=self.company,
                item_code="PM0000865",
                is_active=True,
            ).values_list("material_type_id", flat=True)
        )
        self.assertEqual(linked_type_ids, {self.material_type.id, second_type.id})

    def test_by_sap_item_lists_all_linked_material_types(self):
        view_perm = Permission.objects.get(
            content_type__app_label="quality_control",
            codename="view_rawmaterialinspection",
        )
        self.user.user_permissions.add(view_perm)

        second_type = MaterialType.objects.create(
            company=self.company,
            code="PACK_CARTON",
            name="Packing Carton",
        )
        for material_type in (self.material_type, second_type):
            MaterialTypeSAPItem.objects.create(
                company=self.company,
                material_type=material_type,
                item_code="PM0000865",
                item_name="Shared Item",
            )

        response = self.client.get(
            "/api/v1/quality-control/material-types/by-sap-item/PM0000865/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = {row["id"] for row in response.data}
        self.assertEqual(returned_ids, {self.material_type.id, second_type.id})


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


class ResolveMaterialTypeForInspectionTests(TestCase):
    """Picking one material type for an inspection from a SAP code's candidates."""

    def setUp(self):
        self.company = Company.objects.create(name="Resolve Co", code="RESOLVE_CO")
        self.type_a = MaterialType.objects.create(
            company=self.company, code="TYPE_A", name="Type A",
        )
        self.type_b = MaterialType.objects.create(
            company=self.company, code="TYPE_B", name="Type B",
        )

    def _link(self, material_type, item_code="PM0000865"):
        return MaterialTypeSAPItem.objects.create(
            company=self.company,
            material_type=material_type,
            item_code=item_code,
            item_name="Shared",
        )

    def test_unlinked_code_raises(self):
        with self.assertRaises(ValidationError):
            _resolve_material_type_for_inspection(self.company, "PM0000865")

    def test_single_candidate_resolves_without_a_choice(self):
        self._link(self.type_a)

        material_type, code = _resolve_material_type_for_inspection(
            self.company, " pm0000865 "
        )

        self.assertEqual(material_type, self.type_a)
        self.assertEqual(code, "PM0000865")

    def test_multiple_candidates_require_a_choice(self):
        self._link(self.type_a)
        self._link(self.type_b)

        with self.assertRaises(ValidationError):
            _resolve_material_type_for_inspection(self.company, "PM0000865")

    def test_multiple_candidates_resolve_with_a_valid_choice(self):
        self._link(self.type_a)
        self._link(self.type_b)

        material_type, _ = _resolve_material_type_for_inspection(
            self.company, "PM0000865", material_type_id=self.type_b.id
        )

        self.assertEqual(material_type, self.type_b)

    def test_choice_must_be_a_linked_material_type(self):
        self._link(self.type_a)
        unlinked = MaterialType.objects.create(
            company=self.company, code="TYPE_C", name="Type C",
        )

        with self.assertRaises(ValidationError):
            _resolve_material_type_for_inspection(
                self.company, "PM0000865", material_type_id=unlinked.id
            )

    def test_inactive_link_is_not_a_candidate(self):
        self._link(self.type_a)
        stale = self._link(self.type_b)
        stale.is_active = False
        stale.save(update_fields=["is_active"])

        # Only the active link remains, so it resolves without needing a choice.
        material_type, _ = _resolve_material_type_for_inspection(
            self.company, "PM0000865"
        )

        self.assertEqual(material_type, self.type_a)


class SpecRangeParsingTests(TestCase):
    """Parsing QC spec text (as it is actually stored) into numeric bounds."""

    def test_tolerance_notations(self):
        cases = [
            ("235+-5.0", "230.0", "240.0"),
            ("17.00 +/- 0.15", "16.85", "17.15"),
            ("13.50 ± 0.50", "13.00", "14.00"),
            ("155±1.0", "154.0", "156.0"),
            ("750_±_2.5_%", "747.5", "752.5"),
            ("1.70 ± 0.20GM", "1.50", "1.90"),
        ]
        for std, low, high in cases:
            self.assertEqual(
                parse_spec_range(std), (Decimal(low), Decimal(high)), std
            )

    def test_comparator_one_sided_bounds(self):
        self.assertEqual(parse_spec_range("NLT 20 Kgf"), (Decimal("20"), None))
        self.assertEqual(parse_spec_range("Min 10"), (Decimal("10"), None))
        self.assertEqual(parse_spec_range("NMT 0.5"), (None, Decimal("0.5")))
        self.assertEqual(parse_spec_range("Max. 125"), (None, Decimal("125")))

    def test_dash_range(self):
        self.assertEqual(
            parse_spec_range("58.6-61.7"), (Decimal("58.6"), Decimal("61.7"))
        )
        self.assertEqual(
            parse_spec_range("1.4620-1.4640"), (Decimal("1.4620"), Decimal("1.4640"))
        )

    def test_non_numeric_specs_return_none(self):
        for std in ["Free from all visual defects", "-", "100", "", "Blue"]:
            self.assertIsNone(parse_spec_range(std), std)

    def test_keyword_inside_a_word_is_not_a_bound(self):
        # "min" inside "aluminium" must not be read as a Min bound.
        self.assertIsNone(parse_spec_range("Aluminium 5 micron foil"))


class ExtractResultNumberTests(TestCase):
    """Pulling the measured number out of free-text result entries."""

    def test_plain_and_labelled_numbers(self):
        self.assertEqual(extract_result_number("230"), Decimal("230"))
        self.assertEqual(extract_result_number("AVG-233"), Decimal("233"))
        self.assertEqual(extract_result_number("Avg-8.12"), Decimal("8.12"))
        self.assertEqual(extract_result_number("127/128"), Decimal("127"))

    def test_non_numeric_returns_none(self):
        for rv in ["PASS", "OK", "", None]:
            self.assertIsNone(extract_result_number(rv))


class EvaluateWithinSpecTests(TestCase):
    """End-to-end judgement of a reading against a spec."""

    def test_text_tolerance_spec_with_text_reading(self):
        self.assertTrue(evaluate_within_spec("235+-5.0", None, None, None, "230"))
        self.assertTrue(evaluate_within_spec("235+-5.0", None, None, None, "240"))
        self.assertFalse(evaluate_within_spec("235+-5.0", None, None, None, "244"))
        self.assertFalse(evaluate_within_spec("235+-5.0", None, None, None, "229"))

    def test_comparator_bounds(self):
        self.assertTrue(evaluate_within_spec("NLT 20", None, None, None, "25"))
        self.assertFalse(evaluate_within_spec("NLT 20", None, None, None, "19"))
        self.assertTrue(evaluate_within_spec("NMT 0.5", None, None, None, "0.4"))
        self.assertFalse(evaluate_within_spec("NMT 0.5", None, None, None, "0.6"))

    def test_structured_min_max_take_precedence(self):
        self.assertTrue(
            evaluate_within_spec("100", Decimal("80"), Decimal("120"), None, "100")
        )
        self.assertFalse(
            evaluate_within_spec("100", Decimal("80"), Decimal("120"), None, "130")
        )

    def test_result_numeric_is_preferred_over_text(self):
        self.assertTrue(
            evaluate_within_spec("235+-5.0", None, None, Decimal("236"), "999")
        )

    def test_non_numeric_spec_cannot_be_judged(self):
        self.assertIsNone(
            evaluate_within_spec("Free from defects", None, None, None, "PASS")
        )
        # A numeric reading against a descriptive spec still can't be judged.
        self.assertIsNone(
            evaluate_within_spec("Free from defects", None, None, None, "12.5")
        )

    def test_missing_reading_cannot_be_judged(self):
        self.assertIsNone(evaluate_within_spec("235+-5.0", None, None, None, ""))


class InspectionParameterResultAutoSpecTests(TestCase):
    """The model saves an auto-derived is_within_spec from the spec range."""

    def setUp(self):
        self.company = Company.objects.create(name="AutoSpec Co", code="AUTOSPEC")
        self.material_type = MaterialType.objects.create(
            company=self.company, code="MT", name="MT",
        )
        self.inspection = RawMaterialInspection.objects.create(
            report_no="RPT-AUTOSPEC-1",
            internal_lot_no="LOT-AUTOSPEC-1",
            inspection_date=timezone.localdate(),
            description_of_material="Test material",
            supplier_name="Supplier",
        )

    def _param(self, std, code, ptype=ParameterType.TEXT):
        return QCParameterMaster.objects.create(
            material_type=self.material_type,
            parameter_name="Length",
            parameter_code=code,
            standard_value=std,
            parameter_type=ptype,
        )

    def test_numeric_reading_against_text_tolerance_is_auto_evaluated(self):
        param = self._param("235+-5.0", code="P1")
        result = InspectionParameterResult.objects.create(
            inspection=self.inspection,
            parameter_master=param,
            result_value="230",
            is_within_spec=None,
        )
        result.refresh_from_db()
        self.assertTrue(result.is_within_spec)

    def test_out_of_tolerance_reading_overrides_stale_manual_flag(self):
        param = self._param("235+-5.0", code="P2")
        result = InspectionParameterResult.objects.create(
            inspection=self.inspection,
            parameter_master=param,
            result_value="250",
            is_within_spec=True,  # stale/incorrect manual value
        )
        result.refresh_from_db()
        self.assertFalse(result.is_within_spec)

    def test_visual_spec_keeps_the_manual_flag(self):
        param = self._param(
            "Free from all visual defects", code="P3", ptype=ParameterType.BOOLEAN
        )
        result = InspectionParameterResult.objects.create(
            inspection=self.inspection,
            parameter_master=param,
            result_value="PASS",
            is_within_spec=True,
        )
        result.refresh_from_db()
        self.assertTrue(result.is_within_spec)


class InspectionSubmitRemarkGateTests(APITestCase):
    """A remark is required to submit an inspection that has an out-of-spec parameter."""

    def setUp(self):
        self.company = Company.objects.create(name="Submit Co", code="SUBMIT_CO")
        self.role = UserRole.objects.create(name="QA Chemist")
        self.user = User.objects.create_user(
            email="qc-submit@example.com",
            password="password",
            full_name="QC Submit User",
            employee_code="QCSUB001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role,
            is_default=True, is_active=True,
        )
        self.user.user_permissions.add(Permission.objects.get(
            content_type__app_label="quality_control",
            codename="can_submit_inspection",
        ))
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.material_type = MaterialType.objects.create(
            company=self.company, code="MT", name="MT",
        )
        vehicle = Vehicle.objects.create(vehicle_number="HR55SUB0001")
        driver = Driver.objects.create(
            name="Driver", mobile_no="9800000000", license_no="SUB-DL",
        )
        entry = VehicleEntry.objects.create(
            entry_no="SUBMIT-001", company=self.company, vehicle=vehicle, driver=driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.IN_PROGRESS,
            created_by=self.user, updated_by=self.user,
        )
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry, po_number="PO-SUBMIT-001",
            supplier_code="SUP", supplier_name="Supplier", created_by=self.user,
        )
        item = POItemReceipt.objects.create(
            po_receipt=po_receipt, po_item_code="ITEM-SUB-1", item_name="Item",
            sap_line_num=1, ordered_qty=Decimal("10.000"), received_qty=Decimal("10.000"),
            uom="KG", created_by=self.user,
        )
        slip = MaterialArrivalSlip.objects.create(
            po_item_receipt=item, particulars="Item", arrival_datetime=timezone.now(),
            weighing_required=False, party_name="Supplier",
            billing_qty=Decimal("10.000"), billing_uom="KG",
            truck_no_as_per_bill=vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED, is_submitted=True,
            submitted_at=timezone.now(), submitted_by=self.user, created_by=self.user,
        )
        self.inspection = RawMaterialInspection.objects.create(
            arrival_slip=slip, report_no="RPT-SUBMIT-1", internal_lot_no="LOT-SUBMIT-1",
            inspection_date=timezone.localdate(), description_of_material="Item",
            supplier_name="Supplier", material_type=self.material_type,
            workflow_status=InspectionWorkflowStatus.DRAFT,
            final_status=InspectionStatus.PENDING, created_by=self.user,
        )
        self.param = QCParameterMaster.objects.create(
            material_type=self.material_type, parameter_name="Length",
            parameter_code="P1", standard_value="235+-5.0",
        )

    def _add_result(self, result_value, is_within_spec):
        return InspectionParameterResult.objects.create(
            inspection=self.inspection, parameter_master=self.param,
            result_value=result_value, is_within_spec=is_within_spec,
        )

    def _submit(self, **body):
        return self.client.post(
            f"/api/v1/quality-control/inspections/{self.inspection.id}/submit/",
            body, format="json",
        )

    def test_submit_blocked_when_out_of_spec_and_no_remark(self):
        self._add_result("250", is_within_spec=False)  # 250 vs 235±5 -> out

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("remarks", response.data)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.workflow_status, InspectionWorkflowStatus.DRAFT)

    def test_submit_allowed_when_out_of_spec_with_inline_remark(self):
        self._add_result("250", is_within_spec=False)

        response = self._submit(remarks="Reading high; supplier notified.")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.workflow_status, InspectionWorkflowStatus.SUBMITTED)
        self.assertEqual(self.inspection.remarks, "Reading high; supplier notified.")

    def test_submit_allowed_within_spec_without_remark(self):
        self._add_result("236", is_within_spec=True)  # within 235±5

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.workflow_status, InspectionWorkflowStatus.SUBMITTED)
