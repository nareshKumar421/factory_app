# quality_control/views.py

import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.db import IntegrityError, transaction
from django.db.models import Q, Count, Prefetch

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from raw_material_gatein.models import POItemReceipt
from gate_core.enums import GateEntryStatus

from .models import (
    MaterialType,
    MaterialTypeSAPItem,
    QCParameterMaster,
    QCPrintDocument,
    MaterialArrivalSlip,
    RawMaterialInspection,
    InspectionParameterResult,
    InspectionAttachment,
    ArrivalSlipAttachment,
    AttachmentType,
)
from .serializers import (
    MaterialTypeSerializer,
    MaterialTypeCreateSerializer,
    MaterialTypeSAPItemLinkSerializer,
    QCPrintDocumentSerializer,
    QCParameterMasterSerializer,
    QCParameterMasterCreateSerializer,
    MaterialArrivalSlipSerializer,
    MaterialArrivalSlipCreateSerializer,
    RawMaterialInspectionSerializer,
    RawMaterialInspectionCreateSerializer,
    InspectionParameterResultSerializer,
    InspectionListItemSerializer,
    ApprovalSerializer,
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
    CanLinkMaterialTypeSAPItem,
    CanListOrManageMaterialTypes,
    CanManageMaterialTypes,
    CanManageQCParameters,
)
from .enums import (
    ArrivalSlipStatus,
    InspectionDecision,
    InspectionStatus,
    InspectionWorkflowStatus,
)
from .services.rules import update_entry_status

logger = logging.getLogger(__name__)

# `report_no` / `internal_lot_no` are generated with a non-atomic
# "read current max, then insert" scheme, so concurrent inspection saves can
# compute the same value and collide on the unique `report_no` constraint.
# When that happens we regenerate the identifiers and retry instead of
# surfacing the collision to the user.
MAX_INSPECTION_SAVE_RETRIES = 5


def _normalize_sap_item_code(item_code):
    return (item_code or "").strip().upper()


def _material_type_queryset(company):
    active_sap_items = MaterialTypeSAPItem.objects.filter(is_active=True).order_by("item_code")
    return MaterialType.objects.filter(
        company=company,
        is_active=True
    ).prefetch_related(Prefetch("sap_items", queryset=active_sap_items))


def _qc_print_document_queryset(company):
    return QCPrintDocument.objects.filter(company=company, is_active=True)


def _replace_material_type_sap_items(material_type, sap_items, user, company):
    if sap_items is None:
        return

    normalized_items = []
    seen_codes = set()
    for index, item in enumerate(sap_items, start=1):
        item_code = _normalize_sap_item_code(item.get("item_code"))
        item_name = (item.get("item_name") or "").strip()
        if not item_code:
            raise ValidationError({"sap_items": [f"Row {index}: SAP item code is required."]})
        if item_code in seen_codes:
            raise ValidationError({"sap_items": [f"SAP item {item_code} is duplicated in this material type."]})
        seen_codes.add(item_code)
        normalized_items.append({"item_code": item_code, "item_name": item_name})

    for item in normalized_items:
        conflict = MaterialTypeSAPItem.objects.select_related("material_type").filter(
            company=company,
            item_code=item["item_code"],
            is_active=True,
        ).exclude(material_type=material_type).first()
        if conflict:
            raise ValidationError({
                "sap_items": [
                    f"SAP item {item['item_code']} is already linked to "
                    f"{conflict.material_type.code} - {conflict.material_type.name}."
                ]
            })

    next_codes = {item["item_code"] for item in normalized_items}
    material_type.sap_items.filter(is_active=True).exclude(
        item_code__in=next_codes
    ).update(is_active=False, updated_by=user)

    for item in normalized_items:
        link = MaterialTypeSAPItem.objects.filter(
            company=company,
            item_code=item["item_code"],
        ).first()
        if link:
            link.material_type = material_type
            link.item_name = item["item_name"]
            link.is_active = True
            link.updated_by = user
            link.save()
        else:
            MaterialTypeSAPItem.objects.create(
                material_type=material_type,
                company=company,
                item_code=item["item_code"],
                item_name=item["item_name"],
                created_by=user,
            )


