"""
Serializers for the ETP / STP registers.

Two conventions run through the file:

* **Masters** expose the fields the Settings screen edits, plus a couple of
  read-only conveniences (last reading, usage counts) so the UI does not need a
  second call to know whether a master row is safe to retire.
* **Registers** accept the grid the paper form has and derive everything else.
  The nested writes (monitoring readings, chemical lines, calibration readings)
  are *replace-all*: the payload is the sheet as it now stands, which is how an
  operator thinks about a page they are correcting.
"""

from django.db import transaction
from rest_framework import serializers

from company.models import Company

from .constants import OptionCategory, PrintDocumentKey
from .models import (
    BackwashEntry,
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationPoint,
    CalibrationReading,
    CalibrationRecord,
    ChemicalConsumptionLine,
    ChemicalConsumptionLog,
    DailyPlantLog,
    EtpPrintDocument,
    MonitoringParameter,
    MonitoringReading,
    MonitoringRecord,
    MonitoringValue,
    PlantChemical,
    PlantOption,
    PlantStaff,
    RegisterChangeLog,
    SludgeGenerationEntry,
    TreatmentPlant,
)

# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------


class TreatmentPlantSerializer(serializers.ModelSerializer):
    # Companies are addressed by code (JIVO_OIL / …) so the UI never carries ids.
    company_codes = serializers.SlugRelatedField(
        source="companies",
        slug_field="code",
        many=True,
        required=False,
        queryset=Company.objects.filter(is_active=True),
    )
    companies_display = serializers.SerializerMethodField()
    plant_type_display = serializers.CharField(
        source="get_plant_type_display", read_only=True
    )

    class Meta:
        model = TreatmentPlant
        fields = [
            "id",
            "name",
            "code",
            "plant_type",
            "plant_type_display",
            "location",
            "company_codes",
            "companies_display",
            "capacity_kld",
            "consent_number",
            "sequence",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_companies_display(self, obj) -> str:
        return ", ".join(company.name for company in obj.companies.all())


class PlantStaffSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    plant_ids = serializers.PrimaryKeyRelatedField(
        source="plants",
        many=True,
        required=False,
        queryset=TreatmentPlant.objects.all(),
    )

    class Meta:
        model = PlantStaff
        fields = [
            "id",
            "name",
            "role",
            "role_display",
            "employee_code",
            "plant_ids",
            "sequence",
            "is_active",
        ]


class PlantOptionSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )

    class Meta:
        model = PlantOption
        fields = [
            "id",
            "category",
            "category_display",
            "label",
            "sequence",
            "is_default",
            "is_active",
        ]


class PlantChemicalSerializer(serializers.ModelSerializer):
    uom_display = serializers.CharField(source="get_default_uom_display", read_only=True)
    plant_ids = serializers.PrimaryKeyRelatedField(
        source="plants",
        many=True,
        required=False,
        queryset=TreatmentPlant.objects.all(),
    )

    class Meta:
        model = PlantChemical
        fields = [
            "id",
            "name",
            "default_uom",
            "uom_display",
            "plant_ids",
            "sequence",
            "remarks",
            "is_active",
        ]


class BackwashEquipmentSerializer(serializers.ModelSerializer):
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    default_chemical_name = serializers.CharField(
        source="default_chemical.name", read_only=True, default=""
    )

    class Meta:
        model = BackwashEquipment
        fields = [
            "id",
            "plant",
            "plant_code",
            "name",
            "equipment_code",
            "default_chemical",
            "default_chemical_name",
            "default_duration_minutes",
            "sequence",
            "is_active",
        ]


class MonitoringParameterSerializer(serializers.ModelSerializer):
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    stage_display = serializers.CharField(source="get_stage_display", read_only=True)

    class Meta:
        model = MonitoringParameter
        fields = [
            "id",
            "plant",
            "plant_code",
            "stage",
            "stage_display",
            "parameter_key",
            "parameter_name",
            "unit",
            "min_value",
            "max_value",
            "specification_text",
            "validation_type",
            "sequence",
            "is_active",
        ]


class CalibrationPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalibrationPoint
        fields = ["id", "actual_value", "label", "sequence", "is_active"]


