"""
ETP / STP plant registers — the paper QA records the treatment plants keep,
digitised.

The module covers six registers, all of them a date + a plant + a handful of
readings:

* :class:`DailyPlantLog`          — "Effluent Treatment Plant Record": inlet /
  outlet flow meters, pH, energy meter, one row per day.
* :class:`MonitoringRecord`       — "ETP On Line Monitoring Record": a two-hourly
  grid of stage-wise parameters (influent / aeration / treated).
* :class:`ChemicalConsumptionLog` — "Chemical Consumption Record": one row per
  day, one column per chemical the plant actually doses.
* :class:`SludgeGenerationEntry`  — "Sludge Generation Record".
* :class:`BackwashEntry`          — "Daily Back Washing Record".
* :class:`CalibrationRecord`      — "Calibration Record" for the plant's
  instruments (the pH meter today).

Everything the plant team is expected to maintain is a master row, never a
hardcoded list: the plants themselves, the chemical columns, the monitoring
parameters and their limits, the back-wash equipment, the instruments and their
calibration buffer points, the people who sign, and the small sludge /
calibration dropdowns. See ``etp.constants`` for the fixed vocabularies.

Totals (flow KL, units, variation, contact time) are always DERIVED on save so
the register can never disagree with its own arithmetic.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Max
from django.utils.dateparse import parse_time

from gate_core.models.base import BaseModel

from .constants import (
    CALIBRATION_FREQUENCY_DAYS,
    CalibrationFrequency,
    ChangeAction,
    ChemicalUom,
    MonitoringStage,
    OptionCategory,
    PlantType,
    PrintDocumentKey,
    RegisterKey,
    SpecValidationType,
    StaffRole,
)


def _as_time(value):
    """Accept a ``time`` or an "HH:MM" string — the ORM allows either on assignment."""
    return parse_time(value) if isinstance(value, str) else value


class EtpPermission(models.Model):
    """Sentinel model carrying the module's permissions (no table of its own).

    One view / one manage right per register, so a plant operator can be given
    the daily log without also being handed the calibration record, and the
    masters stay behind a single settings right.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_etp_module", "Can view ETP / STP module"),
            ("can_manage_etp_settings", "Can manage ETP / STP masters & settings"),
            ("can_view_etp_daily_log", "Can view ETP daily plant log"),
            ("can_manage_etp_daily_log", "Can record ETP daily plant log"),
            ("can_view_etp_monitoring", "Can view ETP on-line monitoring record"),
            ("can_manage_etp_monitoring", "Can record ETP on-line monitoring record"),
            ("can_verify_etp_monitoring", "Can verify ETP on-line monitoring record"),
            ("can_view_etp_chemical", "Can view ETP chemical consumption record"),
            ("can_manage_etp_chemical", "Can record ETP chemical consumption"),
            ("can_view_etp_sludge", "Can view ETP sludge generation record"),
            ("can_manage_etp_sludge", "Can record ETP sludge generation"),
            ("can_view_etp_backwash", "Can view ETP daily back-washing record"),
            ("can_manage_etp_backwash", "Can record ETP daily back-washing"),
            ("can_view_etp_calibration", "Can view ETP calibration record"),
            ("can_manage_etp_calibration", "Can record ETP calibration"),
        ]


# ---------------------------------------------------------------------------
# Masters — the configuration the plant team maintains themselves
# ---------------------------------------------------------------------------


class TreatmentPlant(BaseModel):
    """One treatment plant. Every register row hangs off one of these.

    ``companies`` works like the electricity meter master: the Ganaur ETP/STP
    serves the shared campus (Oil + Beverages), so a plant can be tagged with
    several companies; leaving it empty means "not attributed yet" — the plant
    still shows on the unfiltered register but drops out of a company-filtered
    view.
    """

    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(
        max_length=20,
        unique=True,
        help_text="Short code used on prints and filters, e.g. 'ETP' / 'STP'.",
    )
    plant_type = models.CharField(
        max_length=10, choices=PlantType.choices, default=PlantType.ETP
    )
    location = models.CharField(max_length=200, blank=True, default="")
    companies = models.ManyToManyField(
        "company.Company",
        blank=True,
        related_name="treatment_plants",
        help_text=(
            "Companies this plant serves. Pick more than one for a plant shared "
            "between companies; leave empty if it is not attributed to any."
        ),
    )
    capacity_kld = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Design capacity in KLD (kilolitres per day), for reference.",
    )
    consent_number = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Pollution-board consent / NOC number, printed on the register.",
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "name"]
        verbose_name = "Treatment Plant"
        verbose_name_plural = "Treatment Plants"

    def __str__(self):
        return f"{self.code} — {self.name}"