def _copy_material_type_parameters(source_material_type, target_material_type, user):
    source_parameters = list(
        source_material_type.qc_parameters.filter(is_active=True).order_by("sequence", "id")
    )
    if not source_parameters:
        raise ValidationError({
            "source_material_type_id": [
                "Source material type has no active QC parameters to copy."
            ]
        })

    source_codes = [param.parameter_code for param in source_parameters]
    existing_parameters = {
        param.parameter_code: param
        for param in target_material_type.qc_parameters.filter(parameter_code__in=source_codes)
    }

    copied_count = 0
    updated_count = 0
    copied_parameters = []
    copy_fields = [
        "parameter_name",
        "parameter_code",
        "standard_value",
        "parameter_type",
        "min_value",
        "max_value",
        "uom",
        "sequence",
        "is_mandatory",
    ]

    for source_param in source_parameters:
        parameter_data = {
            field: getattr(source_param, field)
            for field in copy_fields
        }
        target_param = existing_parameters.get(source_param.parameter_code)

        if target_param:
            for field, value in parameter_data.items():
                setattr(target_param, field, value)
            target_param.is_active = True
            target_param.updated_by = user
            target_param.save()
            updated_count += 1
        else:
            target_param = QCParameterMaster.objects.create(
                material_type=target_material_type,
                created_by=user,
                **parameter_data
            )
            copied_count += 1

        copied_parameters.append(target_param)

    return copied_count, updated_count, copied_parameters


def _link_sap_item_to_material_type(company, material_type, item_code, item_name, user):
    normalized_code = _normalize_sap_item_code(item_code)
    if not normalized_code:
        raise ValidationError({"item_code": ["SAP item code is required."]})

    link = MaterialTypeSAPItem.objects.select_for_update().filter(
        company=company,
        item_code=normalized_code,
    ).first()
    if link:
        link.material_type = material_type
        link.item_name = (item_name or "").strip()
        link.is_active = True
        link.updated_by = user
        link.save()
        return link

    return MaterialTypeSAPItem.objects.create(
        material_type=material_type,
        company=company,
        item_code=normalized_code,
        item_name=(item_name or "").strip(),
        created_by=user,
        updated_by=user,
    )


def _ensure_can_copy_qc_parameters(user):
    if not user.has_perm("quality_control.can_manage_qc_parameters"):
        raise PermissionDenied("You do not have permission to copy QC parameters.")


def _resolve_material_type_for_sap_item(company, item_code):
    normalized_code = _normalize_sap_item_code(item_code)
    if not normalized_code:
        raise ValidationError({"sap_code": ["SAP item code is required to resolve material type."]})

    link = MaterialTypeSAPItem.objects.select_related("material_type").filter(
        company=company,
        item_code=normalized_code,
        is_active=True,
        material_type__is_active=True,
    ).first()
    if not link:
        raise ValidationError({
            "material_type_id": [
                f"No active material type mapping found for SAP item {normalized_code}."
            ],
            "sap_code": [
                f"Link SAP item {normalized_code} to a material type before creating QC entry."
            ],
        })
    return link.material_type, normalized_code


def _sync_inspection_parameter_results(inspection, material_type, user):
    active_params = list(material_type.qc_parameters.filter(is_active=True))
    active_param_ids = [param.id for param in active_params]

    inspection.parameter_results.exclude(
        parameter_master_id__in=active_param_ids
    ).update(is_active=False, updated_by=user)

    existing_results = {
        result.parameter_master_id: result
        for result in inspection.parameter_results.filter(parameter_master_id__in=active_param_ids)
    }

    for param in active_params:
        result = existing_results.get(param.id)
        if result:
            update_fields = []
            if not result.is_active:
                result.is_active = True
                update_fields.append("is_active")
            if result.parameter_name != param.parameter_name:
                result.parameter_name = param.parameter_name
                update_fields.append("parameter_name")
            if result.standard_value != param.standard_value:
                result.standard_value = param.standard_value
                update_fields.append("standard_value")
            if update_fields:
                result.updated_by = user
                update_fields.extend(["updated_by", "updated_at"])
                result.save(update_fields=update_fields)
            continue

        InspectionParameterResult.objects.create(
            inspection=inspection,
            parameter_master=param,
            parameter_name=param.parameter_name,
            standard_value=param.standard_value,
            created_by=user,
        )