class CalibrationInstrumentSerializer(serializers.ModelSerializer):
    """The instrument plus its buffer points — written together in one call."""

    points = CalibrationPointSerializer(many=True, required=False)
    plant_code = serializers.CharField(source="plant.code", read_only=True, default="")
    frequency_display = serializers.CharField(
        source="get_frequency_display", read_only=True
    )
    # Annotated by the viewset: the latest calibration and the due date it set.
    # (Deliberately NOT called ``next_due_date`` — that is a model method.)
    last_calibration_date = serializers.DateField(read_only=True)
    calibration_due_date = serializers.DateField(read_only=True)
    records_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CalibrationInstrument
        fields = [
            "id",
            "plant",
            "plant_code",
            "equipment_name",
            "equipment_id",
            "line_id",
            "location",
            "working_range",
            "frequency",
            "frequency_display",
            "tolerance",
            "standard_make_model",
            "standard_equipment_id",
            "standard_range",
            "external_calibration_date",
            "external_calibration_due_date",
            "sequence",
            "is_active",
            "points",
            "last_calibration_date",
            "calibration_due_date",
            "records_count",
        ]

    def _sync_points(self, instrument, points_data):
        """Replace the instrument's buffer points with the payload's."""
        keep_ids = []
        for index, point in enumerate(points_data):
            point_id = point.get("id")
            defaults = {
                "label": point.get("label", ""),
                "sequence": point.get("sequence", index),
                "is_active": point.get("is_active", True),
            }
            obj, _ = CalibrationPoint.objects.update_or_create(
                instrument=instrument,
                actual_value=point["actual_value"],
                defaults=defaults,
            )
            keep_ids.append(obj.id)
            if point_id and point_id != obj.id:
                # The value was edited: the old row is superseded.
                CalibrationPoint.objects.filter(
                    id=point_id, instrument=instrument
                ).delete()
        instrument.points.exclude(id__in=keep_ids).delete()

    @transaction.atomic
    def create(self, validated_data):
        points_data = validated_data.pop("points", [])
        instrument = super().create(validated_data)
        self._sync_points(instrument, points_data)
        return instrument

    @transaction.atomic
    def update(self, instance, validated_data):
        points_data = validated_data.pop("points", None)
        instrument = super().update(instance, validated_data)
        if points_data is not None:
            self._sync_points(instrument, points_data)
        return instrument


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _option_of_category(value, category, field_name):
    """Refuse an option picked from the wrong dropdown."""
    if value is not None and value.category != category:
        raise serializers.ValidationError(
            {
                field_name: (
                    f"'{value.label}' belongs to the "
                    f"{value.get_category_display()} list, not "
                    f"{OptionCategory(category).label}."
                )
            }
        )
    return value


def _reject_duplicate(model, field_name, message, instance=None, **lookup):
    """Friendly duplicate check — a UniqueConstraint alone would 500."""
    clash = model.objects.filter(**lookup)
    if instance is not None:
        clash = clash.exclude(pk=instance.pk)
    if clash.exists():
        raise serializers.ValidationError({field_name: message})


# ---------------------------------------------------------------------------
# Register 1 — daily plant log
# ---------------------------------------------------------------------------


