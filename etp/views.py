"""
API for the ETP / STP registers.

Every register list accepts the same filters — ``plant``, ``date``,
``date_from``, ``date_to``, ``company`` — so one set of screen controls drives
all six. ``company`` matches the companies a plant is tagged with (the Ganaur
plants serve the shared campus), and an untagged plant drops out of a
company-filtered view exactly as an untagged electricity meter does.

Two read-only endpoints sit on top of the registers:

* ``GET /etp/dashboard/``  — what is still unfilled today, per plant.
* ``GET /etp/summary/``    — period totals: the footer line of the paper form.
"""

from datetime import date as date_cls
from decimal import Decimal

from django.db.models import Count, OuterRef, ProtectedError, Q, Subquery, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services as etp_services
from .constants import (
    ChangeAction,
    MonitoringStage,
    OptionCategory,
    PrintDocumentKey,
    RegisterKey,
)
from .models import (
    BackwashEntry,
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationRecord,
    ChemicalConsumptionLog,
    DailyPlantLog,
    EtpPrintDocument,
    MonitoringParameter,
    MonitoringRecord,
    PlantChemical,
    PlantOption,
    PlantStaff,
    RegisterChangeLog,
    SludgeGenerationEntry,
    TreatmentPlant,
)
from .permissions import (
    BackwashPermission,
    CalibrationPermission,
    CanVerifyMonitoring,
    ChemicalPermission,
    DailyLogPermission,
    MasterPermission,
    MonitoringPermission,
    SludgePermission,
)
from .serializers import (
    BackwashEntrySerializer,
    BackwashEquipmentSerializer,
    CalibrationInstrumentSerializer,
    CalibrationRecordSerializer,
    ChemicalConsumptionLogSerializer,
    DailyPlantLogSerializer,
    EtpPrintDocumentSerializer,
    MonitoringParameterSerializer,
    MonitoringRecordSerializer,
    PlantChemicalSerializer,
    PlantOptionSerializer,
    PlantStaffSerializer,
    RegisterChangeLogSerializer,
    SludgeGenerationEntrySerializer,
    TreatmentPlantSerializer,
)


def _bool_param(value):
    return str(value).lower() in {"1", "true", "yes"}


class AuthorStampMixin:
    """Record who created / last touched a row."""

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class RegisterAuditMixin(AuthorStampMixin):
    """Stamp the author AND append the change to the register's edit trail.

    A register row stays editable indefinitely (a correction may surface at the
    month-end review), so instead of locking it the module keeps every version
    attributable: the before/after snapshot around each write becomes one line of
    :class:`~etp.models.RegisterChangeLog`.
    """

    #: Which register this viewset writes (a ``RegisterKey``).
    audit_register = ""

    def audit_extra(self, instance):
        """Child rows folded into the snapshot. Overridden by the grid registers."""
        return {}

    def audit_plant(self, instance):
        return getattr(instance, "plant", None)

    def audit_date(self, instance):
        return getattr(instance, "date", None)

    def audit_snapshot(self, instance):
        return etp_services.snapshot(instance, self.audit_extra(instance))

    def log_change(self, instance, action, before=None, after=None, object_id=None):
        etp_services.record_change(
            register=self.audit_register,
            action=action,
            instance=instance,
            before=before,
            after=after,
            user=self.request.user,
            plant=self.audit_plant(instance),
            entry_date=self.audit_date(instance),
            object_id=object_id,
        )

    def perform_create(self, serializer):
        super().perform_create(serializer)
        self.log_change(serializer.instance, ChangeAction.CREATED)

    def perform_update(self, serializer):
        # `serializer.instance` still holds the stored values here — DRF applies
        # the payload during save() — so this is the genuine "before".
        before = self.audit_snapshot(serializer.instance)
        super().perform_update(serializer)
        updated = serializer.instance
        updated.refresh_from_db()
        self.log_change(
            updated, ChangeAction.UPDATED, before, self.audit_snapshot(updated)
        )

    def perform_destroy(self, instance):
        # Everything the trail needs has to be read before the row goes.
        object_id = instance.pk
        plant = self.audit_plant(instance)
        entry_date = self.audit_date(instance)
        super().perform_destroy(instance)
        etp_services.record_change(
            register=self.audit_register,
            action=ChangeAction.DELETED,
            instance=instance,
            user=self.request.user,
            plant=plant,
            entry_date=entry_date,
            object_id=object_id,
        )


