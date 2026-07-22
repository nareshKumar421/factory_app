"""Post-dispatch truck tracking endpoints.

A dispatched truck is a ``VehicleArrival`` carrying at least one DISPATCHED
docking. These list such trucks with their current post-dispatch status and let
operators append status events (in transit, delivered, returned, ...). Scoped
across every company the user belongs to, since a truck is cross-company.
"""
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.models import (
    SalesDispatchGateOutStatus,
    TruckDispatchUpdate,
    VehicleArrival,
)
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.serializers_dispatch_tracking import (
    DispatchTrackingTruckSerializer,
    TruckDispatchUpdateCreateSerializer,
    TruckDispatchUpdateSerializer,
)
from gate_core.services.user_scope import user_company_ids


def _dispatched_trucks_qs(request):
    """Arrivals with a live DISPATCHED docking in one of the user's companies."""
    return (
        VehicleArrival.objects.filter(
            is_active=True,
            gate_outs__is_active=True,
            gate_outs__status=SalesDispatchGateOutStatus.DISPATCHED,
            gate_outs__company_id__in=list(user_company_ids(request)),
        )
        .distinct()
    )


class DispatchTrackingListView(APIView):
    """List dispatched trucks with their current post-dispatch status."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_dispatch_tracking"

    def get(self, request):
        arrivals = (
            _dispatched_trucks_qs(request)
            .select_related("vehicle", "driver")
            .prefetch_related(
                "gate_outs__company",
                "gate_outs__documents",
                "dispatch_updates",
            )
            .order_by("-departed_at", "-id")[:200]
        )
        return Response(DispatchTrackingTruckSerializer(arrivals, many=True).data)


class DispatchTrackingUpdatesView(APIView):
    """A truck's post-dispatch status timeline; POST appends a new event."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_dispatch_tracking",
        "POST": "gate_core.can_update_dispatch_tracking",
    }

    def _get_arrival(self, request, arrival_id):
        return get_object_or_404(_dispatched_trucks_qs(request), id=arrival_id)

    def get(self, request, arrival_id):
        arrival = self._get_arrival(request, arrival_id)
        updates = arrival.dispatch_updates.filter(is_active=True).select_related("created_by")
        return Response(TruckDispatchUpdateSerializer(updates, many=True).data)

    def post(self, request, arrival_id):
        arrival = self._get_arrival(request, arrival_id)
        serializer = TruckDispatchUpdateCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        update = TruckDispatchUpdate.objects.create(
            arrival=arrival,
            status=data["status"],
            occurred_at=data.get("occurred_at") or timezone.now(),
            location=data.get("location", ""),
            remarks=data.get("remarks", ""),
            proof=data.get("proof"),
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(
            TruckDispatchUpdateSerializer(update).data,
            status=status.HTTP_201_CREATED,
        )