def _save_inspection_attachments(inspection, files, user):
    for attachment_file in files:
        InspectionAttachment.objects.create(
            inspection=inspection,
            file=attachment_file,
            original_name=(attachment_file.name or "")[:255],
            uploaded_by=user,
        )


# ==================== QC Print Document APIs ====================

class QCPrintDocumentListCreateAPI(APIView):
    """List and maintain QC print document IDs."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageQCParameters]

    def get(self, request):
        documents = _qc_print_document_queryset(request.company.company)
        serializer = QCPrintDocumentSerializer(documents, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = QCPrintDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        company = request.company.company
        document_key = data.pop("document_key")

        document = QCPrintDocument.objects.filter(
            company=company,
            document_key=document_key,
        ).first()
        response_status = status.HTTP_200_OK

        if document:
            for key, value in data.items():
                setattr(document, key, value)
            document.is_active = True
            document.updated_by = request.user
            document.save()
        else:
            document = QCPrintDocument.objects.create(
                company=company,
                document_key=document_key,
                created_by=request.user,
                **data,
            )
            response_status = status.HTTP_201_CREATED

        return Response(QCPrintDocumentSerializer(document).data, status=response_status)


class QCPrintDocumentDetailAPI(APIView):
    """Get, update, or delete a QC print document ID."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageQCParameters]

    def get(self, request, document_id):
        document = get_object_or_404(
            _qc_print_document_queryset(request.company.company),
            id=document_id,
        )
        return Response(QCPrintDocumentSerializer(document).data)

    def put(self, request, document_id):
        document = get_object_or_404(
            _qc_print_document_queryset(request.company.company),
            id=document_id,
        )
        serializer = QCPrintDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        next_key = data.get("document_key", document.document_key)
        duplicate = QCPrintDocument.objects.filter(
            company=request.company.company,
            document_key=next_key,
            is_active=True,
        ).exclude(id=document.id).first()
        if duplicate:
            raise ValidationError({
                "document_key": ["A print document already exists for this document type."]
            })

        for key, value in data.items():
            setattr(document, key, value)
        document.updated_by = request.user
        document.save()
        return Response(QCPrintDocumentSerializer(document).data)

    def delete(self, request, document_id):
        document = get_object_or_404(
            _qc_print_document_queryset(request.company.company),
            id=document_id,
        )
        document.is_active = False
        document.updated_by = request.user
        document.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ==================== Material Type APIs ====================