class RegisterFilterMixin:
    """Shared ``plant`` / date-window / ``company`` filtering for the registers."""

    #: Lookup from the queryset's model to the plant (overridden where nested).
    plant_field = "plant"
    date_field = "date"

    def filter_register(self, qs):
        params = self.request.query_params
        plant = params.get("plant")
        if plant:
            qs = qs.filter(**{f"{self.plant_field}_id": plant})
        plant_type = params.get("plant_type")
        if plant_type:
            qs = qs.filter(**{f"{self.plant_field}__plant_type": plant_type})
        date = params.get("date")
        if date:
            qs = qs.filter(**{self.date_field: date})
        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(**{f"{self.date_field}__gte": date_from})
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(**{f"{self.date_field}__lte": date_to})
        company = params.get("company")
        if company:
            qs = qs.filter(
                **{f"{self.plant_field}__companies__code": company}
            ).distinct()
        return qs


def _protected_response(name):
    return Response(
        {
            "detail": (
                f"This {name} is already used by a filed entry and cannot be "
                f"deleted. Mark it inactive instead."
            )
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


class MasterViewSetMixin(AuthorStampMixin):
    """Masters share their permission class and a delete that refuses to orphan."""

    permission_classes = [IsAuthenticated, MasterPermission]
    protected_name = "record"

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return _protected_response(self.protected_name)


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------


class TreatmentPlantViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = TreatmentPlantSerializer
    protected_name = "plant"

    def get_queryset(self):
        qs = TreatmentPlant.objects.prefetch_related("companies")
        params = self.request.query_params
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        plant_type = params.get("plant_type")
        if plant_type:
            qs = qs.filter(plant_type=plant_type)
        company = params.get("company")
        if company:
            qs = qs.filter(companies__code=company).distinct()
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(location__icontains=search)
            )
        return qs.order_by("sequence", "name")


class PlantStaffViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = PlantStaffSerializer
    protected_name = "person"

    def get_queryset(self):
        qs = PlantStaff.objects.prefetch_related("plants")
        params = self.request.query_params
        role = params.get("role")
        if role:
            qs = qs.filter(role=role)
        plant = params.get("plant")
        if plant:
            # No plant tag means "works on every plant".
            qs = qs.filter(Q(plants__id=plant) | Q(plants__isnull=True)).distinct()
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("sequence", "name")


class PlantOptionViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = PlantOptionSerializer
    protected_name = "option"

    def get_queryset(self):
        qs = PlantOption.objects.all()
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("category", "sequence", "label")

    @action(detail=False, methods=["get"], url_path="categories")
    def categories(self, request):
        """The dropdowns that exist, for the Settings screen's tabs."""
        return Response(
            [
                {"value": value, "label": label}
                for value, label in OptionCategory.choices
            ]
        )


class PlantChemicalViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = PlantChemicalSerializer
    protected_name = "chemical"

    def get_queryset(self):
        qs = PlantChemical.objects.prefetch_related("plants")
        params = self.request.query_params
        plant = params.get("plant")
        if plant:
            # An untagged chemical is offered on every plant.
            qs = qs.filter(Q(plants__id=plant) | Q(plants__isnull=True)).distinct()
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("sequence", "name")


class BackwashEquipmentViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = BackwashEquipmentSerializer
    protected_name = "equipment"

    def get_queryset(self):
        qs = BackwashEquipment.objects.select_related("plant", "default_chemical")
        params = self.request.query_params
        plant = params.get("plant")
        if plant:
            qs = qs.filter(plant_id=plant)
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("plant__sequence", "sequence", "name")


class MonitoringParameterViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    serializer_class = MonitoringParameterSerializer
    protected_name = "parameter"

    def get_queryset(self):
        qs = MonitoringParameter.objects.select_related("plant")
        params = self.request.query_params
        plant = params.get("plant")
        if plant:
            qs = qs.filter(plant_id=plant)
        stage = params.get("stage")
        if stage:
            qs = qs.filter(stage=stage)
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("plant__sequence", "stage", "sequence", "parameter_name")

    @action(detail=False, methods=["get"], url_path="stages")
    def stages(self, request):
        return Response(
            [{"value": value, "label": label} for value, label in MonitoringStage.choices]
        )


class EtpPrintDocumentViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    """The document numbers printed on the registers — held in the database.

    Read by every print (so a corrected code reaches the paper immediately) and
    written from the Settings screen, which is why it sits behind the same
    permission as the other masters rather than a developer's editor.
    """

    serializer_class = EtpPrintDocumentSerializer
    protected_name = "print document"

    def get_queryset(self):
        qs = EtpPrintDocument.objects.select_related("company")
        params = self.request.query_params
        key = params.get("document_key")
        if key:
            qs = qs.filter(document_key=key)
        company = params.get("company")
        if company:
            # The company's own row plus the factory-wide default it overrides.
            qs = qs.filter(Q(company__code=company) | Q(company__isnull=True))
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("document_key", "company__code")

    @action(detail=False, methods=["get"], url_path="keys")
    def keys(self, request):
        """The printable forms, for the Settings screen's picker."""
        return Response(
            [
                {"value": value, "label": label}
                for value, label in PrintDocumentKey.choices
            ]
        )


class CalibrationInstrumentViewSet(MasterViewSetMixin, viewsets.ModelViewSet):
    """Instrument master. Annotates the last calibration and what it fell due on."""

    serializer_class = CalibrationInstrumentSerializer
    protected_name = "instrument"

    def get_queryset(self):
        latest = CalibrationRecord.objects.filter(instrument=OuterRef("pk")).order_by(
            "-date", "-time"
        )
        qs = (
            CalibrationInstrument.objects.select_related("plant")
            .prefetch_related("points")
            .annotate(
                last_calibration_date=Subquery(latest.values("date")[:1]),
                calibration_due_date=Subquery(latest.values("due_date")[:1]),
                records_count=Count("records", distinct=True),
            )
        )
        params = self.request.query_params
        plant = params.get("plant")
        if plant:
            qs = qs.filter(plant_id=plant)
        is_active = params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=_bool_param(is_active))
        return qs.order_by("sequence", "equipment_name")


