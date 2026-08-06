import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from driver_management.models import VehicleEntry
from gate_core.models import GateAttachment
from maintenance.constants import GateQCStatus, GateReceiptStatus, SpareMovementType
from maintenance.models import (
    MaintenanceGateLink,
    MaintenanceSpare,
    MaintenanceSpareReceipt,
    SpareMovement,
)
from maintenance.serializers import (
    MaintenanceGateReceiptActionSerializer,
    MaintenanceSpareReceiptSerializer,
)
from .models import MaintenanceGateEntry, MaintenanceType
from .serializers import MaintenanceGateEntrySerializer, MaintenanceTypeSerializer
from .services import complete_maintenance_gate_entry
from company.permissions import HasCompanyContext
from .permissions import (
    CanCreateMaintenanceEntry,
    CanViewMaintenanceEntry,
    CanEditMaintenanceEntry,
    CanCompleteMaintenanceEntry,
    CanViewMaintenanceType,
)

logger = logging.getLogger(__name__)


class MaintenanceGateEntryCreateAPI(APIView):
    """
    Create/Read Maintenance & Repair Material gate entry.
    GET: Retrieve existing maintenance entry for a gate entry.
    POST: Create new maintenance entry for a gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateMaintenanceEntry()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewMaintenanceEntry()]

    def get(self, request, gate_entry_id):
        """Get maintenance entry for a specific vehicle entry"""
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if hasattr(entry, "maintenance_entry"):
            serializer = MaintenanceGateEntrySerializer(
                entry.maintenance_entry,
                context={"request": request},
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"detail": "Maintenance entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

    @transaction.atomic
    def post(self, request, gate_entry_id):
        """Create maintenance entry for a specific vehicle entry"""
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        # Validate entry type
        if entry.entry_type != "MAINTENANCE":
            logger.warning(
                f"Invalid entry type {entry.entry_type} for maintenance creation. "
                f"Gate entry ID: {gate_entry_id}, User: {request.user}"
            )
            return Response(
                {"detail": "Invalid entry type. Expected MAINTENANCE."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if locked
        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if already exists
        if hasattr(entry, "maintenance_entry"):
            return Response(
                {"detail": "Maintenance entry already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = MaintenanceGateEntrySerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        maintenance_entry = serializer.save(
            vehicle_entry=entry,
            created_by=request.user
        )

        logger.info(
            f"Maintenance entry created. ID: {maintenance_entry.id}, "
            f"Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(
            {
                "message": "Maintenance gate entry created",
                "id": maintenance_entry.id,
                "work_order_number": maintenance_entry.work_order_number
            },
            status=status.HTTP_201_CREATED
        )


class MaintenanceGateEntryUpdateAPI(APIView):
    """
    Update existing Maintenance & Repair Material gate entry.
    PUT/PATCH: Update maintenance entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditMaintenanceEntry]

    @transaction.atomic
    def put(self, request, gate_entry_id):
        """Update maintenance entry for a specific vehicle entry"""
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not hasattr(entry, "maintenance_entry"):
            return Response(
                {"detail": "Maintenance entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = MaintenanceGateEntrySerializer(
            entry.maintenance_entry,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        logger.info(
            f"Maintenance entry updated. Gate entry: {gate_entry_id}, "
            f"User: {request.user}"
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


class MaintenanceGateCompleteAPI(APIView):
    """
    Final completion for Maintenance & Repair Material gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteMaintenanceEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if not GateAttachment.objects.filter(gate_entry=entry, is_active=True).exists():
            return Response(
                {"detail": "Bill upload is required before completing this maintenance entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            complete_maintenance_gate_entry(entry)
            logger.info(
                f"Maintenance entry completed. Gate entry: {gate_entry_id}, "
                f"User: {request.user}"
            )
        except ValidationError as e:
            logger.warning(
                f"Maintenance completion failed. Gate entry: {gate_entry_id}, "
                f"Error: {str(e)}, User: {request.user}"
            )
            return Response(
                {"detail": str(e.detail) if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {"detail": "Maintenance gate entry completed successfully"},
            status=status.HTTP_200_OK
        )


class MaintenanceGateReceiveSpareAPI(APIView):
    """
    Receive a linked maintenance spare into store stock from a maintenance gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditMaintenanceEntry]

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )
        if not hasattr(entry, "maintenance_entry"):
            return Response(
                {"detail": "Maintenance entry does not exist"},
                status=status.HTTP_404_NOT_FOUND,
            )

        maintenance_entry = entry.maintenance_entry
        try:
            link = (
                MaintenanceGateLink.objects.select_for_update()
                .get(gate_entry=maintenance_entry, company=request.company.company)
            )
        except MaintenanceGateLink.DoesNotExist:
            return Response(
                {"detail": "No maintenance asset/spare link exists for this gate entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not link.spare_id:
            return Response(
                {"detail": "A spare master link is required before receiving stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if link.receipt_status == GateReceiptStatus.RECEIVED:
            return Response(
                {"detail": "This gate spare has already been received into stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = MaintenanceGateReceiptActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt_qc_status = serializer.validated_data.get("qc_status", link.qc_status)
        if link.qc_required and receipt_qc_status not in [
            GateQCStatus.ACCEPTED,
            GateQCStatus.WAIVED,
        ]:
            link.qc_status = receipt_qc_status
            link.receipt_status = GateReceiptStatus.BLOCKED
            link.updated_by = request.user
            link.save(update_fields=["qc_status", "receipt_status", "updated_by", "updated_at"])
            return Response(
                {"detail": "QC must be accepted or waived before receiving this critical spare."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        spare = MaintenanceSpare.objects.select_for_update().get(pk=link.spare_id)
        quantity = serializer.validated_data.get("quantity") or maintenance_entry.quantity
        unit_cost = serializer.validated_data.get("unit_cost")
        if unit_cost in (None, ""):
            unit_cost = spare.unit_cost

        grpo_reference = serializer.validated_data.get("grpo_reference", link.grpo_reference)
        grpo_doc_entry = serializer.validated_data.get("grpo_doc_entry", link.grpo_doc_entry)
        grpo_doc_num = serializer.validated_data.get("grpo_doc_num", link.grpo_doc_num)

        receipt = MaintenanceSpareReceipt.objects.create(
            company=request.company.company,
            gate_link=link,
            asset=link.asset,
            work_order=link.work_order,
            spare=spare,
            quantity=quantity,
            unit_cost=unit_cost,
            qc_status=receipt_qc_status,
            grpo_reference=grpo_reference or "",
            grpo_doc_entry=grpo_doc_entry,
            grpo_doc_num=grpo_doc_num or "",
            invoice_number=maintenance_entry.invoice_number or "",
            received_by=request.user,
            remarks=serializer.validated_data.get("remarks", ""),
            created_by=request.user,
            updated_by=request.user,
        )

        spare.current_stock += quantity
        spare.updated_by = request.user
        spare.save(update_fields=["current_stock", "updated_by", "updated_at"])

        SpareMovement.objects.create(
            company=request.company.company,
            spare_request=None,
            work_order=link.work_order,
            spare=spare,
            movement_type=SpareMovementType.RECEIPT,
            quantity=quantity,
            unit_cost=unit_cost,
            remarks=(
                serializer.validated_data.get("remarks", "")
                or f"Received from maintenance gate {maintenance_entry.work_order_number}"
            ),
            performed_by=request.user,
            created_by=request.user,
            updated_by=request.user,
        )

        link.qc_status = receipt_qc_status
        link.grpo_reference = grpo_reference or ""
        link.grpo_doc_entry = grpo_doc_entry
        link.grpo_doc_num = grpo_doc_num or ""
        link.receipt_status = GateReceiptStatus.RECEIVED
        link.received_quantity = quantity
        link.received_at = timezone.now()
        link.received_by = request.user
        link.updated_by = request.user
        link.save(
            update_fields=[
                "qc_status",
                "grpo_reference",
                "grpo_doc_entry",
                "grpo_doc_num",
                "receipt_status",
                "received_quantity",
                "received_at",
                "received_by",
                "updated_by",
                "updated_at",
            ]
        )

        response_serializer = MaintenanceSpareReceiptSerializer(
            receipt,
            context={"request": request},
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class MaintenanceTypeListAPI(APIView):
    """
    List all active maintenance types for dropdown.
    """
    permission_classes = [IsAuthenticated, CanViewMaintenanceType]

    def get(self, request):
        types = MaintenanceType.objects.filter(is_active=True)
        serializer = MaintenanceTypeSerializer(types, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