class DailyPlantLogSerializer(serializers.ModelSerializer):
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    plant_name = serializers.CharField(source="plant.name", read_only=True)
    operator_name = serializers.CharField(
        source="operator.name", read_only=True, default=""
    )
    chemist_name = serializers.CharField(
        source="chemist.name", read_only=True, default=""
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )
    # Opening readings are optional: when omitted they carry forward from the
    # plant's previous day, which is how the paper register is filled.
    inlet_initial = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    outlet_initial = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )
    energy_initial = serializers.DecimalField(
        max_digits=14, decimal_places=2, required=False, allow_null=True
    )

    class Meta:
        model = DailyPlantLog
        # The unique page / row is checked in ``validate`` so the error lands on
        # a field the form can highlight; DRF's generated unique-together
        # validator would answer with a non-field error instead.
        validators = []
        fields = [
            "id",
            "plant",
            "plant_code",
            "plant_name",
            "date",
            "inlet_initial",
            "inlet_final",
            "inlet_total",
            "outlet_initial",
            "outlet_final",
            "outlet_total",
            "ph_reading",
            "ph_reading_time",
            "energy_initial",
            "energy_final",
            "energy_units",
            "operator",
            "operator_name",
            "chemist",
            "chemist_name",
            "remarks",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "inlet_total",
            "outlet_total",
            "energy_units",
            "created_at",
            "updated_at",
        ]

    #: (opening field, closing field) pairs that carry forward day to day.
    METER_PAIRS = (
        ("inlet_initial", "inlet_final"),
        ("outlet_initial", "outlet_final"),
        ("energy_initial", "energy_final"),
    )

    def validate(self, attrs):
        plant = attrs.get("plant") or getattr(self.instance, "plant", None)
        date = attrs.get("date") or getattr(self.instance, "date", None)

        if plant and date:
            _reject_duplicate(
                DailyPlantLog,
                "date",
                "This plant already has a log for that date — open it and edit instead.",
                instance=self.instance,
                plant=plant,
                date=date,
            )

        previous = None
        if plant and date and self.instance is None:
            previous = (
                DailyPlantLog.objects.filter(plant=plant, date__lt=date)
                .order_by("-date")
                .first()
            )

        for opening_field, closing_field in self.METER_PAIRS:
            opening = attrs.get(
                opening_field, getattr(self.instance, opening_field, None)
            )
            if opening is None and previous is not None:
                opening = getattr(previous, closing_field)
                if opening is not None:
                    attrs[opening_field] = opening
            closing = attrs.get(
                closing_field, getattr(self.instance, closing_field, None)
            )
            if opening is not None and closing is not None and closing < opening:
                raise serializers.ValidationError(
                    {
                        closing_field: (
                            "The closing reading cannot be lower than the opening "
                            "reading — check which figure went where."
                        )
                    }
                )
        return attrs


# ---------------------------------------------------------------------------
# Register 2 — on-line monitoring
# ---------------------------------------------------------------------------


class MonitoringValueSerializer(serializers.ModelSerializer):
    parameter_key = serializers.CharField(
        source="parameter.parameter_key", read_only=True
    )
    parameter_name = serializers.CharField(
        source="parameter.parameter_name", read_only=True
    )
    stage = serializers.CharField(source="parameter.stage", read_only=True)
    unit = serializers.CharField(source="parameter.unit", read_only=True)

    class Meta:
        model = MonitoringValue
        fields = [
            "id",
            "parameter",
            "parameter_key",
            "parameter_name",
            "stage",
            "unit",
            "value",
            "is_out_of_spec",
        ]
        read_only_fields = ["is_out_of_spec"]


class MonitoringReadingSerializer(serializers.ModelSerializer):
    values = MonitoringValueSerializer(many=True, required=False)
    operator_name = serializers.CharField(
        source="operator.name", read_only=True, default=""
    )

    class Meta:
        model = MonitoringReading
        fields = [
            "id",
            "reading_time",
            "operator",
            "operator_name",
            "remarks",
            "values",
        ]


