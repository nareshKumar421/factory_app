from datetime import datetime, timedelta

from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from vehicle_management.models.vehicle import VehicleType
from .models import Transporter, Vehicle
from .serializers import (
    TransporterNameSerializer,
    TransporterSerializer,
    VehicleNameSerializer,
    VehicleSerializer,
    VehicleEntrySerializer,
    VehicleTypeSerializer,
)

class TransporterListCreateAPI(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Transporter.objects.all().order_by("name")
        return Response(TransporterSerializer(qs, many=True).data)

    def post(self, request):
        serializer = TransporterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class TransporterNameListAPI(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Transporter.objects.all().order_by("name")
        return Response(TransporterNameSerializer(qs, many=True).data)
    

class TransporterDetailAPI(APIView):
    """
    Get or update transporter details by ID
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        transporter = get_object_or_404(Transporter, id=id)
        serializer = TransporterSerializer(transporter)
        return Response(serializer.data)

    def put(self, request, id):
        transporter = get_object_or_404(Transporter, id=id)
        serializer = TransporterSerializer(transporter, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)



class VehicleListCreateAPI(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Vehicle.objects.all().order_by("vehicle_number")
        return Response(VehicleSerializer(qs, many=True).data)

    def post(self, request):
        serializer = VehicleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    

class VehicleHistoryAPI(APIView):
    """Everything captured about a previously-registered vehicle, by reg number."""

    permission_classes = [IsAuthenticated]

    def get(self, request, vehicle_number):
        if not request.user.has_perm("dispatch_plans.can_link_dispatch_vehicle"):
            return Response(
                {"detail": "You do not have permission to view vehicle history."},
                status=status.HTTP_403_FORBIDDEN,
            )
        from .vehicle_history_service import build_vehicle_history

        data = build_vehicle_history(vehicle_number, request=request)
        return Response(data)


class VehicleNameListAPI(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Vehicle.objects.all().order_by("vehicle_number")
        return Response(VehicleNameSerializer(qs, many=True).data)
    
class VehicleDetailAPI(APIView):
    """
    Get vehicle details by ID
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, id):
        vehicle = get_object_or_404(Vehicle, id=id)
        serializer = VehicleSerializer(vehicle)
        return Response(serializer.data)

    def put(self, request, id):
        vehicle = get_object_or_404(Vehicle, id=id)
        serializer = VehicleSerializer(vehicle, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

class VehicleEntryListCreateAPI(APIView):
    """
    Gate root entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        # Validate required query parameters
        entry_type = request.query_params.get("entry_type")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if not entry_type:
            return Response(
                {"detail": "entry_type query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not from_date:
            return Response(
                {"detail": "from_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not to_date:
            return Response(
                {"detail": "to_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate date format
        try:
            datetime.strptime(from_date, "%Y-%m-%d")
            to_date_parsed = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Add one day to to_date to include the entire end day
        to_date_inclusive = (to_date_parsed + timedelta(days=1)).strftime("%Y-%m-%d")

        qs = VehicleEntry.objects.filter(
            company=request.company.company,
            entry_type=entry_type,
            created_at__range=(from_date, to_date_inclusive)
        ).prefetch_related(
            "po_receipts",
            "po_receipts__items__arrival_slip__inspection",
        ).order_by("-entry_time")

        return Response(
            VehicleEntrySerializer(
                qs, many=True,
                context={"request": request}
            ).data
        )

    def post(self, request):

        data = request.data.copy()
        data["company"] = request.company.company.id
        serializer = VehicleEntrySerializer(data=data)
        serializer.is_valid(raise_exception=True)

        entry = serializer.save(
            created_by=request.user
        )

        return Response(
            {
                "id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
            },
            status=status.HTTP_201_CREATED
        )



class VehicleEntryDetailAPI(APIView):
    """
    Get vehicle entry details by ID
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, id):
        entry = get_object_or_404(
            VehicleEntry,
            id=id,
            company=request.company.company
        )
        serializer = VehicleEntrySerializer(entry)
        return Response(serializer.data)


class VehicleEntryCountAPI(APIView):
    """
    Get total count of vehicle entries for the company
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        # Validate required query parameters
        entry_type = request.query_params.get("entry_type")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if not entry_type:
            return Response(
                {"detail": "entry_type query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not from_date:
            return Response(
                {"detail": "from_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not to_date:
            return Response(
                {"detail": "to_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate date format
        try:
            datetime.strptime(from_date, "%Y-%m-%d")
            to_date_parsed = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Add one day to to_date to include the entire end day
        to_date_inclusive = (to_date_parsed + timedelta(days=1)).strftime("%Y-%m-%d")

        count = VehicleEntry.objects.filter(
            company=request.company.company,
            entry_type=entry_type,
            created_at__range=(from_date, to_date_inclusive)
        ).values('status').annotate(count=models.Count('status'))

        return Response({"total_vehicle_entries": count})



class VehicleEntryListByStatus(APIView):
    """
    List vehicle entries filtered by status
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        # Validate required query parameters
        status_param = request.query_params.get("status")
        entry_type = request.query_params.get("entry_type")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if not status_param:
            return Response(
                {"detail": "status query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not entry_type:
            return Response(
                {"detail": "entry_type query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not from_date:
            return Response(
                {"detail": "from_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not to_date:
            return Response(
                {"detail": "to_date query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate date format
        try:
            datetime.strptime(from_date, "%Y-%m-%d")
            to_date_parsed = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Add one day to to_date to include the entire end day
        to_date_inclusive = (to_date_parsed + timedelta(days=1)).strftime("%Y-%m-%d")

        qs = VehicleEntry.objects.filter(
            company=request.company.company,
            status=status_param,
            entry_type=entry_type,
            created_at__range=(from_date, to_date_inclusive)
        ).prefetch_related(
            "po_receipts",
            "po_receipts__items__arrival_slip__inspection",
        ).order_by("-entry_time")

        return Response(
            VehicleEntrySerializer(
                qs, many=True,
                context={"request": request}
            ).data
        )
    
class VehicleTypeListAPI(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = VehicleType.objects.all().order_by("name")
        return Response(VehicleTypeSerializer(qs, many=True).data)