class MaterialTypeListCreateAPI(APIView):
    """List and create material types"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanListOrManageMaterialTypes]

    def get(self, request):
        material_types = _material_type_queryset(request.company.company)
        search = request.query_params.get("search")
        if search:
            material_types = material_types.filter(
                Q(code__icontains=search) |
                Q(name__icontains=search) |
                Q(description__icontains=search) |
                Q(sap_items__item_code__icontains=search, sap_items__is_active=True) |
                Q(sap_items__item_name__icontains=search, sap_items__is_active=True)
            ).distinct()
        serializer = MaterialTypeSerializer(material_types, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = MaterialTypeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        sap_items = data.pop("sap_items", None)
        copy_source_id = data.pop("copy_parameters_from_material_type_id", None)
        company = request.company.company

        # Check if a soft-deleted material type with the same code exists
        existing = MaterialType.objects.filter(
            company=company,
            code=data.get("code"),
            is_active=False,
        ).first()

        with transaction.atomic():
            if existing:
                for key, value in data.items():
                    setattr(existing, key, value)
                existing.is_active = True
                existing.updated_by = request.user
                existing.save()
                material_type = existing
            else:
                material_type = MaterialType.objects.create(
                    company=company,
                    created_by=request.user,
                    **data
                )

            _replace_material_type_sap_items(material_type, sap_items, request.user, company)
            if copy_source_id:
                _ensure_can_copy_qc_parameters(request.user)
                source_material_type = get_object_or_404(
                    MaterialType,
                    id=copy_source_id,
                    company=company,
                    is_active=True,
                )
                if source_material_type.id == material_type.id:
                    raise ValidationError({
                        "copy_parameters_from_material_type_id": [
                            "Select a different material type to copy parameters from."
                        ]
                    })
                _copy_material_type_parameters(
                    source_material_type,
                    material_type,
                    request.user,
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
            _material_type_queryset(request.company.company),
            id=material_type_id,
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
        data = dict(serializer.validated_data)
        sap_items = data.pop("sap_items", None)
        data.pop("copy_parameters_from_material_type_id", None)

        with transaction.atomic():
            for key, value in data.items():
                setattr(material_type, key, value)
            material_type.updated_by = request.user
            material_type.save()
            _replace_material_type_sap_items(
                material_type,
                sap_items,
                request.user,
                request.company.company,
            )

        return Response(MaterialTypeSerializer(material_type).data)

    def delete(self, request, material_type_id):
        material_type = get_object_or_404(
            MaterialType,
            id=material_type_id,
            company=request.company.company
        )
        material_type.is_active = False
        material_type.updated_by = request.user
        material_type.sap_items.filter(is_active=True).update(
            is_active=False,
            updated_by=request.user,
        )
        material_type.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MaterialTypeBySAPItemAPI(APIView):
    """Resolve the QC material type linked to a SAP item code."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request, item_code):
        material_type, _ = _resolve_material_type_for_sap_item(
            request.company.company,
            item_code,
        )
        serializer = MaterialTypeSerializer(material_type)
        return Response(serializer.data)


