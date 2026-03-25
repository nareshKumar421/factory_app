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
from company.permissions import HasCompanyContext
from .models import OutboundGateEntry, OutboundPurpose
from .serializers import (
    OutboundGateEntrySerializer,
    OutboundGateEntryListSerializer,
    OutboundPurposeSerializer,
)
from .permissions import (
    CanCreateOutboundEntry,
    CanViewOutboundEntry,
    CanEditOutboundEntry,
    CanCompleteOutboundEntry,
    CanReleaseForLoading,
    CanViewOutboundPurpose,
)
from .services import complete_outbound_gate_entry

logger = logging.getLogger(__name__)


class OutboundGateEntryCreateAPI(APIView):
    """
    GET  /gate-entries/<gate_entry_id>/outbound/  → Read existing outbound entry
    POST /gate-entries/<gate_entry_id>/outbound/  → Create outbound entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateOutboundEntry()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewOutboundEntry()]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )
        if hasattr(entry, "outbound_entry"):
            serializer = OutboundGateEntrySerializer(entry.outbound_entry)
            return Response(serializer.data)
        return Response(
            {"detail": "Outbound entry does not exist"},
            status=status.HTTP_404_NOT_FOUND,
        )

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )

        if entry.entry_type != "OUTBOUND":
            return Response(
                {"detail": "Invalid entry type. Expected OUTBOUND."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if hasattr(entry, "outbound_entry"):
            return Response(
                {"detail": "Outbound entry already exists"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = OutboundGateEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outbound_entry = serializer.save(
            vehicle_entry=entry,
            created_by=request.user,
        )

        logger.info(
            f"Outbound entry created. ID: {outbound_entry.id}, "
            f"Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(
            {
                "message": "Outbound gate entry created",
                "id": outbound_entry.id,
            },
            status=status.HTTP_201_CREATED,
        )


class OutboundGateEntryUpdateAPI(APIView):
    """PUT /gate-entries/<gate_entry_id>/outbound/update/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditOutboundEntry]

    @transaction.atomic
    def put(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )

        if entry.is_locked:
            return Response(
                {"detail": "Gate entry is locked"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not hasattr(entry, "outbound_entry"):
            return Response(
                {"detail": "Outbound entry does not exist"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = OutboundGateEntrySerializer(
            entry.outbound_entry,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        logger.info(
            f"Outbound entry updated. Gate entry: {gate_entry_id}, User: {request.user}"
        )

        return Response(serializer.data)


class OutboundGateCompleteAPI(APIView):
    """POST /gate-entries/<gate_entry_id>/complete/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteOutboundEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )

        try:
            complete_outbound_gate_entry(entry)
            logger.info(
                f"Outbound entry completed. Gate entry: {gate_entry_id}, "
                f"User: {request.user}"
            )
        except ValidationError as e:
            return Response(
                {"detail": str(e.detail) if hasattr(e, "detail") else str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"detail": "Outbound gate entry completed successfully"}
        )


class ReleaseForLoadingAPI(APIView):
    """POST /gate-entries/<gate_entry_id>/release-for-loading/"""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReleaseForLoading]

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
        )

        if not hasattr(entry, "outbound_entry"):
            return Response(
                {"detail": "Outbound entry does not exist"},
                status=status.HTTP_404_NOT_FOUND,
            )

        outbound_entry = entry.outbound_entry

        if outbound_entry.released_for_loading_at:
            return Response(
                {"detail": "Vehicle already released for loading"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not outbound_entry.vehicle_empty_confirmed:
            return Response(
                {"detail": "Vehicle must be confirmed empty before release"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        outbound_entry.released_for_loading_at = timezone.now()
        outbound_entry.save(update_fields=["released_for_loading_at"])

        logger.info(
            f"Vehicle released for loading. Gate entry: {gate_entry_id}, "
            f"User: {request.user}"
        )

        return Response(
            OutboundGateEntrySerializer(outbound_entry).data
        )


class OutboundGateEntryListAPI(APIView):
    """
    GET /available-vehicles/  → List outbound vehicle entries available for linking.
    Used by outbound_dispatch link-vehicle dropdown.
    Filters: only OUTBOUND entries, confirmed empty, for the company.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOutboundEntry]

    def get(self, request):
        company = request.company.company

        qs = OutboundGateEntry.objects.select_related(
            "vehicle_entry",
            "vehicle_entry__vehicle",
            "vehicle_entry__driver",
        ).filter(
            vehicle_entry__company=company,
            vehicle_entry__entry_type="OUTBOUND",
            vehicle_empty_confirmed=True,
        )

        # Optional filters
        gate_status = request.query_params.get("status")
        if gate_status:
            qs = qs.filter(vehicle_entry__status=gate_status)

        # Exclude vehicles already linked to an active shipment
        exclude_linked = request.query_params.get("exclude_linked", "true")
        if exclude_linked.lower() == "true":
            from outbound_dispatch.models import ShipmentOrder, ShipmentStatus
            linked_ve_ids = ShipmentOrder.objects.filter(
                company=company,
                vehicle_entry__isnull=False,
            ).exclude(
                status__in=[ShipmentStatus.DISPATCHED, ShipmentStatus.CANCELLED]
            ).values_list("vehicle_entry_id", flat=True)
            qs = qs.exclude(vehicle_entry_id__in=linked_ve_ids)

        serializer = OutboundGateEntryListSerializer(qs, many=True)
        return Response(serializer.data)


class OutboundPurposeListAPI(APIView):
    """GET /purposes/ → List active outbound purposes for dropdown."""
    permission_classes = [IsAuthenticated, CanViewOutboundPurpose]

    def get(self, request):
        purposes = OutboundPurpose.objects.filter(is_active=True)
        serializer = OutboundPurposeSerializer(purposes, many=True)
        return Response(serializer.data)