class PlantStaff(BaseModel):
    """A person who signs a register (operator / chemist / supervisor / QAM).

    Most floor operators have no application login, and the paper form only ever
    carries a name and a signature — so the signature dropdowns point here.
    Who *entered* the row is recorded separately by ``BaseModel.created_by``.
    """

    name = models.CharField(max_length=120)
    role = models.CharField(
        max_length=12, choices=StaffRole.choices, default=StaffRole.OPERATOR
    )
    employee_code = models.CharField(max_length=40, blank=True, default="")
    plants = models.ManyToManyField(
        TreatmentPlant,
        blank=True,
        related_name="staff",
        help_text="Leave empty to offer this person on every plant's registers.",
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "role"], name="uq_etp_staff_name_role"
            ),
        ]
        verbose_name = "Plant Staff"
        verbose_name_plural = "Plant Staff"

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"


class PlantOption(BaseModel):
    """A value in one of the small maintained dropdowns (see OptionCategory).

    "Filter press", "Bag", "Nil" — words the plant team should be able to add
    without a release.
    """

    category = models.CharField(max_length=32, choices=OptionCategory.choices)
    label = models.CharField(max_length=120)
    sequence = models.PositiveSmallIntegerField(default=0)
    is_default = models.BooleanField(
        default=False,
        help_text="Preselected in the form. Only one default per category is used.",
    )

    class Meta:
        ordering = ["category", "sequence", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "label"], name="uq_etp_option_category_label"
            ),
        ]
        verbose_name = "Plant Dropdown Option"
        verbose_name_plural = "Plant Dropdown Options"

    def __str__(self):
        return f"{self.get_category_display()}: {self.label}"


class PlantChemical(BaseModel):
    """A chemical the plants dose — one column on the consumption register.

    The ETP and the STP dose different chemicals (the ETP form has DAP and Urea,
    the STP form does not), so a chemical is tagged with the plants that use it.
    """

    name = models.CharField(max_length=120, unique=True)
    default_uom = models.CharField(
        max_length=5, choices=ChemicalUom.choices, default=ChemicalUom.KG
    )
    plants = models.ManyToManyField(
        TreatmentPlant,
        blank=True,
        related_name="chemicals",
        help_text="Plants that dose this chemical. Empty = offered on every plant.",
    )
    sequence = models.PositiveSmallIntegerField(
        default=0, help_text="Column order on the consumption register."
    )
    remarks = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["sequence", "name"]
        verbose_name = "Plant Chemical"
        verbose_name_plural = "Plant Chemicals"

    def __str__(self):
        return f"{self.name} ({self.get_default_uom_display()})"


class BackwashEquipment(BaseModel):
    """A step on the daily back-washing register (sand filter backwash, rinse …)."""

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.PROTECT, related_name="backwash_equipment"
    )
    name = models.CharField(max_length=150)
    equipment_code = models.CharField(max_length=60, blank=True, default="")
    default_chemical = models.ForeignKey(
        PlantChemical,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="backwash_equipment",
        help_text="Prefilled as the 'type of chemical' when this step is logged.",
    )
    default_duration_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Usual contact time; used to prefill the stop time.",
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["plant__sequence", "sequence", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "name"], name="uq_etp_backwash_equipment_name"
            ),
        ]
        verbose_name = "Back-wash Equipment / Step"
        verbose_name_plural = "Back-wash Equipment / Steps"

    def __str__(self):
        return f"{self.plant.code} — {self.name}"