class MaterialTypeSAPItemLinkAPI(APIView):
    """Link a SAP item code to a QC material type."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanLinkMaterialTypeSAPItem]

    def post(self, request):
        serializer = MaterialTypeSAPItemLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company = request.company.company

        material_type = get_object_or_404(
            MaterialType,
            id=data["material_type_id"],
            company=company,
            is_active=True,
        )

        with transaction.atomic():
            _link_sap_item_to_material_type(
                company=company,
                material_type=material_type,
                item_code=data["item_code"],
                item_name=data.get("item_name", ""),
                user=request.user,
            )

        material_type = get_object_or_404(
            _material_type_queryset(company),
            id=material_type.id,
        )
        return Response(MaterialTypeSerializer(material_type).data)


class SAPItemSearchAPI(APIView):
    """Search SAP item master for linking items to QC material types."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageMaterialTypes]

    def get(self, request):
        from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError

        search = request.query_params.get("search", "").strip()
        if len(search) < 2:
            return Response([])

        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 100))

        try:
            reader = ProductionOrderReader(request.company.company.code)
            rows = reader.search_items(search=search, limit=limit)
        except SAPReadError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response([
            {
                "item_code": row.get("ItemCode") or "",
                "item_name": row.get("ItemName") or "",
                "uom": row.get("UomCode") or "",
            }
            for row in rows
        ])


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
    parser_classes = [JSONParser, MultiPartParser, FormParser]

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

        data = dict(serializer.validated_data)
        material_type_id = data.pop("material_type_id", None)
        sap_item_code = data.get("sap_code") or slip.po_item_receipt.po_item_code
        material_type, normalized_sap_code = _resolve_material_type_for_sap_item(
            request.company.company,
            sap_item_code,
        )
        if material_type_id and material_type_id != material_type.id:
            return Response(
                {
                    "material_type_id": [
                        "Selected material type does not match the SAP item mapping."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        data["sap_code"] = normalized_sap_code

        # Report number is manually entered by QC and must stay globally unique.
        # Reject duplicates up front with a clear field error (exclude this slip's
        # own inspection so re-saving/editing keeps its existing report number).
        report_no_value = data.get("report_no")
        if RawMaterialInspection.objects.filter(
            report_no=report_no_value
        ).exclude(arrival_slip=slip).exists():
            return Response(
                {"report_no": ["This report number is already in use."]},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Each iteration runs in its own transaction/savepoint. A unique collision
        # (e.g. a concurrent save of the same manually entered report number) is
        # rolled back and surfaced as a validation error after the retries.
        inspection = None
        created = False
        last_integrity_error = None
        for attempt in range(MAX_INSPECTION_SAVE_RETRIES):
            try:
                with transaction.atomic():
                    inspection, created = RawMaterialInspection.objects.get_or_create(
                        arrival_slip=slip,
                        defaults={
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
                        inspection.material_type = material_type
                        inspection.updated_by = request.user
                        inspection.save()

                    _sync_inspection_parameter_results(inspection, material_type, request.user)
                    _save_inspection_attachments(
                        inspection,
                        request.FILES.getlist("qc_attachments"),
                        request.user,
                    )
                break
            except IntegrityError as exc:
                # A collision here means the manually entered report number was
                # taken between the pre-check and the save (concurrent request).
                last_integrity_error = exc
                inspection = None
                logger.warning(
                    "Inspection save for slip %s hit an identifier collision "
                    "(attempt %s/%s); retrying.",
                    slip_id, attempt + 1, MAX_INSPECTION_SAVE_RETRIES,
                )
        else:
            logger.error(
                "Inspection save for slip %s failed after %s attempts: %s",
                slip_id, MAX_INSPECTION_SAVE_RETRIES, last_integrity_error,
            )
            return Response(
                {"report_no": ["This report number is already in use. Please use a different one."]},
                status=status.HTTP_400_BAD_REQUEST
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
        results = inspection.parameter_results.filter(is_active=True)
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
            parameter = get_object_or_404(
                QCParameterMaster,
                id=param_id,
                material_type=inspection.material_type,
                is_active=True,
            )
            result, _ = InspectionParameterResult.objects.get_or_create(
                inspection=inspection,
                parameter_master=parameter,
                defaults={"created_by": request.user}
            )

            result.is_active = True
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
            is_active=True,
            result_value=""
        )
        if mandatory_params.exists():
            return Response(
                {"detail": "All mandatory parameters must have results"},
                status=status.HTTP_400_BAD_REQUEST
            )

        inspection.submit_for_approval(user=request.user)

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
            remarks=serializer.validated_data.get("remarks", ""),
            decision=serializer.validated_data.get("decision", InspectionDecision.APPROVED),
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

        # The manager may revise an earlier decision, so QAM_APPROVED is a valid
        # starting state too — not only the first-time QA_CHEMIST_APPROVED.
        allowed_statuses = [
            InspectionWorkflowStatus.QA_CHEMIST_APPROVED,
            InspectionWorkflowStatus.QAM_APPROVED,
        ]
        if inspection.workflow_status not in allowed_statuses:
            return Response(
                {"detail": "QA Chemist must approve first"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Block changing a decision once its outcome is committed downstream.
        if inspection.is_grpo_done:
            return Response(
                {"detail": "Decision is locked: GRPO has already been posted for this material"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if inspection.is_rejected_qc_returned:
            return Response(
                {"detail": "Decision is locked: the rejected material has already been sent out at the gate"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        inspection.approve_by_qam(
            user=request.user,
            remarks=serializer.validated_data.get("remarks", ""),
            decision=serializer.validated_data.get("decision", InspectionDecision.APPROVED),
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
    ).prefetch_related(
        "parameter_results__parameter_master",
        "arrival_slip__attachments",
        "manager_decision_logs__decided_by",
        Prefetch("qc_attachments", queryset=InspectionAttachment.objects.select_related("uploaded_by")),
    )


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
    """List QA-rejected inspections available for vendor return at the gate."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInspection]

    def get(self, request):
        qs = _get_slip_list_queryset(request.company.company).filter(
            inspection__final_status=InspectionStatus.REJECTED,
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
