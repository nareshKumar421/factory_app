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
from .models import DailyNeedGateEntry
from .serializers import CategoryListSerializer, DailyNeedGateEntrySerializer
from .services import complete_daily_need_gate_entry
from company.permissions import HasCompanyContext
from .permissions import (
    CanCreateDailyNeedEntry,
    CanViewDailyNeedEntry,
    CanCompleteDailyNeedEntry,
    CanViewCategory,
)

logger = logging.getLogger(__name__)


class DailyNeedGateEntryCreateAPI(APIView):
    """
    Create Daily Need / Canteen gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateDailyNeedEntry()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewDailyNeedEntry()]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(VehicleEntry, id=gate_entry_id, company=request.company.company)
        if hasattr(entry, "daily_need_entry"):
            serializer = DailyNeedGateEntrySerializer(entry.daily_need_entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"detail": "Daily need entry does not exist"},
                status=status.HTTP_404_NOT_FOUND
            )

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(VehicleEntry, id=gate_entry_id, company=request.company.company)

        if entry.entry_type != "DAILY_NEED":
            logger.warning(
                f"Invalid entry type {entry.entry_type} for daily need creation. "
                f"Gate entry ID: {gate_entry_id}, User: {request.user}"
            )
            return Response(
                {"detail": "Invalid entry type. Expected DAILY_NEED."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST
            )

        existing_daily_entry = getattr(entry, "daily_need_entry", None)
        serializer = DailyNeedGateEntrySerializer(
            existing_daily_entry,
            data=request.data,
        ) if existing_daily_entry else DailyNeedGateEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        daily_entry = serializer.save(
            vehicle_entry=entry,
            created_by=request.user
        )

        logger.info(
            f"Daily need entry saved. ID: {daily_entry.id}, "
            f"Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(
            {
                "message": "Daily need gate entry saved",
                "id": daily_entry.id
            },
            status=status.HTTP_200_OK if existing_daily_entry else status.HTTP_201_CREATED
        )


class DailyNeedGateCompleteAPI(APIView):
    """
    Final completion for Daily Need / Canteen gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteDailyNeedEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(VehicleEntry, id=gate_entry_id, company=request.company.company)

        if not GateAttachment.objects.filter(gate_entry=entry, is_active=True).exists():
            return Response(
                {"detail": "Bill upload is required before completing this daily need entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            complete_daily_need_gate_entry(entry)
            logger.info(
                f"Daily need entry completed. Gate entry: {gate_entry_id}, "
                f"User: {request.user}"
            )
        except ValidationError as e:
            logger.warning(
                f"Daily need completion failed. Gate entry: {gate_entry_id}, "
                f"Error: {str(e)}, User: {request.user}"
            )
            return Response(
                {"detail": str(e.detail) if hasattr(e, 'detail') else str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {"detail": "Daily need gate entry completed successfully"},
            status=status.HTTP_200_OK
        )
    

class CategoryListAPI(APIView):
    """
    List all categories for Daily Need / Canteen items
    """
    permission_classes = [IsAuthenticated, CanViewCategory]

    def get(self, request):
        categories = DailyNeedGateEntry.item_category.field.related_model.objects.all()
        serializer = CategoryListSerializer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