class MonitoringParameter(BaseModel):
    """One column of the on-line monitoring grid, with its limits.

    Keyed by ``(plant, stage, parameter_key)``: the ETP form monitors pH + TDS on
    the influent, pH + TDS + DO on the aeration tank and on the treated
    effluent. A value outside ``min_value`` / ``max_value`` is flagged (never
    rejected — the register must still be able to record what actually happened).
    """

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.CASCADE, related_name="monitoring_parameters"
    )
    stage = models.CharField(
        max_length=12, choices=MonitoringStage.choices, default=MonitoringStage.INFLUENT
    )
    parameter_key = models.CharField(
        max_length=40, help_text="Stable key, e.g. 'ph', 'tds', 'do'."
    )
    parameter_name = models.CharField(max_length=120)
    unit = models.CharField(max_length=20, blank=True, default="")
    min_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    max_value = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    specification_text = models.CharField(
        max_length=60,
        blank=True,
        default="",
        help_text="Spec as printed, e.g. '6.5-8.5'.",
    )
    validation_type = models.CharField(
        max_length=10,
        choices=SpecValidationType.choices,
        default=SpecValidationType.RANGE,
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["plant__sequence", "stage", "sequence", "parameter_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "stage", "parameter_key"],
                name="uq_etp_parameter_plant_stage_key",
            ),
        ]
        verbose_name = "Monitoring Parameter"
        verbose_name_plural = "Monitoring Parameters"

    def __str__(self):
        return f"{self.get_stage_display()} — {self.parameter_name}"

    def is_within_spec(self, value):
        """True / False / None (None = nothing to check against)."""
        if value is None or self.validation_type == SpecValidationType.NONE:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        low = float(self.min_value) if self.min_value is not None else None
        high = float(self.max_value) if self.max_value is not None else None
        if self.validation_type == SpecValidationType.MIN:
            return None if low is None else numeric >= low
        if self.validation_type == SpecValidationType.MAX:
            return None if high is None else numeric <= high
        if low is None and high is None:
            return None
        if low is not None and numeric < low:
            return False
        if high is not None and numeric > high:
            return False
        return True


# ---------------------------------------------------------------------------
# Register 1 — daily plant log (flow meters, pH, energy meter)
# ---------------------------------------------------------------------------


class DailyPlantLog(BaseModel):
    """One day of the "Effluent Treatment Plant Record" for one plant.

    The three totals are derived from the meter pairs on save, exactly the way
    the paper form's TOTAL columns are worked out by hand. Today's initial
    readings carry forward from yesterday's finals (done in the serializer), so
    an operator normally types only the three closing figures and the pH.
    """

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.PROTECT, related_name="daily_logs"
    )
    date = models.DateField()

    inlet_initial = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Inlet flow meter opening reading (KL).",
    )
    inlet_final = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Inlet flow meter closing reading (KL).",
    )
    inlet_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Derived: inlet final − inlet initial (KL).",
    )

    outlet_initial = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    outlet_final = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    outlet_total = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Derived: outlet final − outlet initial (KL).",
    )

    ph_reading = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="pH meter reading taken with the day's log.",
    )
    ph_reading_time = models.TimeField(null=True, blank=True)

    energy_initial = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    energy_final = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    energy_units = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Derived: energy final − energy initial (units).",
    )

    operator = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="daily_logs_as_operator",
    )
    chemist = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="daily_logs_as_chemist",
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date", "plant__sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "date"], name="uq_etp_daily_log_plant_date"
            ),
        ]
        verbose_name = "Daily Plant Log"
        verbose_name_plural = "Daily Plant Logs"

    @staticmethod
    def _difference(final, initial):
        if final is None or initial is None:
            return Decimal("0")
        return Decimal(str(final)) - Decimal(str(initial))

    def save(self, *args, **kwargs):
        self.inlet_total = self._difference(self.inlet_final, self.inlet_initial)
        self.outlet_total = self._difference(self.outlet_final, self.outlet_initial)
        self.energy_units = self._difference(self.energy_final, self.energy_initial)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.plant.code} {self.date}: in {self.inlet_total} KL"


# ---------------------------------------------------------------------------
# Register 2 — on-line monitoring record (two-hourly grid)
# ---------------------------------------------------------------------------