class MonitoringRecordSerializer(serializers.ModelSerializer):
    """The whole day's sheet: header + every time slot + every cell.

    Sending ``readings`` replaces the sheet's rows with what the payload holds —
    the operator is editing one page, and the page is the unit of truth.
    """

    readings = MonitoringReadingSerializer(many=True, required=False)
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    plant_name = serializers.CharField(source="plant.name", read_only=True)
    chemist_name = serializers.CharField(
        source="chemist.name", read_only=True, default=""
    )
    verified_by_name = serializers.CharField(
        source="verified_by.name", read_only=True, default=""
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )
    is_verified = serializers.BooleanField(read_only=True)
    out_of_spec_count = serializers.SerializerMethodField()

    class Meta:
        model = MonitoringRecord
        # The unique page / row is checked in ``validate`` so the error lands on
        # a field the form can highlight; DRF's generated unique-together
        # validator would answer with a non-field error instead.
        validators = []
        fields = [
            "id",
            "plant",
            "plant_code",
            "plant_name",
            "date",
            "interval_hours",
            "chemist",
            "chemist_name",
            "verified_by",
            "verified_by_name",
            "verified_at",
            "is_verified",
            "remarks",
            "readings",
            "out_of_spec_count",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["verified_at", "created_at", "updated_at"]

    def get_out_of_spec_count(self, obj) -> int:
        return sum(
            1
            for reading in obj.readings.all()
            for value in reading.values.all()
            if value.is_out_of_spec
        )

    def validate(self, attrs):
        plant = attrs.get("plant") or getattr(self.instance, "plant", None)
        date = attrs.get("date") or getattr(self.instance, "date", None)
        if plant and date:
            _reject_duplicate(
                MonitoringRecord,
                "date",
                "This plant already has a monitoring sheet for that date.",
                instance=self.instance,
                plant=plant,
                date=date,
            )
        readings = attrs.get("readings")
        if readings is not None:
            times = [reading["reading_time"] for reading in readings]
            if len(times) != len(set(times)):
                raise serializers.ValidationError(
                    {"readings": "Two rows carry the same reading time."}
                )
            if plant is not None:
                self._check_parameters_belong_to_plant(plant, readings)
        return attrs

    @staticmethod
    def _check_parameters_belong_to_plant(plant, readings):
        for reading in readings:
            for value in reading.get("values", []):
                parameter = value.get("parameter")
                if parameter is not None and parameter.plant_id != plant.id:
                    raise serializers.ValidationError(
                        {
                            "readings": (
                                f"Parameter '{parameter.parameter_name}' is "
                                f"configured for another plant."
                            )
                        }
                    )

    def _sync_readings(self, record, readings_data, user=None):
        record.readings.all().delete()
        for reading in readings_data:
            values = reading.pop("values", [])
            row = MonitoringReading.objects.create(
                record=record, created_by=user, updated_by=user, **reading
            )
            for value in values:
                MonitoringValue.objects.create(
                    reading=row, created_by=user, updated_by=user, **value
                )

    @transaction.atomic
    def create(self, validated_data):
        readings_data = validated_data.pop("readings", [])
        record = super().create(validated_data)
        self._sync_readings(readings_data=readings_data, record=record,
                            user=validated_data.get("created_by"))
        return record

    @transaction.atomic
    def update(self, instance, validated_data):
        readings_data = validated_data.pop("readings", None)
        record = super().update(instance, validated_data)
        if readings_data is not None:
            self._sync_readings(
                readings_data=readings_data,
                record=record,
                user=validated_data.get("updated_by"),
            )
        return record


# ---------------------------------------------------------------------------
# Register 3 — chemical consumption
# ---------------------------------------------------------------------------


class ChemicalConsumptionLineSerializer(serializers.ModelSerializer):
    chemical_name = serializers.CharField(source="chemical.name", read_only=True)
    uom = serializers.CharField(required=False)
    # The form posts one cell per chemical and most of them are empty; a blank
    # cell is dropped rather than filed as a zero dose (see ``_sync_lines``).
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True
    )

    class Meta:
        model = ChemicalConsumptionLine
        fields = ["id", "chemical", "chemical_name", "quantity", "uom"]


