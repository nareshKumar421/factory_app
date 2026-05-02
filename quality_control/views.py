# quality_control/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from django.shortcuts import get_object_or_404
from django.db import IntegrityError
from django.db.models import Q, Count

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from raw_material_gatein.models import POItemReceipt
from gate_core.enums import GateEntryStatus

from .models import (
    MaterialType,
    QCParameterMaster,
    MaterialArrivalSlip,
    RawMaterialInspection,
    InspectionParameterResult,
    ArrivalSlipAttachment,
    AttachmentType,
)
from .serializers import (
    MaterialTypeSerializer,
    MaterialTypeCreateSerializer,
    QCParameterMasterSerializer,
    QCParameterMasterCreateSerializer,
    MaterialArrivalSlipSerializer,
    MaterialArrivalSlipCreateSerializer,
    RawMaterialInspectionSerializer,
    RawMaterialInspectionCreateSerializer,
    InspectionParameterResultSerializer,
    InspectionListItemSerializer,
    ApprovalSerializer,
    FactoryHeadDecisionSerializer,
    ParameterResultBulkUpdateSerializer,
)
from .permissions import (
    CanManageArrivalSlip,
    CanSubmitArrivalSlip,
    CanSendBackArrivalSlip,
    CanViewArrivalSlip,
    CanManageInspection,
    CanSubmitInspection,
    CanViewInspection,
    CanApproveAsChemist,
    CanApproveAsQAM,
    CanRejectInspection,
    CanFactoryHeadDecision,
    CanManageMaterialTypes,
    CanManageQCParameters,
)
from .enums import (
    ArrivalSlipStatus,
    FactoryHeadDecision,
    InspectionStatus,
    InspectionWorkflowStatus,
)
from .services.rules import update_entry_status


# ==================== Material Type APIs ====================