class MonitoringRecord(BaseModel):
    """One day's on-line monitoring sheet for one plant.

    The sheet is a grid: a row per reading time, a column per
    :class:`MonitoringParameter`. ``interval_hours`` only drives the time slots
    the form offers (the paper form is every two hours) — a reading may be added
    at any time.
    """

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.PROTECT, related_name="monitoring_records"
    )
    date = models.DateField()
    interval_hours = models.PositiveSmallIntegerField(
        default=2, help_text="Sampling frequency the sheet is filled at, in hours."
    )
    chemist = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="monitoring_records_as_chemist",
        help_text="QA chemist who signs the sheet.",
    )
    verified_by = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="monitoring_records_as_verifier",
        help_text="QAM who countersigns the sheet.",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date", "plant__sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "date"], name="uq_etp_monitoring_plant_date"
            ),
        ]
        verbose_name = "On-line Monitoring Record"
        verbose_name_plural = "On-line Monitoring Records"

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def __str__(self):
        return f"{self.plant.code} monitoring {self.date}"


class MonitoringReading(BaseModel):
    """One time slot on the monitoring sheet."""

    record = models.ForeignKey(
        MonitoringRecord, on_delete=models.CASCADE, related_name="readings"
    )
    reading_time = models.TimeField()
    operator = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="monitoring_readings",
    )
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["reading_time", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "reading_time"], name="uq_etp_reading_record_time"
            ),
        ]

    def __str__(self):
        return f"{self.record_id} @ {self.reading_time}"


class MonitoringValue(BaseModel):
    """One cell of the monitoring grid: a parameter's value at a reading time.

    A blank cell is simply no row — the paper form leaves the influent columns
    empty on the night shifts, and the register must be able to say the same.
    """

    reading = models.ForeignKey(
        MonitoringReading, on_delete=models.CASCADE, related_name="values"
    )
    parameter = models.ForeignKey(
        MonitoringParameter, on_delete=models.PROTECT, related_name="values"
    )
    value = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # Denormalised flag so a list / print never has to re-run the spec check.
    is_out_of_spec = models.BooleanField(default=False)

    class Meta:
        ordering = ["parameter__stage", "parameter__sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["reading", "parameter"], name="uq_etp_value_reading_parameter"
            ),
        ]

    def save(self, *args, **kwargs):
        within = self.parameter.is_within_spec(self.value)
        self.is_out_of_spec = within is False
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.parameter.parameter_name}={self.value}"


# ---------------------------------------------------------------------------
# Register 3 — chemical consumption
# ---------------------------------------------------------------------------


class ChemicalConsumptionLog(BaseModel):
    """One day of the chemical consumption register for one plant."""

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.PROTECT, related_name="chemical_logs"
    )
    date = models.DateField()
    operator = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="chemical_logs_as_operator",
    )
    verified_by = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="chemical_logs_as_verifier",
    )
    remarks = models.TextField(
        blank=True,
        default="",
        help_text="Free note — the paper form uses it for stock received, e.g. "
        "'200 kg DAP received 24/04/2026'.",
    )

    class Meta:
        ordering = ["-date", "plant__sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["plant", "date"], name="uq_etp_chemical_log_plant_date"
            ),
        ]
        verbose_name = "Chemical Consumption Log"
        verbose_name_plural = "Chemical Consumption Logs"

    def __str__(self):
        return f"{self.plant.code} chemicals {self.date}"


class ChemicalConsumptionLine(BaseModel):
    """How much of one chemical was dosed on that day.

    ``uom`` is snapshotted from the chemical master at entry time so changing a
    chemical's default unit later never re-scales history.
    """

    log = models.ForeignKey(
        ChemicalConsumptionLog, on_delete=models.CASCADE, related_name="lines"
    )
    chemical = models.ForeignKey(
        PlantChemical, on_delete=models.PROTECT, related_name="consumption_lines"
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0"))],
    )
    uom = models.CharField(max_length=5, choices=ChemicalUom.choices)

    class Meta:
        ordering = ["chemical__sequence", "chemical__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["log", "chemical"], name="uq_etp_chemical_line_log_chemical"
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.uom:
            self.uom = self.chemical.default_uom
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.chemical.name}: {self.quantity} {self.uom}"


# ---------------------------------------------------------------------------
# Register 4 — sludge generation
# ---------------------------------------------------------------------------


