import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from driver_management.models import VehicleEntry
from gate_core.models import GateAttachment
from .models import ConstructionGateEntry, ConstructionMaterialCategory
from .serializers import ConstructionGateEntrySerializer, ConstructionMaterialCategorySerializer
from .services import complete_construction_gate_entry
from company.permissions import HasCompanyContext
from .permissions import (
    CanCreateConstructionEntry,
    CanViewConstructionEntry,
    CanEditConstructionEntry,
    CanCompleteConstructionEntry,
    CanViewMaterialCategory,
)

logger = logging.getLogger(__name__)


class ConstructionGateEntryCreateAPI(APIView):
    """
    Create/Read Construction / Civil Work Material gate entry.
    GET: Retrieve existing construction entry for a gate entry.
    POST: Create new construction entry for a gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateConstructionEntry()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewConstructionEntry()]

    def get(self, request, gate_entry_id):
        """Get construction entry for a specific vehicle entry"""
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if hasattr(entry, "construction_entry"):
            serializer = ConstructionGateEntrySerializer(entry.construction_entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"detail": "Construction entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

    @transaction.atomic
    def post(self, request, gate_entry_id):
        """Create construction entry for a specific vehicle entry"""
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        # Validate entry type
        if entry.entry_type != "CONSTRUCTION":
            logger.warning(
                f"Invalid entry type {entry.entry_type} for construction creation. "
                f"Gate entry ID: {gate_entry_id}, User: {request.user}"
            )
            return Response(
                {"detail": "Invalid entry type. Expected CONSTRUCTION."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if locked
        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if already exists
        if hasattr(entry, "construction_entry"):
            return Response(
                {"detail": "Construction entry already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ConstructionGateEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        construction_entry = serializer.save(
            vehicle_entry=entry,
            created_by=request.user
        )

        logger.info(
            f"Construction entry created. ID: {construction_entry.id}, "
            f"Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(
            {
                "message": "Construction gate entry created",
                "id": construction_entry.id,
                "work_order_number": construction_entry.work_order_number
            },
            status=status.HTTP_201_CREATED
        )


class ConstructionGateEntryUpdateAPI(APIView):
    """
    Update existing Construction / Civil Work Material gate entry.
    PUT/PATCH: Update construction entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditConstructionEntry]

    @transaction.atomic
    def put(self, request, gate_entry_id):
        """Update construction entry for a specific vehicle entry"""
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

        if not hasattr(entry, "construction_entry"):
            return Response(
                {"detail": "Construction entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ConstructionGateEntrySerializer(
            entry.construction_entry,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        logger.info(
            f"Construction entry updated. Gate entry: {gate_entry_id}, "
            f"User: {request.user}"
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


class ConstructionGateCompleteAPI(APIView):
    """
    Final completion for Construction / Civil Work Material gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteConstructionEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if not GateAttachment.objects.filter(gate_entry=entry, is_active=True).exists():
            return Response(
                {"detail": "Bill upload is required before completing this construction entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            complete_construction_gate_entry(entry)
            logger.info(
                f"Construction entry completed. Gate entry: {gate_entry_id}, "
                f"User: {request.user}"
            )
        except ValidationError as e:
            logger.warning(
                f"Construction completion failed. Gate entry: {gate_entry_id}, "
                f"Error: {str(e)}, User: {request.user}"
            )
            return Response(
                {"detail": str(e.detail) if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {"detail": "Construction gate entry completed successfully"},
            status=status.HTTP_200_OK
        )


class ConstructionMaterialCategoryListAPI(APIView):
    """
    List all active construction material categories for dropdown.
    """
    permission_classes = [IsAuthenticated, CanViewMaterialCategory]

    def get(self, request):
        categories = ConstructionMaterialCategory.objects.filter(is_active=True)
        serializer = ConstructionMaterialCategorySerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)