# ---------------------------------------------------------------------------
# Register 1 — daily plant log
# ---------------------------------------------------------------------------


class DailyPlantLogViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = DailyPlantLogSerializer
    permission_classes = [IsAuthenticated, DailyLogPermission]
    audit_register = RegisterKey.DAILY_LOG

    def get_queryset(self):
        qs = DailyPlantLog.objects.select_related(
            "plant", "operator", "chemist", "created_by"
        )
        return self.filter_register(qs).order_by("-date", "plant__sequence")

    @action(detail=False, methods=["get"], url_path="last-readings")
    def last_readings(self, request):
        """Yesterday's closing figures for a plant, to prefill today's openings."""
        plant = request.query_params.get("plant")
        if not plant:
            return Response(
                {"detail": "Pass ?plant=<id>."}, status=status.HTTP_400_BAD_REQUEST
            )
        before = request.query_params.get("before")
        qs = DailyPlantLog.objects.filter(plant_id=plant)
        if before:
            qs = qs.filter(date__lt=before)
        previous = qs.order_by("-date").first()
        if previous is None:
            return Response({"found": False})
        return Response(
            {
                "found": True,
                "date": previous.date,
                "inlet_final": previous.inlet_final,
                "outlet_final": previous.outlet_final,
                "energy_final": previous.energy_final,
            }
        )


# ---------------------------------------------------------------------------
# Register 2 — on-line monitoring
# ---------------------------------------------------------------------------


class MonitoringRecordViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = MonitoringRecordSerializer
    permission_classes = [IsAuthenticated, MonitoringPermission]
    audit_register = RegisterKey.MONITORING

    def audit_extra(self, instance):
        # The grid is replaced on every save, so it is audited as one value.
        return {"readings": etp_services.describe_monitoring_readings(instance)}

    def get_queryset(self):
        qs = MonitoringRecord.objects.select_related(
            "plant", "chemist", "verified_by", "created_by"
        ).prefetch_related(
            "readings__operator",
            "readings__values__parameter",
        )
        return self.filter_register(qs).order_by("-date", "plant__sequence")

    @action(detail=False, methods=["get"], url_path="sheet-template")
    def sheet_template(self, request):
        """The blank sheet for a plant: its parameter columns + its time slots.

        The form asks for this once and then renders a grid; ``interval_hours``
        (default 2, matching the paper form) decides the slots, starting at
        ``start_hour`` (default 06:00, the plant's shift start).
        """
        plant_id = request.query_params.get("plant")
        if not plant_id:
            return Response(
                {"detail": "Pass ?plant=<id>."}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            interval = int(request.query_params.get("interval_hours") or 2)
            start_hour = int(request.query_params.get("start_hour") or 6)
        except ValueError:
            return Response(
                {"detail": "interval_hours and start_hour must be whole numbers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        interval = max(1, min(interval, 12))
        start_hour = max(0, min(start_hour, 23))

        parameters = MonitoringParameter.objects.filter(
            plant_id=plant_id, is_active=True
        ).order_by("stage", "sequence", "parameter_name")
        slots = [
            f"{(start_hour + step * interval) % 24:02d}:00"
            for step in range(24 // interval)
        ]
        return Response(
            {
                "plant": int(plant_id),
                "interval_hours": interval,
                "time_slots": slots,
                "parameters": MonitoringParameterSerializer(parameters, many=True).data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="verify",
        permission_classes=[IsAuthenticated, CanVerifyMonitoring],
    )
    def verify(self, request, pk=None):
        """Countersign the sheet (the QAM signature on the paper form)."""
        record = self.get_object()
        verifier_id = request.data.get("verified_by")
        if verifier_id:
            record.verified_by_id = verifier_id
        record.verified_at = timezone.now()
        record.updated_by = request.user
        record.save(
            update_fields=["verified_by", "verified_at", "updated_by", "updated_at"]
        )
        self.log_change(record, ChangeAction.VERIFIED)
        return Response(self.get_serializer(record).data)


# ---------------------------------------------------------------------------
# Register 3 — chemical consumption
# ---------------------------------------------------------------------------


class ChemicalConsumptionLogViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = ChemicalConsumptionLogSerializer
    permission_classes = [IsAuthenticated, ChemicalPermission]
    audit_register = RegisterKey.CHEMICAL

    def audit_extra(self, instance):
        return {"chemicals": etp_services.describe_chemical_lines(instance)}

    def get_queryset(self):
        qs = ChemicalConsumptionLog.objects.select_related(
            "plant", "operator", "verified_by", "created_by"
        ).prefetch_related("lines__chemical")
        return self.filter_register(qs).order_by("-date", "plant__sequence")

    @action(detail=False, methods=["get"], url_path="totals")
    def totals(self, request):
        """Per-chemical totals over the filtered window — the form's TOTAL row."""
        qs = self.filter_register(ChemicalConsumptionLog.objects.all())
        rows = (
            qs.values(
                "lines__chemical__id",
                "lines__chemical__name",
                "lines__uom",
            )
            .annotate(total=Sum("lines__quantity"))
            .exclude(lines__chemical__id=None)
            .order_by("lines__chemical__name")
        )
        return Response(
            [
                {
                    "chemical": row["lines__chemical__id"],
                    "chemical_name": row["lines__chemical__name"],
                    "uom": row["lines__uom"],
                    "total": row["total"] or Decimal("0"),
                }
                for row in rows
            ]
        )


# ---------------------------------------------------------------------------
# Register 4 — sludge generation
# ---------------------------------------------------------------------------


class SludgeGenerationEntryViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = SludgeGenerationEntrySerializer
    permission_classes = [IsAuthenticated, SludgePermission]
    audit_register = RegisterKey.SLUDGE

    def get_queryset(self):
        qs = SludgeGenerationEntry.objects.select_related(
            "plant",
            "collection_mode",
            "storage_method",
            "disposal_mode",
            "operator",
            "supervisor",
            "created_by",
        )
        return self.filter_register(qs).order_by("-date", "-serial_no")


# ---------------------------------------------------------------------------
# Register 5 — daily back washing
# ---------------------------------------------------------------------------


class BackwashEntryViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = BackwashEntrySerializer
    permission_classes = [IsAuthenticated, BackwashPermission]
    audit_register = RegisterKey.BACKWASH

    def get_queryset(self):
        qs = BackwashEntry.objects.select_related(
            "plant", "equipment", "chemical", "operator", "chemist"
        )
        params = self.request.query_params
        equipment = params.get("equipment")
        if equipment:
            qs = qs.filter(equipment_id=equipment)
        return self.filter_register(qs).order_by("-date", "start_time")


# ---------------------------------------------------------------------------
# Register 6 — calibration
# ---------------------------------------------------------------------------


class CalibrationRecordViewSet(
    RegisterAuditMixin, RegisterFilterMixin, viewsets.ModelViewSet
):
    serializer_class = CalibrationRecordSerializer
    permission_classes = [IsAuthenticated, CalibrationPermission]
    plant_field = "instrument__plant"
    audit_register = RegisterKey.CALIBRATION

    def audit_plant(self, instance):
        # A calibration hangs off the instrument, which carries the plant.
        return instance.instrument.plant

    def audit_extra(self, instance):
        return {"readings": etp_services.describe_calibration_readings(instance)}

    def get_queryset(self):
        qs = CalibrationRecord.objects.select_related(
            "instrument",
            "corrective_action",
            "checked_by",
            "verified_by",
            "created_by",
        ).prefetch_related("readings")
        instrument = self.request.query_params.get("instrument")
        if instrument:
            qs = qs.filter(instrument_id=instrument)
        out_of_calibration = self.request.query_params.get("out_of_calibration")
        if out_of_calibration is not None:
            qs = qs.filter(is_out_of_calibration=_bool_param(out_of_calibration))
        return self.filter_register(qs).order_by("-date", "-time")


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------


class RegisterChangeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """The registers' edit trail — read-only by construction (append-only table).

    Filters: ``register``, ``object_id``, ``plant``, ``action``, ``changed_by``
    and a ``date_from`` / ``date_to`` window on the *register row's* date (not on
    when the edit happened), so the trail lines up with the page showing that
    month. ``limit`` caps the rows returned (default 50, max 200).
    """

    serializer_class = RegisterChangeLogSerializer
    # Whoever may read a register may read who changed it; nothing here is
    # writable, so one module-level read right is enough.
    permission_classes = [IsAuthenticated, MasterPermission]

    DEFAULT_LIMIT = 50
    MAX_LIMIT = 200

    def get_queryset(self):
        qs = RegisterChangeLog.objects.select_related("plant", "changed_by")
        params = self.request.query_params
        register = params.get("register")
        if register:
            qs = qs.filter(register=register)
        object_id = params.get("object_id")
        if object_id:
            qs = qs.filter(object_id=object_id)
        plant = params.get("plant")
        if plant:
            qs = qs.filter(plant_id=plant)
        action_param = params.get("action")
        if action_param:
            qs = qs.filter(action=action_param)
        changed_by = params.get("changed_by")
        if changed_by:
            qs = qs.filter(changed_by_id=changed_by)
        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(entry_date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(entry_date__lte=date_to)
        company = params.get("company")
        if company:
            qs = qs.filter(plant__companies__code=company).distinct()

        try:
            limit = int(params.get("limit") or self.DEFAULT_LIMIT)
        except ValueError:
            limit = self.DEFAULT_LIMIT
        limit = max(1, min(limit, self.MAX_LIMIT))
        return qs.order_by("-changed_at", "-id")[:limit]


# ---------------------------------------------------------------------------
# Read-only overviews
# ---------------------------------------------------------------------------


class EtpDashboardAPI(APIView):
    """What is filled and what is still open today, plant by plant.

    This is the module's landing page: the plant team should be able to see in
    one glance which of today's registers are still blank and whether anything
    is out of spec or out of calibration.
    """

    permission_classes = [IsAuthenticated, MasterPermission]

    def get(self, request):
        today = request.query_params.get("date") or timezone.localdate().isoformat()
        company = request.query_params.get("company")
        plants = TreatmentPlant.objects.filter(is_active=True).prefetch_related(
            "companies"
        )
        if company:
            plants = plants.filter(companies__code=company).distinct()

        cards = []
        for plant in plants:
            monitoring = (
                MonitoringRecord.objects.filter(plant=plant, date=today)
                .annotate(reading_count=Count("readings", distinct=True))
                .first()
            )
            out_of_spec = 0
            if monitoring is not None:
                out_of_spec = sum(
                    1
                    for reading in monitoring.readings.all()
                    for value in reading.values.all()
                    if value.is_out_of_spec
                )
            last_sludge = (
                SludgeGenerationEntry.objects.filter(plant=plant)
                .order_by("-date")
                .first()
            )
            cards.append(
                {
                    "plant": plant.id,
                    "plant_code": plant.code,
                    "plant_name": plant.name,
                    "plant_type": plant.plant_type,
                    "companies_display": ", ".join(
                        company.name for company in plant.companies.all()
                    ),
                    "daily_log_done": DailyPlantLog.objects.filter(
                        plant=plant, date=today
                    ).exists(),
                    "chemical_log_done": ChemicalConsumptionLog.objects.filter(
                        plant=plant, date=today
                    ).exists(),
                    "monitoring_readings": (
                        monitoring.reading_count if monitoring else 0
                    ),
                    "monitoring_verified": bool(monitoring and monitoring.verified_at),
                    "monitoring_out_of_spec": out_of_spec,
                    "backwash_entries": BackwashEntry.objects.filter(
                        plant=plant, date=today
                    ).count(),
                    "last_sludge_date": last_sludge.date if last_sludge else None,
                }
            )

        # Calibration is instrument-wise, not plant-wise: anything due within a
        # week (or already overdue) belongs on the landing page.
        horizon = date_cls.fromisoformat(today)
        due = []
        instruments = CalibrationInstrument.objects.filter(
            is_active=True
        ).select_related("plant")
        for instrument in instruments:
            last = instrument.records.order_by("-date", "-time").first()
            due_date = last.due_date if last else None
            if due_date is None or (due_date - horizon).days <= 7:
                due.append(
                    {
                        "instrument": instrument.id,
                        "equipment_name": instrument.equipment_name,
                        "equipment_id": instrument.equipment_id,
                        "plant_code": instrument.plant.code if instrument.plant else "",
                        "frequency": instrument.frequency,
                        "last_calibration_date": last.date if last else None,
                        "due_date": due_date,
                        "is_overdue": bool(due_date and due_date < horizon),
                        "was_out_of_calibration": bool(
                            last and last.is_out_of_calibration
                        ),
                    }
                )

        return Response({"date": today, "plants": cards, "calibration_due": due})


class EtpSummaryAPI(APIView):
    """Period totals for one plant — the TOTAL row printed at the foot of a month.

    ``?plant=<id>&date_from=…&date_to=…``; the window defaults to the current
    calendar month.
    """

    permission_classes = [IsAuthenticated, MasterPermission]

    def get(self, request):
        plant_id = request.query_params.get("plant")
        if not plant_id:
            return Response(
                {"detail": "Pass ?plant=<id>."}, status=status.HTTP_400_BAD_REQUEST
            )
        today = timezone.localdate()
        date_from = request.query_params.get("date_from") or today.replace(
            day=1
        ).isoformat()
        date_to = request.query_params.get("date_to") or today.isoformat()

        logs = DailyPlantLog.objects.filter(
            plant_id=plant_id, date__gte=date_from, date__lte=date_to
        )
        log_totals = logs.aggregate(
            inlet=Sum("inlet_total"),
            outlet=Sum("outlet_total"),
            energy=Sum("energy_units"),
            days=Count("id"),
        )
        sludge = SludgeGenerationEntry.objects.filter(
            plant_id=plant_id, date__gte=date_from, date__lte=date_to
        ).aggregate(kg=Sum("quantity_kg"))
        chemical_rows = (
            ChemicalConsumptionLog.objects.filter(
                plant_id=plant_id, date__gte=date_from, date__lte=date_to
            )
            .values("lines__chemical__name", "lines__uom")
            .annotate(total=Sum("lines__quantity"))
            .exclude(lines__chemical__name=None)
            .order_by("lines__chemical__name")
        )
        monitoring = MonitoringRecord.objects.filter(
            plant_id=plant_id, date__gte=date_from, date__lte=date_to
        )
        out_of_spec = sum(
            1
            for record in monitoring.prefetch_related("readings__values")
            for reading in record.readings.all()
            for value in reading.values.all()
            if value.is_out_of_spec
        )

        return Response(
            {
                "plant": int(plant_id),
                "date_from": date_from,
                "date_to": date_to,
                "days_logged": log_totals["days"] or 0,
                "inlet_kl": log_totals["inlet"] or Decimal("0"),
                "outlet_kl": log_totals["outlet"] or Decimal("0"),
                "energy_units": log_totals["energy"] or Decimal("0"),
                "sludge_kg": sludge["kg"] or Decimal("0"),
                "chemicals": [
                    {
                        "chemical_name": row["lines__chemical__name"],
                        "uom": row["lines__uom"],
                        "total": row["total"] or Decimal("0"),
                    }
                    for row in chemical_rows
                ],
                "monitoring_sheets": monitoring.count(),
                "monitoring_out_of_spec": out_of_spec,
            }
        )