class SludgeGenerationEntry(BaseModel):
    """One line of the "Sludge Generation Record".

    ``serial_no`` continues the paper register's running Sr. No. — allocated on
    first save and never reused, so a print matches the book it replaces.
    """

    serial_no = models.PositiveIntegerField(
        unique=True, editable=False, null=True, blank=True
    )
    plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.PROTECT,
        related_name="sludge_entries",
        help_text="Source of the sludge (the ETP or the STP).",
    )
    date = models.DateField()
    quantity_kg = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Quantity of sludge generated, in kg.",
    )
    collection_mode = models.ForeignKey(
        PlantOption,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sludge_collection_entries",
        help_text="Mode of collection, e.g. filter press.",
    )
    storage_method = models.ForeignKey(
        PlantOption,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sludge_storage_entries",
        help_text="Method of storage, e.g. bag.",
    )
    disposal_mode = models.ForeignKey(
        PlantOption,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sludge_disposal_entries",
    )
    operator = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sludge_entries_as_operator",
    )
    supervisor = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sludge_entries_as_supervisor",
    )
    photo = models.ImageField(upload_to="etp/sludge/", null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date", "-serial_no"]
        verbose_name = "Sludge Generation Entry"
        verbose_name_plural = "Sludge Generation Entries"

    def save(self, *args, **kwargs):
        if self.serial_no is None:
            highest = SludgeGenerationEntry.objects.aggregate(m=Max("serial_no"))["m"]
            self.serial_no = (highest or 0) + 1
        super().save(*args, **kwargs)

    def __str__(self):
        return f"#{self.serial_no} {self.date} {self.plant.code}: {self.quantity_kg} kg"


# ---------------------------------------------------------------------------
# Register 5 — daily back washing
# ---------------------------------------------------------------------------


class BackwashEntry(BaseModel):
    """One back-wash / rinse step performed on one day.

    ``contact_minutes`` is derived from start and stop; a stop time before the
    start is read as crossing midnight.
    """

    plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.PROTECT, related_name="backwash_entries"
    )
    date = models.DateField()
    equipment = models.ForeignKey(
        BackwashEquipment, on_delete=models.PROTECT, related_name="entries"
    )
    chemical = models.ForeignKey(
        PlantChemical,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="backwash_entries",
        help_text="Type of chemical used, when any.",
    )
    chemical_quantity = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    start_time = models.TimeField()
    stop_time = models.TimeField(null=True, blank=True)
    contact_minutes = models.PositiveIntegerField(
        default=0, help_text="Derived: stop − start, in minutes."
    )
    operator = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="backwash_entries_as_operator",
    )
    chemist = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="backwash_entries_as_chemist",
    )
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-date", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["equipment", "date", "start_time"],
                name="uq_etp_backwash_equipment_date_start",
            ),
        ]
        verbose_name = "Back-washing Entry"
        verbose_name_plural = "Back-washing Entries"

    def save(self, *args, **kwargs):
        if self.start_time and self.stop_time:
            base = datetime(2000, 1, 1)
            start = datetime.combine(base, _as_time(self.start_time))
            stop = datetime.combine(base, _as_time(self.stop_time))
            if stop < start:  # crossed midnight
                stop += timedelta(days=1)
            self.contact_minutes = int((stop - start).total_seconds() // 60)
        else:
            self.contact_minutes = 0
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} {self.equipment.name} {self.start_time}"


# ---------------------------------------------------------------------------
# Register 6 — instrument calibration
# ---------------------------------------------------------------------------


