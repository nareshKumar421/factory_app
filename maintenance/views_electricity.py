"""Daily Electricity++'s API: meters in the tree, readings, the split.

The Daily Electricity page keeps its own endpoints in ``maintenance.views`` and
works exactly as it always has; these serve the same meters and readings to
Daily Electricity++, which adds the tree, holds readings to a chain and works
out who pays.

Factory-wide, not company-scoped: one campus, one set of meters, whichever
company the viewer is signed into. Who may *change* what is split three ways,
and the three are deliberately separate rights:

* a meter's **keeper** (``meter_scope``) records its readings and tunes the
  hardware facts — multiplying factor, number, location;
* whoever holds **can_manage_electricity_allocation** places meters in the tree
  and decides who pays for each one's units, for every meter on the campus;
* anyone on the register may **read** all of it, the split included.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db import transaction
from django.db.models import Count, OuterRef, ProtectedError, Q, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from blowing.models import BlowingMachine
from production_execution.models import ProductionLine

from . import meter_scope
from .electricity import service
from .electricity.setup import delete_setup
from .models import (
    DailyElectricityReading,
    ElectricityAllocationBasis,
    ElectricityMeter,
    ElectricityMeterSetup,
)
from .models_manager import UserElectricityMeter
from .permissions import (
    CanAddDailyElectricity,
    CanEditDailyElectricity,
    CanManageElectricityAllocation,
    CanViewDailyElectricity,
    CanViewElectricityMeter,
)
from .serializers_electricity import (
    DaySheetSaveSerializer,
    ElectricityMeterSetupSerializer,
    TreeMeterSerializer,
    TreeReadingSerializer,
    meter_rate_per_unit,
    next_reading,
    previous_reading,
    relink_next,
)
from .views import (
    DailyElectricityPermissionMixin,
    ElectricityMeterPermissionMixin,
    _attributed_to,
)

#: The longest span the split is worked out for in one request. A year of
#: days is still a few hundred milliseconds; beyond it is a report job.
MAX_SPAN_DAYS = 366


def _bool_param(value):
    return str(value).lower() in {"1", "true", "yes"}


def _date_param(request, name, default):
    raw = request.query_params.get(name)
    if not raw:
        return default
    parsed = parse_date(raw)
    if parsed is None:
        raise ValidationError({name: "Use a date like 2026-09-24."})
    return parsed


def _can(user, permission_class) -> bool:
    """The same any-of test the permission class applies to a request."""
    return any(user.has_perm(permission) for permission in permission_class.permissions)


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


class ElectricityTreeMeterViewSet(ElectricityMeterPermissionMixin, viewsets.ModelViewSet):
    """The meters, listed in tree order as the tree stands on ``?date=`` (today).

    Each row carries ``tree``: whether the meter is in the tree that day, its
    depth and parent, and the setup version in force.
    """

    serializer_class = TreeMeterSerializer

    def tree_date(self) -> date:
        return _date_param(self.request, "date", timezone.localdate())

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Worked out on first use — after the save, on a create or an update —
        # and then shared by every row of the response.
        context["tree_date"] = self.tree_date()
        return context

    def get_queryset(self):
        latest = DailyElectricityReading.objects.filter(meter=OuterRef("pk"), is_active=True).order_by(
            "-date"
        )
        qs = ElectricityMeter.objects.select_related("register_of").prefetch_related(
            "companies", "consumers"
        ).annotate(
            last_reading_date=Subquery(latest.values("date")[:1]),
            last_closing_reading=Subquery(latest.values("closing_reading")[:1]),
            # distinct: the company filter below joins the companies M2M, which
            # would otherwise multiply the counted reading rows.
            readings_count=Count("daily_readings", distinct=True),
        )
        params = self.request.query_params
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(meter_number__icontains=search)
                | Q(location__icontains=search)
            )
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        is_main = params.get("is_main")
        if is_main is not None:
            qs = qs.filter(is_main=_bool_param(is_main))
        supply_source = params.get("supply_source")
        if supply_source:
            qs = qs.filter(supply_source=supply_source.upper())
        company = params.get("company")
        if company:
            qs = qs.filter(Q(companies__code=company) | Q(consumers__code=company)).distinct()
        return qs.order_by("name")

    def list(self, request, *args, **kwargs):
        meters = list(self.filter_queryset(self.get_queryset()))
        context = self.get_serializer_context()
        tree = context["tree"] = service.meter_tree(context["tree_date"])
        in_service = request.query_params.get("in_service")
        if in_service is not None:
            wanted = _bool_param(in_service)
            meters = [m for m in meters if tree.get(m.id, {}).get("in_service", False) == wanted]
        meters.sort(key=lambda m: (tree.get(m.id, {}).get("order", 10**6), m.name.lower()))
        return Response(self.get_serializer(meters, many=True, context=context).data)

    def perform_create(self, serializer):
        # Creating is NOT scoped, and cannot be: a meter that does not exist yet
        # has no manager to check against. The permission alone gates it, and
        # the creator is made its first manager below so the meter is never born
        # unkept — which would leave it uneditable by everyone but a superuser.
        placement = serializer.validated_data.get("placement") or {}
        decides_split = (
            placement.get("basis") not in (None, ElectricityAllocationBasis.UNASSIGNED)
            or placement.get("shares")
            or placement.get("drivers")
        )
        if decides_split and not _can(self.request.user, CanManageElectricityAllocation):
            raise PermissionDenied(
                "You can add the meter and say where it sits, but deciding who pays "
                "for it needs the 'set who pays for each electricity meter' right."
            )
        meter = serializer.save(created_by=self.request.user, updated_by=self.request.user)
        if not self.request.user.is_superuser:
            UserElectricityMeter.objects.get_or_create(
                user=self.request.user,
                meter=meter,
                defaults={"created_by": self.request.user, "updated_by": self.request.user},
            )

    def perform_update(self, serializer):
        meter_scope.assert_can_edit_meter(self.request.user, serializer.instance)
        serializer.save(updated_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        meter = self.get_object()
        meter_scope.assert_can_edit_meter(request.user, meter)
        if meter.daily_readings.exists():
            return Response(
                {"detail": "This meter has readings and cannot be deleted. Take it out of service instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {
                    "detail": (
                        "Other meters sit under this one, follow its reading, or read "
                        "as its second register. Move them first."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


class ElectricityMeterSetupViewSet(viewsets.ModelViewSet):
    """A meter's setup versions — where it sits and who pays, from each date on.

    ``POST`` adds a version (a change on the floor from a date); ``PATCH`` a
    version corrects it (every day it covers is worked out again). The split is
    computed from the readings each time it is asked for, never stored, so both
    take effect at once.
    """

    serializer_class = ElectricityMeterSetupSerializer

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsAuthenticated(), CanManageElectricityAllocation()]
        return [IsAuthenticated(), CanViewElectricityMeter()]

    def get_queryset(self):
        qs = (
            ElectricityMeterSetup.objects.filter(is_active=True)
            .select_related("meter", "parent", "created_by", "updated_by")
            .prefetch_related(
                "shares__company",
                "shares__consumer",
                "drivers__production_line__company",
                "drivers__blowing_machine__company",
                "drivers__meter",
                "drivers__company",
            )
        )
        meter = self.request.query_params.get("meter")
        if meter:
            qs = qs.filter(meter_id=meter)
        return qs.order_by("meter__name", "-effective_from")

    def perform_destroy(self, instance):
        delete_setup(instance)


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


class ElectricityTreeReadingViewSet(DailyElectricityPermissionMixin, viewsets.ModelViewSet):
    serializer_class = TreeReadingSerializer

    def get_queryset(self):
        qs = DailyElectricityReading.objects.select_related("meter", "created_by").prefetch_related(
            "companies", "consumers", "meter__companies", "meter__consumers"
        )
        params = self.request.query_params
        day = params.get("date")
        if day:
            qs = qs.filter(date=day)
        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(date__lte=date_to)
        meter = params.get("meter")
        if meter:
            qs = qs.filter(meter_id=meter)
        is_main = params.get("is_main")
        if is_main is not None:
            qs = qs.filter(meter__is_main=_bool_param(is_main))
        supply_source = params.get("supply_source")
        if supply_source:
            qs = qs.filter(meter__supply_source=supply_source.upper())
        company = params.get("company")
        if company:
            qs = qs.filter(_attributed_to(company)).distinct()
        return qs.order_by("-date", "meter__name")

    def perform_create(self, serializer):
        meter_scope.assert_can_record_for(self.request.user, [serializer.validated_data.get("meter")])
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        meter_scope.assert_can_record_for(self.request.user, [serializer.instance.meter])
        serializer.save(updated_by=self.request.user)

    @transaction.atomic
    def perform_destroy(self, instance):
        meter_scope.assert_can_record_for(self.request.user, [instance.meter])
        # The next reading took over from this one's closing. With this one
        # gone it takes over from where this one started, so the days this
        # reading covered pass to it instead of dropping out of the register.
        following = next_reading(instance.meter, instance.date, exclude_pk=instance.pk)
        relink_next(following, instance.closing_reading, instance.opening_reading, user=self.request.user)
        instance.delete()


class ElectricityDaySheetAPI(APIView):
    """One day's readings for every meter, in tree order — read and saved together.

    ``GET ?date=`` lists every meter in the tree that day with its previous
    closing and the day's reading if there is one. ``POST`` saves a set of
    closings in one go: all of them or none, so a half-saved round never leaves
    the parents and their sub-meters out of step.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated()]
        return [IsAuthenticated(), CanViewDailyElectricity()]

    def get(self, request):
        day = _date_param(request, "date", timezone.localdate())
        return Response(self._sheet(request.user, day))

    def _sheet(self, user, day: date) -> dict:
        tree = service.meter_tree(day)
        in_tree = [mid for mid, info in tree.items() if info["in_service"]]
        meters = {m.id: m for m in ElectricityMeter.objects.filter(pk__in=in_tree).prefetch_related("companies")}
        todays = {
            row.meter_id: row
            for row in DailyElectricityReading.objects.filter(date=day, meter_id__in=in_tree, is_active=True)
        }
        window = timedelta(days=62)
        before, after = {}, {}
        for row in DailyElectricityReading.objects.filter(
            meter_id__in=in_tree, date__gte=day - window, date__lte=day + window, is_active=True
        ).order_by("date"):
            if row.date < day:
                before[row.meter_id] = row
            elif row.date > day and row.meter_id not in after:
                after[row.meter_id] = row
        for meter_id in in_tree:
            if meter_id not in before and meter_id in meters:
                found = previous_reading(meters[meter_id], day)
                if found is not None:
                    before[meter_id] = found

        rows = []
        for meter_id in sorted(in_tree, key=lambda mid: tree[mid]["order"]):
            meter = meters.get(meter_id)
            if meter is None:
                continue
            info = tree[meter_id]
            setup_row = info.get("setup")
            reading = todays.get(meter_id)
            prev = before.get(meter_id)
            nxt = after.get(meter_id)
            rows.append(
                {
                    "meter": meter.id,
                    "name": meter.name,
                    "location": meter.location,
                    "depth": info["depth"],
                    "parent": info["parent_id"],
                    "parent_name": info.get("parent_name"),
                    "is_register": info.get("register_of") is not None,
                    "register_of": info.get("register_of"),
                    "multiplying_factor": str(meter.multiplying_factor),
                    "rate_per_unit": str(meter_rate_per_unit(meter, as_of=day)),
                    "keeps": meter_scope.manages(user, meter),
                    "split": service.describe_setup(setup_row)["summary"] if setup_row else None,
                    "previous": {"date": prev.date, "closing_reading": str(prev.closing_reading)}
                    if prev
                    else None,
                    "reading": {
                        "id": reading.id,
                        "opening_reading": str(reading.opening_reading),
                        "closing_reading": str(reading.closing_reading),
                        "units_consumed": str(reading.units_consumed),
                        "meter_reset": reading.meter_reset,
                        "reading_time": reading.reading_time,
                        "remarks": reading.remarks,
                    }
                    if reading
                    else None,
                    "next": {"date": nxt.date, "opening_reading": str(nxt.opening_reading)}
                    if nxt
                    else None,
                }
            )
        return {"date": day, "rows": rows}

    def post(self, request):
        payload = DaySheetSaveSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        day = payload.validated_data["date"]
        entries = payload.validated_data["entries"]
        if not entries:
            raise ValidationError({"entries": "Nothing to save."})

        existing = {
            row.meter_id: row
            for row in DailyElectricityReading.objects.filter(
                date=day, meter__in=[entry["meter"] for entry in entries]
            )
        }
        creating = [entry for entry in entries if entry["meter"].pk not in existing]
        updating = [entry for entry in entries if entry["meter"].pk in existing]
        if creating and not _can(request.user, CanAddDailyElectricity):
            raise PermissionDenied("You may not record new readings.")
        if updating and not _can(request.user, CanEditDailyElectricity):
            raise PermissionDenied("You may not correct readings already entered for this day.")
        meter_scope.assert_can_record_for(request.user, [entry["meter"] for entry in entries])

        serializers_by_meter = {}
        errors = {}
        for entry in entries:
            meter = entry["meter"]
            data = {
                "meter": meter.pk,
                "date": day,
                "closing_reading": entry["closing_reading"],
                "meter_reset": entry.get("meter_reset", False),
                "remarks": entry.get("remarks", ""),
            }
            if entry.get("opening_reading") is not None:
                data["opening_reading"] = entry["opening_reading"]
            if entry.get("reading_time"):
                data["reading_time"] = entry["reading_time"]
            instance = existing.get(meter.pk)
            if instance is not None:
                data.pop("meter")
                data.pop("date")
            serializer = TreeReadingSerializer(
                instance=instance,
                data=data,
                partial=instance is not None,
                context={"request": request},
            )
            if serializer.is_valid():
                serializers_by_meter[meter.pk] = serializer
            else:
                errors[meter.pk] = serializer.errors
        if errors:
            return Response(
                {"detail": "Nothing was saved. Fix the rows marked below.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            for meter_id, serializer in serializers_by_meter.items():
                if meter_id in existing:
                    serializer.save(updated_by=request.user)
                else:
                    serializer.save(created_by=request.user, updated_by=request.user)
        return Response(
            {
                "created": len(creating),
                "updated": len(updating),
                "sheet": self._sheet(request.user, day),
            },
            status=status.HTTP_200_OK,
        )


class ElectricityAllocationAPI(APIView):
    """Who used how many units, and what they cost, between two dates.

    ``?date_from=&date_to=`` — defaults to the month so far. The whole answer is
    worked out from the readings and the setups each time; see
    ``maintenance.electricity``.
    """

    permission_classes = [IsAuthenticated, CanViewDailyElectricity]

    def get(self, request):
        today = timezone.localdate()
        date_to = _date_param(request, "date_to", today)
        date_from = _date_param(request, "date_from", date_to.replace(day=1))
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        if (date_to - date_from).days + 1 > MAX_SPAN_DAYS:
            raise ValidationError({"date_from": f"Pick at most {MAX_SPAN_DAYS} days at a time."})
        return Response(service.report(date_from, date_to))


class ElectricityRunSourcesAPI(APIView):
    """The production lines and blowing machines a run-hours split can follow."""

    permission_classes = [IsAuthenticated, CanViewElectricityMeter]

    def get(self, request):
        rows = []
        for line in ProductionLine.objects.select_related("company").order_by("company__name", "name"):
            rows.append(
                {
                    "kind": "LINE",
                    "id": line.id,
                    "name": line.name,
                    "company": line.company.code,
                    "company_name": line.company.name,
                    "is_active": line.is_active,
                }
            )
        for machine in BlowingMachine.objects.select_related("company").order_by("company__name", "name"):
            rows.append(
                {
                    "kind": "BLOWING_MACHINE",
                    "id": machine.id,
                    "name": machine.name,
                    "company": machine.company.code,
                    "company_name": machine.company.name,
                    "is_active": machine.is_active,
                }
            )
        return Response(rows)
