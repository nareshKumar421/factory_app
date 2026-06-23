from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from gate_core.services.user_scope import user_company_ids
from .models import Weighment
from .serializers import WeighmentSerializer


class WeighmentCreateUpdateAPI(APIView):
    """
    Create or update weighment for a gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, gate_entry_id):
        # Cross-company: the gate's weighment step acts on a vehicle entry that may
        # belong to a sibling company (the selector is a decorator). Resolve across
        # the user's companies, not the active Company-Code.
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company_id__in=user_company_ids(request),
        )

        weighment, _ = Weighment.objects.get_or_create(
            vehicle_entry=entry
        )

        serializer = WeighmentSerializer(
            weighment,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user, updated_by=request.user)

        return Response(WeighmentSerializer(weighment).data, status=status.HTTP_200_OK)


class WeighmentDetailAPI(APIView):
    """
    Get weighment details for a gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, gate_entry_id):
        # Cross-company: load the weighment for a vehicle entry of any of the user's
        # companies, so the tare shows for a sibling-company entry (not just the
        # active Company-Code).
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company_id__in=user_company_ids(request),
        )

        if not hasattr(entry, "weighment"):
            return Response(
                {"detail": "Weighment not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = WeighmentSerializer(entry.weighment)
        return Response(serializer.data)
