"""The company vehicle register's API.

Shape of it: a vehicle master with CRUD, two money registers that each add an
approval endpoint, a document register read mostly by expiry, and the handful
of read-only endpoints the dashboard and the cost report need.

Every write to a fuel entry -- create, edit or delete -- ends by recomputing
that vehicle's mileage chain, because all three move the row its neighbours
are measured against.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .constants import (
    ApprovalStatus,
    DOCUMENT_WARNING_DAYS,
    DocumentKind,
    FUEL_UNITS,
    FILLABLE_FUELS,
    FuelType,
    PaymentMode,
    SERVICE_DUE_WARNING_KM,
    ServiceKind,
    VehicleCategory,
    VehicleStatus,
)
from .models import FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument
from .permissions import (
    ADD_EXPENSE_PERMISSION,
    APPROVE_EXPENSE_PERMISSION,
    MANAGE_VEHICLE_PERMISSION,
    CanApproveFleetExpense,
    CanViewFleet,
    FleetExpensePermission,
    FleetVehiclePermission,
)
from .serializers import (
    ApprovalActionSerializer,
    FleetVehicleSerializer,
    FleetVehicleWriteSerializer,
    FuelEntrySerializer,
    FuelEntryWriteSerializer,
    ServiceEntrySerializer,
    ServiceEntryWriteSerializer,
    VehicleDocumentSerializer,
)

UPLOAD_PARSERS = [MultiPartParser, FormParser, JSONParser]


def _date(request, name):
    """One query parameter as a date, or None. A bad date is simply ignored."""
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _choices(enum):
    return [{"value": value, "label": label} for value, label in enum.choices]


class FleetOptionsAPI(APIView):
    """Everything the forms need to draw themselves, and what this user may do.

    Sent by the server rather than hardcoded in the client so a category or a
    payment mode added in ``constants.py`` reaches the form without a frontend
    release, and so the page's buttons match the API's real answer.
    """

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        user = request.user
        return Response(
            {
                "categories": _choices(VehicleCategory),
                "fuel_types": _choices(FuelType),
                "fillable_fuels": [
                    {
                        "value": fuel,
                        "label": FuelType(fuel).label,
                        "unit": FUEL_UNITS.get(fuel, "L"),
                    }
                    for fuel in FILLABLE_FUELS
                ],
                "vehicle_statuses": _choices(VehicleStatus),
                "payment_modes": _choices(PaymentMode),
                "service_kinds": _choices(ServiceKind),
                "document_kinds": _choices(DocumentKind),
                "approval_statuses": _choices(ApprovalStatus),
                "document_warning_days": DOCUMENT_WARNING_DAYS,
                "service_due_warning_km": SERVICE_DUE_WARNING_KM,
                "can_manage_vehicles": user.has_perm(MANAGE_VEHICLE_PERMISSION),
                "can_add_expense": user.has_perm(ADD_EXPENSE_PERMISSION)
                or user.has_perm(MANAGE_VEHICLE_PERMISSION),
                "can_approve_expense": user.has_perm(APPROVE_EXPENSE_PERMISSION),
            }
        )


# ----------------------------------------------------------------- vehicles


class FleetVehicleListCreateAPI(generics.ListCreateAPIView):
    """GET the fleet, POST a new vehicle.

    Filters: ``search`` (number, nickname, make, who keeps it), ``category``,
    ``status``, ``fuel_type``. ``include_inactive=true`` brings back vehicles
    that were retired from the register.
    """

    permission_classes = [IsAuthenticated, FleetVehiclePermission]
    parser_classes = UPLOAD_PARSERS

    def get_serializer_class(self):
        return (
            FleetVehicleWriteSerializer if self.request.method == "POST" else FleetVehicleSerializer
        )

    def get_queryset(self):
        params = self.request.query_params
        queryset = FleetVehicle.objects.all()
        if params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        for field in ("category", "status", "fuel_type"):
            if params.get(field):
                queryset = queryset.filter(**{field: params[field]})
        search = (params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(vehicle_number__icontains=search)
                | Q(nickname__icontains=search)
                | Q(make_model__icontains=search)
                | Q(assigned_to__icontains=search)
            )
        return queryset.prefetch_related("documents", "fuel_entries", "service_entries")

    def create(self, request, *args, **kwargs):
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        vehicle = write.save(created_by=request.user, updated_by=request.user)
        return Response(FleetVehicleSerializer(vehicle).data, status=status.HTTP_201_CREATED)


class FleetVehicleDetailAPI(APIView):
    """One vehicle: read it, edit it, or retire it.

    A delete is a retirement, never a row removal -- the fuel and service
    history hanging off it is the point of the module.
    """

    permission_classes = [IsAuthenticated, FleetVehiclePermission]
    parser_classes = UPLOAD_PARSERS

    def get_object(self, pk):
        return get_object_or_404(FleetVehicle, pk=pk)

    def get(self, request, pk):
        return Response(FleetVehicleSerializer(self.get_object(pk)).data)

    def patch(self, request, pk):
        vehicle = self.get_object(pk)
        write = FleetVehicleWriteSerializer(vehicle, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        vehicle = write.save(updated_by=request.user)
        return Response(FleetVehicleSerializer(vehicle).data)

    def delete(self, request, pk):
        vehicle = self.get_object(pk)
        vehicle.is_active = False
        vehicle.updated_by = request.user
        vehicle.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class FleetVehicleSummaryAPI(APIView):
    """One vehicle's running cost over ``from``/``to``, with a monthly series."""

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request, pk):
        vehicle = get_object_or_404(FleetVehicle, pk=pk)
        summary = services.vehicle_summary(vehicle, _date(request, "from"), _date(request, "to"))
        return Response({"vehicle": FleetVehicleSerializer(vehicle).data, **summary})