class ChemicalConsumptionLogSerializer(serializers.ModelSerializer):
    lines = ChemicalConsumptionLineSerializer(many=True, required=False)
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    plant_name = serializers.CharField(source="plant.name", read_only=True)
    operator_name = serializers.CharField(
        source="operator.name", read_only=True, default=""
    )
    verified_by_name = serializers.CharField(
        source="verified_by.name", read_only=True, default=""
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = ChemicalConsumptionLog
        # The unique page / row is checked in ``validate`` so the error lands on
        # a field the form can highlight; DRF's generated unique-together
        # validator would answer with a non-field error instead.
        validators = []
        fields = [
            "id",
            "plant",
            "plant_code",
            "plant_name",
            "date",
            "operator",
            "operator_name",
            "verified_by",
            "verified_by_name",
            "remarks",
            "lines",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        plant = attrs.get("plant") or getattr(self.instance, "plant", None)
        date = attrs.get("date") or getattr(self.instance, "date", None)
        if plant and date:
            _reject_duplicate(
                ChemicalConsumptionLog,
                "date",
                "This plant already has a chemical entry for that date.",
                instance=self.instance,
                plant=plant,
                date=date,
            )
        lines = attrs.get("lines")
        if lines is not None:
            chemicals = [line["chemical"].id for line in lines]
            if len(chemicals) != len(set(chemicals)):
                raise serializers.ValidationError(
                    {"lines": "The same chemical is listed twice."}
                )
        return attrs

    def _sync_lines(self, log, lines_data, user=None):
        log.lines.all().delete()
        for line in lines_data:
            # A blank cell is not a zero-dose row — leave it off the register.
            if line.get("quantity") in (None, ""):
                continue
            ChemicalConsumptionLine.objects.create(
                log=log, created_by=user, updated_by=user, **line
            )

    @transaction.atomic
    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        log = super().create(validated_data)
        self._sync_lines(log, lines_data, user=validated_data.get("created_by"))
        return log

    @transaction.atomic
    def update(self, instance, validated_data):
        lines_data = validated_data.pop("lines", None)
        log = super().update(instance, validated_data)
        if lines_data is not None:
            self._sync_lines(log, lines_data, user=validated_data.get("updated_by"))
        return log


# ---------------------------------------------------------------------------
# Register 4 — sludge generation
# ---------------------------------------------------------------------------


class SludgeGenerationEntrySerializer(serializers.ModelSerializer):
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    plant_name = serializers.CharField(source="plant.name", read_only=True)
    collection_mode_label = serializers.CharField(
        source="collection_mode.label", read_only=True, default=""
    )
    storage_method_label = serializers.CharField(
        source="storage_method.label", read_only=True, default=""
    )
    disposal_mode_label = serializers.CharField(
        source="disposal_mode.label", read_only=True, default=""
    )
    operator_name = serializers.CharField(
        source="operator.name", read_only=True, default=""
    )
    supervisor_name = serializers.CharField(
        source="supervisor.name", read_only=True, default=""
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = SludgeGenerationEntry
        fields = [
            "id",
            "serial_no",
            "plant",
            "plant_code",
            "plant_name",
            "date",
            "quantity_kg",
            "collection_mode",
            "collection_mode_label",
            "storage_method",
            "storage_method_label",
            "disposal_mode",
            "disposal_mode_label",
            "operator",
            "operator_name",
            "supervisor",
            "supervisor_name",
            "photo",
            "remarks",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["serial_no", "created_at", "updated_at"]

    def validate(self, attrs):
        _option_of_category(
            attrs.get("collection_mode"),
            OptionCategory.SLUDGE_COLLECTION_MODE,
            "collection_mode",
        )
        _option_of_category(
            attrs.get("storage_method"),
            OptionCategory.SLUDGE_STORAGE_METHOD,
            "storage_method",
        )
        _option_of_category(
            attrs.get("disposal_mode"),
            OptionCategory.SLUDGE_DISPOSAL_MODE,
            "disposal_mode",
        )
        return attrs


# ---------------------------------------------------------------------------
# Register 5 — daily back washing
# ---------------------------------------------------------------------------


class BackwashEntrySerializer(serializers.ModelSerializer):
    plant_code = serializers.CharField(source="plant.code", read_only=True)
    equipment_name = serializers.CharField(source="equipment.name", read_only=True)
    chemical_name = serializers.CharField(
        source="chemical.name", read_only=True, default=""
    )
    operator_name = serializers.CharField(
        source="operator.name", read_only=True, default=""
    )
    chemist_name = serializers.CharField(
        source="chemist.name", read_only=True, default=""
    )

    class Meta:
        model = BackwashEntry
        # The unique page / row is checked in ``validate`` so the error lands on
        # a field the form can highlight; DRF's generated unique-together
        # validator would answer with a non-field error instead.
        validators = []
        fields = [
            "id",
            "plant",
            "plant_code",
            "date",
            "equipment",
            "equipment_name",
            "chemical",
            "chemical_name",
            "chemical_quantity",
            "start_time",
            "stop_time",
            "contact_minutes",
            "operator",
            "operator_name",
            "chemist",
            "chemist_name",
            "remarks",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["contact_minutes", "created_at", "updated_at"]

    def validate(self, attrs):
        equipment = attrs.get("equipment") or getattr(self.instance, "equipment", None)
        plant = attrs.get("plant") or getattr(self.instance, "plant", None)
        if equipment and plant and equipment.plant_id != plant.id:
            raise serializers.ValidationError(
                {
                    "equipment": (
                        f"'{equipment.name}' belongs to {equipment.plant.code}, "
                        f"not {plant.code}."
                    )
                }
            )
        date = attrs.get("date") or getattr(self.instance, "date", None)
        start = attrs.get("start_time") or getattr(self.instance, "start_time", None)
        if equipment and date and start:
            _reject_duplicate(
                BackwashEntry,
                "start_time",
                "That step is already logged for this date and start time.",
                instance=self.instance,
                equipment=equipment,
                date=date,
                start_time=start,
            )
        return attrs


# ---------------------------------------------------------------------------
# Register 6 — calibration
# ---------------------------------------------------------------------------


class CalibrationReadingSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalibrationReading
        fields = [
            "id",
            "actual_value",
            "observed_value",
            "variation",
            "is_within_tolerance",
            "remarks",
        ]
        read_only_fields = ["variation", "is_within_tolerance"]


class CalibrationRecordSerializer(serializers.ModelSerializer):
    """One calibration plus its standard-vs-observed rows.

    When ``readings`` is omitted on create, the instrument's configured buffer
    points are laid out as empty rows so the operator only fills the observed
    column.
    """

    readings = CalibrationReadingSerializer(many=True, required=False)
    instrument_name = serializers.CharField(
        source="instrument.equipment_name", read_only=True
    )
    instrument_code = serializers.CharField(
        source="instrument.equipment_id", read_only=True
    )
    instrument_location = serializers.CharField(
        source="instrument.location", read_only=True
    )
    instrument_working_range = serializers.CharField(
        source="instrument.working_range", read_only=True
    )
    instrument_frequency = serializers.CharField(
        source="instrument.get_frequency_display", read_only=True
    )
    standard_make_model = serializers.CharField(
        source="instrument.standard_make_model", read_only=True
    )
    tolerance = serializers.DecimalField(
        source="instrument.tolerance", max_digits=8, decimal_places=3, read_only=True
    )
    corrective_action_label = serializers.CharField(
        source="corrective_action.label", read_only=True, default=""
    )
    checked_by_name = serializers.CharField(
        source="checked_by.name", read_only=True, default=""
    )
    verified_by_name = serializers.CharField(
        source="verified_by.name", read_only=True, default=""
    )
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = CalibrationRecord
        # Same reason as the other registers: our own duplicate check names the
        # field the form can highlight.
        validators = []
        fields = [
            "id",
            "instrument",
            "instrument_name",
            "instrument_code",
            "instrument_location",
            "instrument_working_range",
            "instrument_frequency",
            "standard_make_model",
            "tolerance",
            "date",
            "time",
            "due_date",
            "corrective_action",
            "corrective_action_label",
            "is_out_of_calibration",
            "was_replaced",
            "checked_by",
            "checked_by_name",
            "verified_by",
            "verified_by_name",
            "remarks",
            "readings",
            "created_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "is_out_of_calibration",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        _option_of_category(
            attrs.get("corrective_action"),
            OptionCategory.CALIBRATION_ACTION,
            "corrective_action",
        )
        instrument = attrs.get("instrument") or getattr(
            self.instance, "instrument", None
        )
        date = attrs.get("date") or getattr(self.instance, "date", None)
        time = attrs.get("time", getattr(self.instance, "time", None))
        if instrument and date:
            _reject_duplicate(
                CalibrationRecord,
                "time",
                "This instrument already has a calibration at that date and time.",
                instance=self.instance,
                instrument=instrument,
                date=date,
                time=time,
            )
        return attrs

    def _default_readings(self, instrument):
        return [
            {"actual_value": point.actual_value, "observed_value": None}
            for point in instrument.points.filter(is_active=True)
        ]

    def _sync_readings(self, record, readings_data, user=None):
        record.readings.all().delete()
        for reading in readings_data:
            CalibrationReading.objects.create(
                record=record, created_by=user, updated_by=user, **reading
            )
        record.recompute_from_readings()

    @transaction.atomic
    def create(self, validated_data):
        readings_data = validated_data.pop("readings", None)
        record = super().create(validated_data)
        if readings_data is None:
            readings_data = self._default_readings(record.instrument)
        self._sync_readings(
            record, readings_data, user=validated_data.get("created_by")
        )
        return record

    @transaction.atomic
    def update(self, instance, validated_data):
        readings_data = validated_data.pop("readings", None)
        record = super().update(instance, validated_data)
        if readings_data is not None:
            self._sync_readings(
                record, readings_data, user=validated_data.get("updated_by")
            )
        return record


# ---------------------------------------------------------------------------
# Print documents
# ---------------------------------------------------------------------------


class EtpPrintDocumentSerializer(serializers.ModelSerializer):
    """The controlled-document identity of one printable form.

    ``company_code`` is how the UI addresses the scope: null / omitted means the
    factory-wide default row, a code names the company whose set overrides it.
    """

    # Declared explicitly so it does not inherit the model's field-level unique
    # validator: the partial index that keeps ONE factory-wide row per form would
    # otherwise be read as "one row per form, full stop", refusing a company's
    # override. The real check is in ``validate``.
    document_key = serializers.ChoiceField(choices=PrintDocumentKey.choices)
    document_key_label = serializers.CharField(
        source="get_document_key_display", read_only=True
    )
    company_code = serializers.SlugRelatedField(
        source="company",
        slug_field="code",
        required=False,
        allow_null=True,
        queryset=Company.objects.filter(is_active=True),
    )

    class Meta:
        model = EtpPrintDocument
        # `company` (id, read-only) and `company_code` (writable) both map to the
        # same model field, so DRF cannot build the unique-together validator
        # itself — ``validate`` below does that check, and names the field the
        # form can highlight.
        validators = []
        fields = [
            "id",
            "document_key",
            "document_key_label",
            "company",
            "company_code",
            "form_name",
            "document_code",
            "revision",
            "issue_date",
            "document_id",
            "notes",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["company", "created_at", "updated_at"]

    def validate(self, attrs):
        company = attrs.get(
            "company", getattr(self.instance, "company", None)
        )
        key = attrs.get("document_key") or getattr(self.instance, "document_key", None)
        if key:
            clash = EtpPrintDocument.objects.filter(document_key=key, company=company)
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                scope = company.code if company else "every company"
                raise serializers.ValidationError(
                    {
                        "document_key": (
                            f"This form already has a number for {scope} — edit "
                            f"that row instead."
                        )
                    }
                )
        return attrs


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------


class RegisterChangeLogSerializer(serializers.ModelSerializer):
    """One line of the edit trail, ready to print next to the register."""

    register_display = serializers.CharField(
        source="get_register_display", read_only=True
    )
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    plant_code = serializers.CharField(source="plant.code", read_only=True, default="")
    changed_by_name = serializers.CharField(
        source="changed_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = RegisterChangeLog
        fields = [
            "id",
            "register",
            "register_display",
            "action",
            "action_display",
            "object_id",
            "model_name",
            "plant",
            "plant_code",
            "entry_date",
            "changes",
            "summary",
            "changed_by",
            "changed_by_name",
            "changed_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Read-only summaries
# ---------------------------------------------------------------------------


class MonthlyTotalsSerializer(serializers.Serializer):
    """Period totals for one plant — the footer line of the paper register."""

    plant = serializers.IntegerField()
    plant_code = serializers.CharField()
    days = serializers.IntegerField()
    inlet_kl = serializers.DecimalField(max_digits=16, decimal_places=2)
    outlet_kl = serializers.DecimalField(max_digits=16, decimal_places=2)
    energy_units = serializers.DecimalField(max_digits=16, decimal_places=2)
    sludge_kg = serializers.DecimalField(max_digits=16, decimal_places=2)
    chemicals = serializers.ListField(child=serializers.DictField())