class MaterialTypeListCreateAPI(APIView):
    """List and create material types"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageMaterialTypes]

    def get(self, request):
        material_types = MaterialType.objects.filter(
            company=request.company.company,
            is_active=True
        )
        serializer = MaterialTypeSerializer(material_types, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = MaterialTypeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Check if a soft-deleted material type with the same code exists
        existing = MaterialType.objects.filter(
            company=request.company.company,
            code=serializer.validated_data.get("code"),
            is_active=False,
        ).first()

        if existing:
            for key, value in serializer.validated_data.items():
                setattr(existing, key, value)
            existing.is_active = True
            existing.updated_by = request.user
            existing.save()
            material_type = existing
        else:
            material_type = MaterialType.objects.create(
                company=request.company.company,
                created_by=request.user,
                **serializer.validated_data
            )
        return Response(
            MaterialTypeSerializer(material_type).data,
            status=status.HTTP_201_CREATED
        )


class MaterialTypeDetailAPI(APIView):
    """Get, update, delete material type"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageMaterialTypes]

    def get(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        serializer = MaterialTypeSerializer(material_type)
        return Response(serializer.data)

    def put(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        serializer = MaterialTypeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        for key, value in serializer.validated_data.items():
            setattr(material_type, key, value)
        material_type.updated_by = request.user
        material_type.save()

        return Response(MaterialTypeSerializer(material_type).data)

    def delete(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        material_type.is_active = False
        material_type.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================== QC Parameter Master APIs ====================

class QCParameterListCreateAPI(APIView):
    """List and create QC parameters for a material type"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageQCParameters]

    def get(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        parameters = QCParameterMaster.objects.filter(
            material_type=material_type,
            is_active=True
        )
        serializer = QCParameterMasterSerializer(parameters, many=True)
        return Response(serializer.data)

    def post(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        serializer = QCParameterMasterCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Check if a soft-deleted parameter with the same code exists
        existing = QCParameterMaster.objects.filter(
            material_type=material_type,
            parameter_code=serializer.validated_data.get("parameter_code"),
            is_active=False,
        ).first()

        if existing:
            for key, value in serializer.validated_data.items():
                setattr(existing, key, value)
            existing.is_active = True
            existing.updated_by = request.user
            existing.save()
            parameter = existing
        else:
            parameter = QCParameterMaster.objects.create(
                material_type=material_type,
                created_by=request.user,
                **serializer.validated_data
            )
        return Response(
            QCParameterMasterSerializer(parameter).data,
            status=status.HTTP_201_CREATED
        )


class QCParameterDetailAPI(APIView):
    """Get, update, delete QC parameter"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageQCParameters]

    def get(self, request, parameter_id):
        parameter = get_object_or_404(
            QCParameterMaster,
            id=parameter_id,
            material_type__company=request.company.company
        )
        serializer = QCParameterMasterSerializer(parameter)
        return Response(serializer.data)

    def put(self, request, parameter_id):
        parameter = get_object_or_404(
            QCParameterMaster,
            id=parameter_id,
            material_type__company=request.company.company
        )
        serializer = QCParameterMasterCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        for key, value in serializer.validated_data.items():
            setattr(parameter, key, value)
        parameter.updated_by = request.user
        parameter.save()

        return Response(QCParameterMasterSerializer(parameter).data)

    def delete(self, request, parameter_id):
        parameter = get_object_or_404(
            QCParameterMaster,
            id=parameter_id,
            material_type__company=request.company.company
        )
        parameter.is_active = False
        parameter.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================== Material Arrival Slip APIs ====================

class ArrivalSlipListAPI(APIView):
    """List all arrival slips for a company"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArrivalSlip]

    def get(self, request):
        slips = MaterialArrivalSlip.objects.filter(
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        ).select_related(
            "po_item_receipt", "po_item_receipt__po_receipt",
            "po_item_receipt__po_receipt__vehicle_entry"
        )

        # Filter by status if provided
        status_filter = request.query_params.get("status")
        if status_filter:
            slips = slips.filter(status=status_filter)

        serializer = MaterialArrivalSlipSerializer(slips, many=True, context={'request': request})
        return Response(serializer.data)


class ArrivalSlipCreateUpdateAPI(APIView):
    """Create or update arrival slip for a PO item"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageArrivalSlip]

    def get(self, request, po_item_id):
        po_item = get_object_or_404(
            POItemReceipt,
            id=po_item_id,
            po_receipt__vehicle_entry__company=request.company.company
        )
        try:
            slip = po_item.arrival_slip
            serializer = MaterialArrivalSlipSerializer(slip, context={'request': request})
            return Response(serializer.data)
        except MaterialArrivalSlip.DoesNotExist:
            return Response(
                {"detail": "Arrival slip not found"},
                status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, po_item_id):
        po_item = get_object_or_404(
            POItemReceipt,
            id=po_item_id,
            po_receipt__vehicle_entry__company=request.company.company
        )

        serializer = MaterialArrivalSlipCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        slip, created = MaterialArrivalSlip.objects.get_or_create(
            po_item_receipt=po_item,
            defaults={
                "created_by": request.user,
                **serializer.validated_data
            }
        )

        if not created:
            # Check if already submitted and not rejected
            if slip.is_submitted and slip.status != ArrivalSlipStatus.REJECTED:
                return Response(
                    {"detail": "Arrival slip already submitted"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Update existing slip
            for key, value in serializer.validated_data.items():
                setattr(slip, key, value)
            slip.updated_by = request.user

            # If was rejected, reset to draft
            if slip.status == ArrivalSlipStatus.REJECTED:
                slip.status = ArrivalSlipStatus.DRAFT
                slip.is_submitted = False

            slip.save()

        return Response(
            MaterialArrivalSlipSerializer(slip, context={'request': request}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )


class ArrivalSlipDetailAPI(APIView):
    """Get arrival slip by ID"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArrivalSlip]

    def get(self, request, slip_id):
        slip = get_object_or_404(
            MaterialArrivalSlip,
            id=slip_id,
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )
        serializer = MaterialArrivalSlipSerializer(slip, context={'request': request})
        return Response(serializer.data)


class ArrivalSlipSubmitAPI(APIView):
    """Submit arrival slip to QA.

    Accepts optional file attachments via multipart/form-data:
    - certificate_of_analysis: file (required if has_certificate_of_analysis is true on the slip)
    - certificate_of_quantity: file (required if has_certificate_of_quantity is true on the slip)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitArrivalSlip]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, slip_id):
        slip = get_object_or_404(
            MaterialArrivalSlip,
            id=slip_id,
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if slip.is_submitted and slip.status == ArrivalSlipStatus.SUBMITTED:
            return Response(
                {"detail": "Already submitted"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get optional file attachments
        coa_file = request.FILES.get("certificate_of_analysis")
        coq_file = request.FILES.get("certificate_of_quantity")

        # Check for existing attachments (e.g. resubmission after send-back)
        has_existing_coa = slip.attachments.filter(
            attachment_type=AttachmentType.CERTIFICATE_OF_ANALYSIS
        ).exists()
        has_existing_coq = slip.attachments.filter(
            attachment_type=AttachmentType.CERTIFICATE_OF_QUANTITY
        ).exists()

        # Validate: if has_certificate_of_analysis is True, COA file is required
        # (unless an attachment already exists from a previous submission)
        if slip.has_certificate_of_analysis and not coa_file and not has_existing_coa:
            return Response(
                {"detail": "Certificate of Analysis attachment is required when has_certificate_of_analysis is true."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate: if has_certificate_of_quantity is True, COQ file is required
        # (unless an attachment already exists from a previous submission)
        if slip.has_certificate_of_quantity and not coq_file and not has_existing_coq:
            return Response(
                {"detail": "Certificate of Quantity attachment is required when has_certificate_of_quantity is true."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Save attachments (update_or_create to handle resubmission after rejection)
        if coa_file:
            ArrivalSlipAttachment.objects.update_or_create(
                arrival_slip=slip,
                attachment_type=AttachmentType.CERTIFICATE_OF_ANALYSIS,
                defaults={"file": coa_file},
            )

        if coq_file:
            ArrivalSlipAttachment.objects.update_or_create(
                arrival_slip=slip,
                attachment_type=AttachmentType.CERTIFICATE_OF_QUANTITY,
                defaults={"file": coq_file},
            )

        slip.submit_to_qa(request.user)

        # Update vehicle entry status
        entry = slip.po_item_receipt.po_receipt.vehicle_entry
        if entry.status in [GateEntryStatus.IN_PROGRESS, GateEntryStatus.ARRIVAL_SLIP_REJECTED]:
            entry.status = GateEntryStatus.ARRIVAL_SLIP_SUBMITTED
            entry.save(update_fields=["status"])

        return Response(
            MaterialArrivalSlipSerializer(slip, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


class ArrivalSlipSendBackAPI(APIView):
    """Send arrival slip back to gate for correction.

    Allowed when the slip is SUBMITTED and the inspection either doesn't exist
    or is still in DRAFT (not yet submitted to chemist).
    If a draft inspection exists, it is soft-deleted (is_active=False).
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendBackArrivalSlip]

    def post(self, request, slip_id):
        slip = get_object_or_404(
            MaterialArrivalSlip,
            id=slip_id,
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if slip.status != ArrivalSlipStatus.SUBMITTED:
            return Response(
                {"detail": "Only submitted arrival slips can be sent back"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Block if inspection has already been submitted to chemist
        if hasattr(slip, "inspection"):
            inspection = slip.inspection
            if inspection.workflow_status != InspectionWorkflowStatus.DRAFT:
                return Response(
                    {"detail": "Cannot send back — inspection has already been submitted. Use inspection rejection instead."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Soft-delete the draft inspection
            inspection.cancel_for_send_back(user=request.user, remarks=request.data.get("remarks", ""))

        remarks = request.data.get("remarks", "")
        slip.send_back_to_gate(user=request.user, remarks=remarks)

        # Update vehicle entry status
        entry = slip.po_item_receipt.po_receipt.vehicle_entry
        entry.status = GateEntryStatus.ARRIVAL_SLIP_REJECTED
        entry.save(update_fields=["status"])

        return Response(
            MaterialArrivalSlipSerializer(slip, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


# ==================== Raw Material Inspection APIs ====================

class InspectionCreateUpdateAPI(APIView):
    """Create or update inspection for an arrival slip"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageInspection]

    def get(self, request, slip_id):
        slip = get_object_or_404(
            MaterialArrivalSlip,
            id=slip_id,
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )
        try:
            inspection = slip.inspection
            serializer = RawMaterialInspectionSerializer(inspection, context={'request': request})
            return Response(serializer.data)
        except RawMaterialInspection.DoesNotExist:
            return Response(
                {"detail": "Inspection not found"},
                status=status.HTTP_404_NOT_FOUND
            )

    def post(self, request, slip_id):
        slip = get_object_or_404(
            MaterialArrivalSlip,
            id=slip_id,
            po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        # Check if arrival slip is submitted
        if slip.status != ArrivalSlipStatus.SUBMITTED:
            return Response(
                {"detail": "Arrival slip must be submitted first"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = RawMaterialInspectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        material_type_id = data.pop("material_type_id", None)
        material_type = None
        if material_type_id:
            material_type = get_object_or_404(
                MaterialType,
                id=material_type_id,
                company=request.company.company
            )

        internal_lot_no = data.pop("internal_lot_no", None) or RawMaterialInspection.generate_lot_no()

        try:
            inspection, created = RawMaterialInspection.objects.get_or_create(
                arrival_slip=slip,
                defaults={
                    "report_no": RawMaterialInspection.generate_report_no(),
                    "internal_lot_no": internal_lot_no,
                    "material_type": material_type,
                    "created_by": request.user,
                    **data
                }
            )

            if not created:
                if inspection.is_locked:
                    return Response(
                        {"detail": "Inspection is locked"},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                for key, value in data.items():
                    setattr(inspection, key, value)
                if material_type:
                    inspection.material_type = material_type
                # Update internal_lot_no if provided in request
                if internal_lot_no and internal_lot_no != inspection.internal_lot_no:
                    inspection.internal_lot_no = internal_lot_no
                inspection.updated_by = request.user
                inspection.save()
        except IntegrityError:
            return Response(
                {"internal_lot_no": [f"Internal lot number '{internal_lot_no}' already exists."]},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create parameter results if material type has parameters
        if material_type and created:
            for param in material_type.qc_parameters.filter(is_active=True):
                InspectionParameterResult.objects.get_or_create(
                    inspection=inspection,
                    parameter_master=param,
                    defaults={
                        "parameter_name": param.parameter_name,
                        "standard_value": param.standard_value,
                        "created_by": request.user,
                    }
                )

        # Update vehicle entry status based on overall QC progress
        entry = slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )


class InspectionDetailAPI(APIView):
    """Get inspection by ID"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )
        serializer = RawMaterialInspectionSerializer(inspection, context={'request': request})
        return Response(serializer.data)


class InspectionParameterResultsAPI(APIView):
    """Update parameter results for an inspection"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageInspection]

    def get(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )
        results = inspection.parameter_results.all()
        serializer = InspectionParameterResultSerializer(results, many=True)
        return Response(serializer.data)

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.is_locked:
            return Response(
                {"detail": "Inspection is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ParameterResultBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        results_data = serializer.validated_data.get("results", [])
        updated_results = []

        for result_data in results_data:
            param_id = result_data.pop("parameter_master_id")
            result, _ = InspectionParameterResult.objects.get_or_create(
                inspection=inspection,
                parameter_master_id=param_id,
                defaults={"created_by": request.user}
            )

            for key, value in result_data.items():
                setattr(result, key, value)
            result.updated_by = request.user
            result.save()
            updated_results.append(result)

        return Response(
            InspectionParameterResultSerializer(updated_results, many=True).data,
            status=status.HTTP_200_OK
        )


class InspectionSubmitAPI(APIView):
    """Submit inspection for QA Chemist approval"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitInspection]

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.is_locked:
            return Response(
                {"detail": "Inspection is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if inspection.workflow_status != InspectionWorkflowStatus.DRAFT:
            return Response(
                {"detail": "Already submitted"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check mandatory parameters
        mandatory_params = inspection.parameter_results.filter(
            parameter_master__is_mandatory=True,
            result_value=""
        )
        if mandatory_params.exists():
            return Response(
                {"detail": "All mandatory parameters must have results"},
                status=status.HTTP_400_BAD_REQUEST
            )

        inspection.submit_for_approval()

        # Update vehicle entry status based on overall QC progress
        entry = inspection.arrival_slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


# ==================== Approval APIs ====================

class InspectionApproveChemistAPI(APIView):
    """QA Chemist approval"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveAsChemist]

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.is_locked:
            return Response(
                {"detail": "Inspection is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if inspection.workflow_status != InspectionWorkflowStatus.SUBMITTED:
            return Response(
                {"detail": "Inspection must be submitted first"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        inspection.approve_by_chemist(
            user=request.user,
            remarks=serializer.validated_data.get("remarks", "")
        )

        # Update vehicle entry status based on overall QC progress
        entry = inspection.arrival_slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


class InspectionApproveQAMAPI(APIView):
    """QA Manager approval"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveAsQAM]

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.is_locked:
            return Response(
                {"detail": "Inspection is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if inspection.workflow_status != InspectionWorkflowStatus.QA_CHEMIST_APPROVED:
            return Response(
                {"detail": "QA Chemist must approve first"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        final_status_str = serializer.validated_data.get("final_status", "ACCEPTED")
        final_status = getattr(InspectionStatus, final_status_str, InspectionStatus.ACCEPTED)

        inspection.approve_by_qam(
            user=request.user,
            remarks=serializer.validated_data.get("remarks", ""),
            final_status=final_status
        )

        # Update vehicle entry status based on overall QC progress
        entry = inspection.arrival_slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


class InspectionRejectAPI(APIView):
    """Reject inspection - sends back to security guard"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRejectInspection]

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.is_locked:
            return Response(
                {"detail": "Inspection is already locked and cannot be rejected"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Only allow rejection if inspection has been submitted
        allowed_statuses = [
            InspectionWorkflowStatus.SUBMITTED,
            InspectionWorkflowStatus.QA_CHEMIST_APPROVED,
        ]
        if inspection.workflow_status not in allowed_statuses:
            return Response(
                {"detail": "Inspection must be submitted before it can be rejected"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        remarks = serializer.validated_data.get("remarks", "")
        inspection.reject(user=request.user, remarks=remarks)

        # Update vehicle entry status based on overall QC progress
        entry = inspection.arrival_slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


class InspectionFactoryHeadDecisionAPI(APIView):
    """Factory Head decision after QA Manager rejection."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanFactoryHeadDecision]

    def post(self, request, inspection_id):
        inspection = get_object_or_404(
            RawMaterialInspection,
            id=inspection_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company
        )

        if inspection.final_status != InspectionStatus.REJECTED:
            return Response(
                {"detail": "Factory Head decision is only allowed after QA rejection"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if inspection.is_rejected_qc_returned:
            return Response(
                {"detail": "Factory Head decision is locked because the material is already out"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = FactoryHeadDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        inspection.record_factory_head_decision(
            user=request.user,
            decision=serializer.validated_data["decision"],
            remarks=serializer.validated_data.get("remarks", "")
        )

        entry = inspection.arrival_slip.po_item_receipt.po_receipt.vehicle_entry
        update_entry_status(entry)

        return Response(
            RawMaterialInspectionSerializer(inspection, context={'request': request}).data,
            status=status.HTTP_200_OK
        )


# ==================== Inspection List APIs (Status-Based) ====================

def _get_inspection_queryset(company):
    """Base queryset for inspection detail/approval APIs with optimized joins."""
    return RawMaterialInspection.objects.filter(
        arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=company
    ).select_related(
        "arrival_slip",
        "arrival_slip__po_item_receipt",
        "arrival_slip__po_item_receipt__po_receipt",
        "arrival_slip__po_item_receipt__po_receipt__vehicle_entry",
        "material_type",
        "qa_chemist",
        "qam",
        "rejected_by",
        "rejected_qc_return_item__entry",
    ).prefetch_related("parameter_results__parameter_master", "arrival_slip__attachments")


def _get_slip_list_queryset(company):
    """Base queryset for list endpoints. Queries from MaterialArrivalSlip with LEFT JOIN to inspection."""
    return MaterialArrivalSlip.objects.filter(
        po_item_receipt__po_receipt__vehicle_entry__company=company,
        status__in=[ArrivalSlipStatus.SUBMITTED, ArrivalSlipStatus.REJECTED],
    ).select_related(
        "inspection",
        "inspection__material_type",
        "inspection__rejected_qc_return_item__entry",
        "po_item_receipt",
        "po_item_receipt__po_receipt",
        "po_item_receipt__po_receipt__vehicle_entry",
    )


def _apply_date_filters(qs, request):
    """Apply from_date/to_date filters on submitted_at."""
    from_date = request.query_params.get("from_date")
    to_date = request.query_params.get("to_date")
    if from_date:
        qs = qs.filter(submitted_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(submitted_at__date__lte=to_date)
    return qs


class InspectionListAPI(APIView):
    """List all submitted arrival slips regardless of inspection status — 'All' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company)
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionPendingListAPI(APIView):
    """List arrival slips with no inspection — 'Pending' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__isnull=True
        )
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionDraftListAPI(APIView):
    """List arrival slips with draft inspections — 'Draft' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__workflow_status=InspectionWorkflowStatus.DRAFT
        )
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionActionableListAPI(APIView):
    """List items needing action — 'Actionable' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            Q(inspection__isnull=True) |
            Q(inspection__workflow_status__in=[
                InspectionWorkflowStatus.DRAFT,
                InspectionWorkflowStatus.SUBMITTED,
                InspectionWorkflowStatus.QA_CHEMIST_APPROVED,
            ])
        )
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionAwaitingChemistAPI(APIView):
    """List inspections awaiting QA Chemist approval"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveAsChemist]

    def get(self, request):
        qs = _get_inspection_queryset(request.company.company).filter(
            workflow_status=InspectionWorkflowStatus.SUBMITTED
        )
        serializer = RawMaterialInspectionSerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)


class InspectionAwaitingQAMAPI(APIView):
    """List inspections awaiting QA Manager approval"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveAsQAM]

    def get(self, request):
        qs = _get_inspection_queryset(request.company.company).filter(
            workflow_status=InspectionWorkflowStatus.QA_CHEMIST_APPROVED
        )
        serializer = RawMaterialInspectionSerializer(qs, many=True, context={'request': request})
        return Response(serializer.data)


class InspectionCompletedAPI(APIView):
    """List QAM-approved items — 'Approved' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
        )
        final_status_param = request.query_params.get("final_status")
        if final_status_param:
            qs = qs.filter(inspection__final_status=final_status_param)
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionRejectedAPI(APIView):
    """List rejected items — 'Rejected' tab"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__final_status=InspectionStatus.REJECTED
        )
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionReturnToVendorAPI(APIView):
    """List rejected inspections approved by Factory Head for vendor return."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__final_status=InspectionStatus.REJECTED,
            inspection__factory_head_decision=FactoryHeadDecision.RETURN_TO_VENDOR,
            inspection__rejected_qc_return_item__isnull=True,
        )
        qs = _apply_date_filters(qs, request)
        return Response(InspectionListItemSerializer(qs, many=True).data)


class InspectionCountsAPI(APIView):
    """Dashboard counts — single DB query using conditional aggregation"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        base = _get_slip_list_queryset(request.company.company)
        base = _apply_date_filters(base, request)

        counts = base.aggregate(
            not_started=Count("id", filter=Q(inspection__isnull=True)),
            draft=Count("id", filter=Q(inspection__workflow_status="DRAFT")),
            awaiting_chemist=Count("id", filter=Q(inspection__workflow_status="SUBMITTED")),
            awaiting_qam=Count("id", filter=Q(inspection__workflow_status="QA_CHEMIST_APPROVED")),
            completed=Count("id", filter=Q(
                inspection__workflow_status="QAM_APPROVED",
                inspection__final_status="ACCEPTED",
            )),
            rejected=Count("id", filter=Q(inspection__final_status="REJECTED")),
            hold=Count("id", filter=Q(
                inspection__workflow_status="QAM_APPROVED",
                inspection__final_status="HOLD",
            )),
        )
        counts["actionable"] = (
            counts["not_started"] + counts["draft"]
            + counts["awaiting_chemist"] + counts["awaiting_qam"]
        )
        return Response(counts)