# -------------------------------------------------------------------- money


class _EntryListCreateAPI(generics.ListCreateAPIView):
    """Shared list/create for the two money registers.

    Filters: ``vehicle``, ``from``, ``to``, ``approval_status``, ``search``.
    """

    permission_classes = [IsAuthenticated, FleetExpensePermission]
    parser_classes = UPLOAD_PARSERS
    model = None
    read_serializer = None
    write_serializer = None
    search_fields: tuple[str, ...] = ()

    def get_serializer_class(self):
        return self.write_serializer if self.request.method == "POST" else self.read_serializer

    def get_queryset(self):
        params = self.request.query_params
        queryset = self.model.objects.select_related("vehicle", "approved_by", "created_by")
        if params.get("vehicle"):
            queryset = queryset.filter(vehicle_id=params["vehicle"])
        if params.get("approval_status"):
            queryset = queryset.filter(approval_status=params["approval_status"])
        date_from = _date(self.request, "from")
        date_to = _date(self.request, "to")
        if date_from:
            queryset = queryset.filter(entry_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(entry_date__lte=date_to)
        search = (params.get("search") or "").strip()
        if search and self.search_fields:
            condition = Q()
            for field in self.search_fields:
                condition |= Q(**{f"{field}__icontains": search})
            queryset = queryset.filter(condition)
        return queryset

    def after_write(self, entry):
        """Hook for whatever has to be recomputed once a row has moved."""

    def create(self, request, *args, **kwargs):
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        entry = write.save(created_by=request.user, updated_by=request.user)
        self.after_write(entry)
        entry.refresh_from_db()
        return Response(self.read_serializer(entry).data, status=status.HTTP_201_CREATED)


class _EntryDetailAPI(APIView):
    """Shared read/edit/delete for one money entry.

    An approved entry is frozen: correcting it means sending it back first, so
    a figure that was passed cannot quietly change afterwards.
    """

    permission_classes = [IsAuthenticated, FleetExpensePermission]
    parser_classes = UPLOAD_PARSERS
    model = None
    read_serializer = None
    write_serializer = None

    def get_object(self, pk):
        return get_object_or_404(self.model.objects.select_related("vehicle"), pk=pk)

    def after_write(self, vehicle):
        """Hook for whatever has to be recomputed once a row has moved."""

    def get(self, request, pk):
        return Response(self.read_serializer(self.get_object(pk)).data)

    def patch(self, request, pk):
        entry = self.get_object(pk)
        if entry.approval_status == ApprovalStatus.APPROVED:
            return Response(
                {"detail": "This entry is approved. Send it back before changing it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        write = self.write_serializer(entry, data=request.data, partial=True)
        write.is_valid(raise_exception=True)
        entry = write.save(updated_by=request.user)
        self.after_write(entry.vehicle)
        entry.refresh_from_db()
        return Response(self.read_serializer(entry).data)

    def delete(self, request, pk):
        entry = self.get_object(pk)
        if entry.approval_status == ApprovalStatus.APPROVED:
            return Response(
                {"detail": "This entry is approved. Send it back before deleting it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        vehicle = entry.vehicle
        entry.delete()
        self.after_write(vehicle)
        return Response(status=status.HTTP_204_NO_CONTENT)


class _EntryApprovalAPI(APIView):
    """POST an approval or a rejection on one entry.

    Separate right from entering, and a separate endpoint from editing, so
    passing a bill is one deliberate act.
    """

    permission_classes = [IsAuthenticated, CanApproveFleetExpense]
    model = None
    read_serializer = None

    def post(self, request, pk):
        entry = get_object_or_404(self.model.objects.select_related("vehicle"), pk=pk)
        action = ApprovalActionSerializer(data=request.data)
        action.is_valid(raise_exception=True)

        entry.approval_status = action.validated_data["approval_status"]
        entry.rejection_reason = action.validated_data.get("rejection_reason", "")
        entry.approved_by = request.user
        entry.approved_at = timezone.now()
        entry.updated_by = request.user
        entry.save(
            update_fields=[
                "approval_status",
                "rejection_reason",
                "approved_by",
                "approved_at",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(self.read_serializer(entry).data)


class FuelEntryListCreateAPI(_EntryListCreateAPI):
    model = FuelEntry
    read_serializer = FuelEntrySerializer
    write_serializer = FuelEntryWriteSerializer
    search_fields = ("vehicle__vehicle_number", "vehicle__nickname", "station_name", "bill_number")

    def after_write(self, entry):
        services.recalculate_fuel_metrics(entry.vehicle)


class FuelEntryDetailAPI(_EntryDetailAPI):
    model = FuelEntry
    read_serializer = FuelEntrySerializer
    write_serializer = FuelEntryWriteSerializer

    def after_write(self, vehicle):
        services.recalculate_fuel_metrics(vehicle)


class FuelEntryApprovalAPI(_EntryApprovalAPI):
    model = FuelEntry
    read_serializer = FuelEntrySerializer


class ServiceEntryListCreateAPI(_EntryListCreateAPI):
    model = ServiceEntry
    read_serializer = ServiceEntrySerializer
    write_serializer = ServiceEntryWriteSerializer
    search_fields = ("vehicle__vehicle_number", "vehicle__nickname", "workshop_name", "bill_number")


class ServiceEntryDetailAPI(_EntryDetailAPI):
    model = ServiceEntry
    read_serializer = ServiceEntrySerializer
    write_serializer = ServiceEntryWriteSerializer


class ServiceEntryApprovalAPI(_EntryApprovalAPI):
    model = ServiceEntry
    read_serializer = ServiceEntrySerializer


class PendingApprovalsAPI(APIView):
    """Both registers' pending bills in one list, oldest first.

    One endpoint rather than two so the approvals screen is a single queue: an
    approver does not think in terms of which table a bill came from.
    """

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        fuel = FuelEntry.objects.filter(approval_status=ApprovalStatus.PENDING).select_related(
            "vehicle", "created_by"
        )
        service = ServiceEntry.objects.filter(
            approval_status=ApprovalStatus.PENDING
        ).select_related("vehicle", "created_by")
        if request.query_params.get("vehicle"):
            fuel = fuel.filter(vehicle_id=request.query_params["vehicle"])
            service = service.filter(vehicle_id=request.query_params["vehicle"])
        return Response(
            {
                "fuel": FuelEntrySerializer(fuel.order_by("entry_date", "id"), many=True).data,
                "service": ServiceEntrySerializer(
                    service.order_by("entry_date", "id"), many=True
                ).data,
            }
        )


# ---------------------------------------------------------------- documents


class VehicleDocumentListCreateAPI(generics.ListCreateAPIView):
    """GET documents (filter by ``vehicle``, ``doc_type``), POST a new one."""

    permission_classes = [IsAuthenticated, FleetVehiclePermission]
    parser_classes = UPLOAD_PARSERS
    serializer_class = VehicleDocumentSerializer

    def get_queryset(self):
        queryset = VehicleDocument.objects.filter(is_active=True).select_related("vehicle")
        params = self.request.query_params
        if params.get("vehicle"):
            queryset = queryset.filter(vehicle_id=params["vehicle"])
        if params.get("doc_type"):
            queryset = queryset.filter(doc_type=params["doc_type"])
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)


class VehicleDocumentDetailAPI(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, FleetVehiclePermission]
    parser_classes = UPLOAD_PARSERS
    serializer_class = VehicleDocumentSerializer
    queryset = VehicleDocument.objects.select_related("vehicle")

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.updated_by = self.request.user
        instance.save(update_fields=["is_active", "updated_by", "updated_at"])


class ExpiringDocumentsAPI(APIView):
    """Papers already out of date or about to be, soonest first."""

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        try:
            within = int(request.query_params.get("days", DOCUMENT_WARNING_DAYS))
        except ValueError:
            within = DOCUMENT_WARNING_DAYS
        documents = services.expiring_documents(within)
        return Response(
            {
                "days": within,
                "rows": VehicleDocumentSerializer(documents, many=True).data,
            }
        )


# ------------------------------------------------------------------ reports


class FleetSummaryAPI(APIView):
    """The dashboard: this month's spend, what is pending, what is expiring."""

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        return Response(services.fleet_summary())


class FleetCostReportAPI(APIView):
    """One row per vehicle over ``from``/``to`` — the monthly cost sheet."""

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        date_from = _date(request, "from")
        date_to = _date(request, "to")
        rows = services.fleet_cost_rows(date_from, date_to)
        return Response(
            {
                "from": date_from,
                "to": date_to,
                "rows": rows,
                "totals": {
                    "fuel_cost": sum((row["fuel_cost"] for row in rows), 0),
                    "service_cost": sum((row["service_cost"] for row in rows), 0),
                    "total_cost": sum((row["total_cost"] for row in rows), 0),
                },
            }
        )