class CalibrationInstrument(BaseModel):
    """An instrument on the calibration register (the plant pH meter today).

    The header block of the paper form — equipment id, location, working range,
    frequency, and the standard equipment it is calibrated against — is master
    data, typed once here instead of on every page of the register.
    """

    plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="instruments",
    )
    equipment_name = models.CharField(max_length=150)
    equipment_id = models.CharField(max_length=60, unique=True)
    line_id = models.CharField(max_length=60, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    working_range = models.CharField(
        max_length=60, blank=True, default="", help_text="e.g. '0 - 14'."
    )
    frequency = models.CharField(
        max_length=12,
        choices=CalibrationFrequency.choices,
        default=CalibrationFrequency.WEEKLY,
    )
    tolerance = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal("0.200"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=(
            "Largest variation from the standard that still counts as in "
            "calibration. A reading beyond this flags the instrument."
        ),
    )

    standard_make_model = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Standard equipment used for the calibration, e.g. 'ADVIT PH14'.",
    )
    standard_equipment_id = models.CharField(max_length=60, blank=True, default="")
    standard_range = models.CharField(max_length=60, blank=True, default="")
    external_calibration_date = models.DateField(null=True, blank=True)
    external_calibration_due_date = models.DateField(null=True, blank=True)

    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "equipment_name"]
        verbose_name = "Calibration Instrument"
        verbose_name_plural = "Calibration Instruments"

    def __str__(self):
        return f"{self.equipment_name} ({self.equipment_id})"

    def next_due_date(self, from_date):
        """The due date the register should print for a calibration on ``from_date``."""
        days = CALIBRATION_FREQUENCY_DAYS.get(self.frequency, 7)
        return from_date + timedelta(days=days)


class CalibrationPoint(BaseModel):
    """A standard value the instrument is checked at (pH buffer 4.00 / 7.00 / 10.01).

    Configured per instrument, so the calibration form offers exactly the rows
    the paper form has.
    """

    instrument = models.ForeignKey(
        CalibrationInstrument, on_delete=models.CASCADE, related_name="points"
    )
    actual_value = models.DecimalField(
        max_digits=12, decimal_places=3, help_text="The standard / buffer value."
    )
    label = models.CharField(max_length=60, blank=True, default="")
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "actual_value"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "actual_value"],
                name="uq_etp_calibration_point_value",
            ),
        ]
        verbose_name = "Calibration Point"
        verbose_name_plural = "Calibration Points"

    def __str__(self):
        return f"{self.instrument.equipment_id} @ {self.actual_value}"


class CalibrationRecord(BaseModel):
    """One calibration of one instrument: its readings plus the verdict.

    ``due_date`` and ``is_out_of_calibration`` are derived — the due date from
    the instrument's frequency, the verdict from whether every reading came in
    inside the instrument's tolerance (recomputed by
    :meth:`recompute_from_readings` once the readings are saved).
    """

    instrument = models.ForeignKey(
        CalibrationInstrument, on_delete=models.PROTECT, related_name="records"
    )
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    due_date = models.DateField(
        null=True, blank=True, help_text="Derived from the instrument's frequency."
    )
    corrective_action = models.ForeignKey(
        PlantOption,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calibration_records",
        help_text="Corrective observation, e.g. 'Nil' / 'Adjusted' / 'Replaced'.",
    )
    is_out_of_calibration = models.BooleanField(
        default=False, help_text="Derived: any reading outside the tolerance."
    )
    was_replaced = models.BooleanField(default=False)
    checked_by = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calibration_records_checked",
    )
    verified_by = models.ForeignKey(
        PlantStaff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calibration_records_verified",
    )
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-date", "-time"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "date", "time"],
                name="uq_etp_calibration_instrument_date_time",
            ),
        ]
        verbose_name = "Calibration Record"
        verbose_name_plural = "Calibration Records"

    def save(self, *args, **kwargs):
        if self.date and not self.due_date:
            self.due_date = self.instrument.next_due_date(self.date)
        super().save(*args, **kwargs)

    def recompute_from_readings(self, save=True):
        """Set the out-of-calibration verdict from the saved readings."""
        self.is_out_of_calibration = self.readings.filter(
            is_within_tolerance=False
        ).exists()
        if save:
            self.save(update_fields=["is_out_of_calibration", "updated_at"])
        return self.is_out_of_calibration

    def __str__(self):
        return f"{self.instrument.equipment_id} calibrated {self.date}"


class CalibrationReading(BaseModel):
    """One standard-vs-observed pair. Variation and verdict are derived."""

    record = models.ForeignKey(
        CalibrationRecord, on_delete=models.CASCADE, related_name="readings"
    )
    actual_value = models.DecimalField(
        max_digits=12, decimal_places=3, help_text="Standard / buffer value."
    )
    observed_value = models.DecimalField(
        max_digits=12, decimal_places=3, null=True, blank=True
    )
    variation = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=Decimal("0"),
        help_text="Derived: observed − actual.",
    )
    is_within_tolerance = models.BooleanField(default=True)
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["actual_value", "id"]

    def save(self, *args, **kwargs):
        if self.observed_value is None:
            self.variation = Decimal("0")
            self.is_within_tolerance = True
        else:
            self.variation = Decimal(str(self.observed_value)) - Decimal(
                str(self.actual_value)
            )
            tolerance = Decimal(str(self.record.instrument.tolerance or 0))
            self.is_within_tolerance = abs(self.variation) <= tolerance
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.actual_value} → {self.observed_value} ({self.variation})"


