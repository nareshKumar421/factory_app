"""The company vehicle register's API.

Shape of it: a vehicle master with CRUD, two money registers -- of which only
the service one has an approval endpoint -- a document register read mostly by
expiry, and the handful of read-only endpoints the dashboard and the cost
report need.

Every write to a fuel entry -- create, edit or delete -- ends by recomputing
that vehicle's mileage chain, because all three move the row its neighbours
are measured against.
"""

from __future__ import annotations

import os
from datetime import datetime

from django.db.models import Q
from django.http import FileResponse, Http404
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
from .models import DailyReading, FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument
from .permissions import (
    ADD_EXPENSE_PERMISSION,
    CanAddFleetExpense,
    APPROVE_EXPENSE_PERMISSION,
    MANAGE_VEHICLE_PERMISSION,
    CanApproveFleetExpense,
    CanViewFleet,
    FleetExpensePermission,
    FleetVehiclePermission,
)
from .serializers import (
    ApprovalActionSerializer,
    DailyReadingSerializer,
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


#: Every stored file in the module, by the slug its URL uses. Kept in one
#: place so the serializers and the endpoint cannot name different things.
FILE_SOURCES = {
    "fuel": (FuelEntry, "bill_photo"),
    "service": (ServiceEntry, "bill_photo"),
    "document": (VehicleDocument, "file"),
    "vehicle": (FleetVehicle, "photo"),
}


class FleetAttachmentAPI(APIView):
    """Stream one bill photo, scan or vehicle photo.

    Served through here rather than linked at its ``/media/`` path, the way
    ``artwork`` and ``quality_control`` serve theirs: the endpoint is
    permission-checked, so the request has to carry the auth header, and a
    plain media URL would hand a fuel bill or an insurance policy to anybody
    who guessed the path. It is also the only way the link works when the page
    and the API are not on the same host.
    """

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request, kind, pk):
        source = FILE_SOURCES.get(kind)
        if not source:
            raise Http404("No such attachment")
        model, field = source
        stored = getattr(get_object_or_404(model, pk=pk), field)
        if not stored:
            raise Http404("Nothing filed here")
        return FileResponse(
            stored.open("rb"), as_attachment=False, filename=os.path.basename(stored.name)
        )


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
    #: False for a register with no approval column -- fuel.
    is_approvable = True

    def get_serializer_class(self):
        return self.write_serializer if self.request.method == "POST" else self.read_serializer

    def get_queryset(self):
        params = self.request.query_params
        related = ["vehicle", "created_by"] + (["approved_by"] if self.is_approvable else [])
        queryset = self.model.objects.select_related(*related)
        if params.get("vehicle"):
            queryset = queryset.filter(vehicle_id=params["vehicle"])
        if self.is_approvable and params.get("approval_status"):
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

    An APPROVED entry is frozen: correcting it means sending it back first, so
    a figure somebody passed cannot quietly change afterwards. A fuel entry has
    no approval, so it stays editable -- ``getattr`` below is what makes the
    same class serve both.
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
        if getattr(entry, "approval_status", None) == ApprovalStatus.APPROVED:
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
        if getattr(entry, "approval_status", None) == ApprovalStatus.APPROVED:
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
    is_approvable = False
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
    """Workshop bills waiting to be passed, oldest first.

    Fuel is absent by design: a filling has no approval. The response keeps its
    ``fuel`` key, always empty, so a client that still reads it does not break.
    """

    permission_classes = [IsAuthenticated, CanViewFleet]

    def get(self, request):
        service = ServiceEntry.objects.filter(
            approval_status=ApprovalStatus.PENDING
        ).select_related("vehicle", "created_by")
        if request.query_params.get("vehicle"):
            service = service.filter(vehicle_id=request.query_params["vehicle"])
        return Response(
            {
                "fuel": [],
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


# ------------------------------------------------------------ running log


class DailyReadingListCreateAPI(generics.ListCreateAPIView):
    """GET the readings (filter by ``vehicle``, ``from``, ``to``), POST one.

    POST is an upsert on (vehicle, date): re-entering a day overwrites it,
    which is what someone correcting a typo expects, and it keeps the unique
    constraint from turning a correction into an error page.
    """

    permission_classes = [IsAuthenticated, FleetExpensePermission]
    serializer_class = DailyReadingSerializer

    def get_queryset(self):
        queryset = DailyReading.objects.select_related("vehicle", "created_by")
        params = self.request.query_params
        if params.get("vehicle"):
            queryset = queryset.filter(vehicle_id=params["vehicle"])
        date_from = _date(self.request, "from")
        date_to = _date(self.request, "to")
        if date_from:
            queryset = queryset.filter(reading_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(reading_date__lte=date_to)
        return queryset

    def create(self, request, *args, **kwargs):
        existing = DailyReading.objects.filter(
            vehicle_id=request.data.get("vehicle"),
            reading_date=request.data.get("reading_date"),
        ).first()
        serializer = self.get_serializer(existing, data=request.data, partial=bool(existing))
        serializer.is_valid(raise_exception=True)
        if existing:
            reading = serializer.save(updated_by=request.user)
            code = status.HTTP_200_OK
        else:
            reading = serializer.save(created_by=request.user, updated_by=request.user)
            code = status.HTTP_201_CREATED
        return Response(DailyReadingSerializer(reading).data, status=code)


class DailyReadingDetailAPI(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, FleetExpensePermission]
    serializer_class = DailyReadingSerializer
    queryset = DailyReading.objects.select_related("vehicle")

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class RunningLogAPI(APIView):
    """The day-wise log.

    With ``vehicle``: one row per day for that vehicle over ``from``/``to`` --
    meter, distance, fuel and cost, gaps included. Without: one row per
    vehicle, which is the "who ran how much" answer for the whole fleet.

    The window defaults to this month. It is capped at 366 days, because the
    day-wise shape means a row per day and nobody reads ten thousand of them.
    """

    permission_classes = [IsAuthenticated, CanViewFleet]
    MAX_DAYS = 366

    def get(self, request):
        today = timezone.localdate()
        date_from = _date(request, "from") or today.replace(day=1)
        date_to = _date(request, "to") or today
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        if (date_to - date_from).days + 1 > self.MAX_DAYS:
            return Response(
                {"detail": f"Ask for at most {self.MAX_DAYS} days at a time."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if request.query_params.get("vehicle"):
            vehicle = get_object_or_404(FleetVehicle, pk=request.query_params["vehicle"])
            log = services.running_log(vehicle, date_from, date_to)
            return Response(
                {
                    "from": date_from,
                    "to": date_to,
                    "vehicle": FleetVehicleSerializer(vehicle).data,
                    "rows": log["rows"],
                    "totals": log["totals"],
                }
            )

        return Response(
            {
                "from": date_from,
                "to": date_to,
                "vehicle": None,
                "rows": services.running_log_by_vehicle(date_from, date_to),
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
