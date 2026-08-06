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
from .models import FixedAssetGateEntry, AssetCategory
from .serializers import FixedAssetGateEntrySerializer, AssetCategorySerializer
from .services import complete_fixed_asset_gate_entry
from company.permissions import HasCompanyContext
from .permissions import (
    CanCreateFixedAssetEntry,
    CanViewFixedAssetEntry,
    CanEditFixedAssetEntry,
    CanCompleteFixedAssetEntry,
    CanViewAssetCategory,
)

logger = logging.getLogger(__name__)


class FixedAssetGateEntryCreateAPI(APIView):
    """
    Create/Read a Fixed Asset gate entry.
    GET: Retrieve existing fixed asset entry for a gate entry.
    POST: Create new fixed asset entry for a gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateFixedAssetEntry()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewFixedAssetEntry()]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if hasattr(entry, "fixed_asset_entry"):
            serializer = FixedAssetGateEntrySerializer(entry.fixed_asset_entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"detail": "Fixed asset entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if entry.entry_type != "FIXED_ASSET":
            logger.warning(
                f"Invalid entry type {entry.entry_type} for fixed asset creation. "
                f"Gate entry ID: {gate_entry_id}, User: {request.user}"
            )
            return Response(
                {"detail": "Invalid entry type. Expected FIXED_ASSET."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if hasattr(entry, "fixed_asset_entry"):
            return Response(
                {"detail": "Fixed asset entry already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = FixedAssetGateEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        asset_entry = serializer.save(
            vehicle_entry=entry,
            created_by=request.user
        )

        logger.info(
            f"Fixed asset entry created. ID: {asset_entry.id}, "
            f"Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(
            {
                "message": "Fixed asset gate entry created",
                "id": asset_entry.id,
                "work_order_number": asset_entry.work_order_number
            },
            status=status.HTTP_201_CREATED
        )


class FixedAssetGateEntryUpdateAPI(APIView):
    """
    Update an existing Fixed Asset gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditFixedAssetEntry]

    @transaction.atomic
    def put(self, request, gate_entry_id):
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

        if not hasattr(entry, "fixed_asset_entry"):
            return Response(
                {"detail": "Fixed asset entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = FixedAssetGateEntrySerializer(
            entry.fixed_asset_entry,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        logger.info(
            f"Fixed asset entry updated. Gate entry: {gate_entry_id}, "
            f"User: {request.user}"
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


class FixedAssetGateCompleteAPI(APIView):
    """
    Final completion (lock) for a Fixed Asset gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteFixedAssetEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        if not GateAttachment.objects.filter(gate_entry=entry, is_active=True).exists():
            return Response(
                {"detail": "Document upload is required before completing this asset entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            complete_fixed_asset_gate_entry(entry)
            logger.info(
                f"Fixed asset entry completed. Gate entry: {gate_entry_id}, "
                f"User: {request.user}"
            )
        except ValidationError as e:
            logger.warning(
                f"Fixed asset completion failed. Gate entry: {gate_entry_id}, "
                f"Error: {str(e)}, User: {request.user}"
            )
            return Response(
                {"detail": str(e.detail) if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {"detail": "Fixed asset gate entry completed successfully"},
            status=status.HTTP_200_OK
        )


class AssetCategoryListAPI(APIView):
    """
    List all active asset categories for the dropdown.
    """
    permission_classes = [IsAuthenticated, CanViewAssetCategory]

    def get(self, request):
        categories = AssetCategory.objects.filter(is_active=True)
        serializer = AssetCategorySerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