# ---------------------------------------------------------------------------
# Change log — who touched a register row, when, and what moved
# ---------------------------------------------------------------------------


class RegisterChangeLog(models.Model):
    """One line of the registers' edit trail.

    A treatment-plant register is a controlled record, so a value that changes
    after the fact has to be attributable. ``BaseModel`` only keeps a
    last-write stamp (``updated_by`` / ``updated_at``), which cannot answer "what
    did it say before?" — this model does: every create, edit, delete and
    verification writes a row with the field-level before/after.

    Deliberately NOT a ``BaseModel``: a log line is never edited or deactivated,
    it only ever gets appended.
    """

    register = models.CharField(max_length=20, choices=RegisterKey.choices)
    action = models.CharField(max_length=10, choices=ChangeAction.choices)

    # The row that changed. Kept as a plain integer (not a FK) so the trail
    # survives the row being deleted.
    object_id = models.PositiveIntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=60, blank=True, default="")

    # Denormalised so the trail can be filtered next to the register it belongs
    # to, without joining a row that may no longer exist.
    plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="change_logs",
    )
    entry_date = models.DateField(
        null=True, blank=True, help_text="The date of the register row that changed."
    )

    #: ``{field: {"from": <old>, "to": <new>}}`` — empty on a create.
    changes = models.JSONField(default=dict, blank=True)
    #: One-line human summary, e.g. "pH reading 7.84 → 7.60".
    summary = models.CharField(max_length=500, blank=True, default="")

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="etp_register_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["register", "-changed_at"]),
            models.Index(fields=["register", "object_id"]),
            models.Index(fields=["entry_date"]),
        ]
        verbose_name = "Register Change Log"
        verbose_name_plural = "Register Change Log"

    def __str__(self):
        return f"{self.get_register_display()} {self.action} #{self.object_id}"


class EtpPrintDocument(BaseModel):
    """The controlled-document identity printed on one ETP / STP form.

    Mirrors ``quality_control.QCPrintDocument``: the number lives in the database
    and is maintained from the Settings screen, so QA can correct a code or bump
    a revision without a release.

    ``company`` is optional and means "which company's controlled-document set
    this row belongs to". A row with no company is the factory-wide default; a
    row naming a company overrides it for that company only — the Ganaur plants
    are shared, so most installations need the default row alone.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="etp_print_documents",
        help_text="Leave empty for every company (the usual case).",
    )
    document_key = models.CharField(max_length=40, choices=PrintDocumentKey.choices)

    form_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Name printed in the header, e.g. 'SLUDGE GENERATION RECORD'.",
    )
    document_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Controlled-document code, e.g. 'QA-FRM-14-00-08-06'.",
    )
    revision = models.CharField(
        max_length=10,
        blank=True,
        default="00",
        help_text="Revision number as printed, e.g. '00'.",
    )
    issue_date = models.DateField(
        null=True, blank=True, help_text="Issue / revision date of this form."
    )
    document_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Optional per-copy document ID printed in the footer, the way the QC "
            "prints carry one."
        ),
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["document_key", "company__code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "document_key"],
                name="uq_etp_print_document_company_key",
            ),
            # A single factory-wide row per form (company IS NULL is not covered
            # by the constraint above on PostgreSQL).
            models.UniqueConstraint(
                fields=["document_key"],
                condition=models.Q(company__isnull=True),
                name="uq_etp_print_document_default_key",
            ),
        ]
        verbose_name = "ETP Print Document"
        verbose_name_plural = "ETP Print Documents"

    def __str__(self):
        scope = self.company.code if self.company_id else "all companies"
        return f"{self.get_document_key_display()} ({scope}): {self.document_code or '—'}"
