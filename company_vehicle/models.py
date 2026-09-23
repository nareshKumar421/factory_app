"""The company's own vehicles, and what they cost to run.

Deliberately separate from ``vehicle_management.Vehicle``. That register is
the gate's: a row is created for every outside truck that arrives, it requires
a transporter, and it is about who delivered what. This one is about the
handful of vehicles the company owns -- the trucks, the cars, the Eeco, the
scooty -- and the two things they cost money for: fuel and service.

Five tables:

``FleetVehicle``   the vehicle itself, entered once.
``DailyReading``   one meter reading on one day. What makes a running log possible.
``FuelEntry``      one filling. The screen that gets used every day.
``ServiceEntry``   one service or repair bill.
``VehicleDocument``insurance, PUC, fitness and the rest, held for their expiry.

Only ``ServiceEntry`` carries an approval: a workshop bill is PENDING until
someone with the approve right passes it, and only an APPROVED one is counted
as spend. A fuel filling has none -- it is a pump slip for a few thousand
rupees, entered daily, and making somebody pass each one was work without a
decision in it. A filling counts the moment it is recorded.

Nothing here posts to the cash book or the accounts module -- ``payment_mode``
records how a bill was paid and stops there.
"""

from django.conf import settings
from django.db import models

from gate_core.models import BaseModel

from .constants import (
    ApprovalStatus,
    DocumentKind,
    FUEL_UNITS,
    FuelType,
    PaymentMode,
    ServiceKind,
    VehicleCategory,
    VehicleStatus,
)


class FleetPermission(models.Model):
    """Sentinel model carrying the module's rights.

    Unmanaged and tableless, the way ``returnable_items.ReturnablePermission``
    and ``maintenance.MaintenancePermission`` do it: the migration mints the
    permission rows, the database gets no table.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Company Vehicle Permission"
        permissions = [
            ("can_view_fleet", "Can view the company vehicle register"),
            ("can_manage_fleet_vehicle", "Can add and edit company vehicles"),
            ("can_add_fleet_expense", "Can record fuel and service entries"),
            ("can_approve_fleet_expense", "Can approve or reject fuel and service entries"),
        ]


def _bill_upload_path(instance, filename):
    """``fleet/<vehicle number>/<kind>/<filename>``.

    Grouped by vehicle so one vehicle's paperwork sits together on disk.
    """
    number = (getattr(instance.vehicle, "vehicle_number", "") or "UNFILED").replace("/", "-")
    return f"fleet/{number}/{instance.UPLOAD_FOLDER}/{filename}"


class ApprovableEntry(BaseModel):
    """The approval half of a money entry.

    Abstract, and used by :class:`ServiceEntry` alone. It stays a mixin rather
    than being folded into that model because the next thing anyone puts
    through an approval -- a tyre account, a hired vehicle -- wants the same
    four columns and the same two transitions.
    """

    approval_status = models.CharField(
        max_length=10,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    #: Why it was sent back. Kept after a later approval, so the history of a
    #: corrected bill is still readable.
    rejection_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        abstract = True

    @property
    def is_counted(self) -> bool:
        """Whether this entry's money belongs in the totals."""
        return self.approval_status == ApprovalStatus.APPROVED


class FleetVehicle(BaseModel):
    """One vehicle the company owns.

    Entered once and then left alone. Only four fields are required -- number,
    category, fuel and status -- so a clerk can get the fleet on to the system
    in one sitting and fill in the rest later.
    """

    vehicle_number = models.CharField(
        max_length=20,
        unique=True,
        help_text="As on the RC, without spaces, e.g. PB65AB1234. For a "
        "machine with no registration, any internal code the factory uses.",
    )
    nickname = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="What the staff actually call it -- 'Truck 1', 'Office "
        "Eeco'. Shown beside the number, because nobody remembers numbers.",
    )
    category = models.CharField(max_length=20, choices=VehicleCategory.choices)
    fuel_type = models.CharField(
        max_length=12,
        choices=FuelType.choices,
        help_text="What it runs on. Decides the unit on the fuel form (litres "
        "or kg) and whether that form offers a petrol/CNG choice.",
    )
    make_model = models.CharField(
        max_length=100, blank=True, default="", help_text="e.g. Tata 407, Maruti Eeco, Activa 6G."
    )
    status = models.CharField(
        max_length=12, choices=VehicleStatus.choices, default=VehicleStatus.ACTIVE, db_index=True
    )

    purchase_date = models.DateField(null=True, blank=True)
    purchase_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    assigned_to = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Driver or employee who keeps it. Free text on purpose: the "
        "drivers of an own vehicle are not always in the driver master.",
    )
    department = models.CharField(max_length=100, blank=True, default="")

    opening_odometer = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The km reading on the day this vehicle went on to the "
        "system. Only used to show distance run before the first fuel entry.",
    )
    photo = models.ImageField(upload_to="fleet/photos/", null=True, blank=True)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["vehicle_number"]
        indexes = [models.Index(fields=["status", "category"])]

    def __str__(self):
        return f"{self.vehicle_number} ({self.nickname})" if self.nickname else self.vehicle_number

    def save(self, *args, **kwargs):
        # One spelling of a number, always: the gate types it with spaces, the
        # office types it in lower case, and both must find the same vehicle.
        self.vehicle_number = (self.vehicle_number or "").upper().replace(" ", "")
        super().save(*args, **kwargs)

    @property
    def fuel_unit(self) -> str:
        """``L`` or ``Kg``. Dual-fuel has no single unit, so it reads ``L/Kg``."""
        if self.fuel_type == FuelType.PETROL_CNG:
            return "L/Kg"
        return FUEL_UNITS.get(self.fuel_type, "L")

    @property
    def last_odometer(self):
        """Highest reading seen anywhere -- a daily reading, a filling, a service.

        The starting point the forms prefill and validate against.
        """
        readings = [
            self.daily_readings.aggregate(m=models.Max("odometer"))["m"],
            self.fuel_entries.aggregate(m=models.Max("odometer"))["m"],
            self.service_entries.aggregate(m=models.Max("odometer"))["m"],
            self.opening_odometer,
        ]
        readings = [r for r in readings if r is not None]
        return max(readings) if readings else None


class DailyReading(BaseModel):
    """One vehicle's meter reading on one day.

    Without this the app knows a truck ran 400 km *between two fillings*, but
    not what it ran on Tuesday -- a meter reading only existed when somebody
    bought fuel. A reading a day turns that into a running log.

    One row per vehicle per day, enforced in the database: a second reading for
    a day is the same fact typed twice, and two of them would make the day's
    distance ambiguous. Re-entering a day overwrites it (the API does an
    upsert), which is what somebody correcting a typo expects.

    A filling records a meter reading too, and that one is not duplicated here
    -- the log reads both tables and takes the day's highest reading, so
    nothing has to be typed twice.
    """

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.CASCADE, related_name="daily_readings"
    )
    reading_date = models.DateField(db_index=True)
    odometer = models.PositiveIntegerField(help_text="Meter reading in km, as it read that day.")
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-reading_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "reading_date"], name="uq_daily_reading_vehicle_date"
            )
        ]
        indexes = [models.Index(fields=["vehicle", "reading_date"])]

    def __str__(self):
        return f"{self.vehicle.vehicle_number} {self.reading_date} {self.odometer}"


class FuelEntry(BaseModel):
    """One filling at a pump.

    The screen a driver or clerk uses every day, so it asks for as little as it
    can: vehicle, date, meter reading, quantity and amount. Rate is derived
    when it is left out, because a pump slip prints the amount and the litres.

    No approval, unlike :class:`ServiceEntry`: a filling counts as spend the
    moment it is recorded. ``created_by`` says who entered it, which is the
    part anyone actually goes back to.

    ``distance_km`` and ``mileage`` are written by
    :func:`company_vehicle.services.recalculate_fuel_metrics`, never typed in.
    """

    UPLOAD_FOLDER = "fuel"

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.CASCADE, related_name="fuel_entries"
    )
    entry_date = models.DateField(db_index=True)
    fuel_type = models.CharField(
        max_length=12,
        choices=FuelType.choices,
        help_text="The ONE fuel actually filled. On a dual-fuel vehicle this "
        "is what separates the petrol stream from the CNG one.",
    )
    odometer = models.PositiveIntegerField(help_text="Meter reading in km at the pump.")
    quantity = models.DecimalField(
        max_digits=9, decimal_places=2, help_text="Litres, or kg for CNG."
    )
    rate = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=11, decimal_places=2)

    is_tank_full = models.BooleanField(
        default=True,
        help_text="Tank filled to the brim. Mileage can only be worked out "
        "between two full tanks, so a part fill is recorded but not measured.",
    )
    station_name = models.CharField(max_length=120, blank=True, default="")
    bill_number = models.CharField(max_length=50, blank=True, default="")
    bill_photo = models.FileField(upload_to=_bill_upload_path, null=True, blank=True)
    payment_mode = models.CharField(
        max_length=10, choices=PaymentMode.choices, default=PaymentMode.CASH
    )
    filled_by = models.CharField(max_length=100, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    #: Set when the reading entered is lower than the last one. Meters do get
    #: replaced and do break, so this is a reason, not a block.
    odometer_note = models.CharField(max_length=255, blank=True, default="")

    # --- derived, recomputed for the whole vehicle on every write ----------
    distance_km = models.PositiveIntegerField(
        null=True, blank=True, help_text="Km run since the previous entry of the same fuel."
    )
    mileage = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Km per litre (per kg for CNG), measured full tank to full "
        "tank. Null on a part fill, and on the first fill of a vehicle.",
    )

    class Meta:
        ordering = ["-entry_date", "-odometer", "-id"]
        verbose_name_plural = "Fuel entries"
        indexes = [
            models.Index(fields=["vehicle", "entry_date"]),
            models.Index(fields=["vehicle", "fuel_type", "odometer"]),
        ]

    def __str__(self):
        return f"{self.vehicle.vehicle_number} {self.entry_date} {self.amount}"

    @property
    def unit(self) -> str:
        return FUEL_UNITS.get(self.fuel_type, "L")


class ServiceEntry(ApprovableEntry):
    """One service, repair or replacement bill."""

    UPLOAD_FOLDER = "service"

    vehicle = models.ForeignKey(
        FleetVehicle, on_delete=models.CASCADE, related_name="service_entries"
    )
    entry_date = models.DateField(db_index=True)
    odometer = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=ServiceKind.choices, default=ServiceKind.ROUTINE)
    workshop_name = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(
        blank=True, default="", help_text="What was done, and which parts were replaced."
    )

    parts_amount = models.DecimalField(max_digits=11, decimal_places=2, default=0)
    labour_amount = models.DecimalField(max_digits=11, decimal_places=2, default=0)
    total_amount = models.DecimalField(
        max_digits=11,
        decimal_places=2,
        help_text="Parts plus labour unless the bill says otherwise -- a "
        "single-figure garage bill is entered here alone.",
    )

    bill_number = models.CharField(max_length=50, blank=True, default="")
    bill_photo = models.FileField(upload_to=_bill_upload_path, null=True, blank=True)
    payment_mode = models.CharField(
        max_length=10, choices=PaymentMode.choices, default=PaymentMode.CASH
    )

    next_service_date = models.DateField(null=True, blank=True)
    next_service_odometer = models.PositiveIntegerField(null=True, blank=True)
    down_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Days the vehicle was off the road for this."
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-entry_date", "-id"]
        verbose_name_plural = "Service entries"
        indexes = [models.Index(fields=["vehicle", "entry_date"])]

    def __str__(self):
        return f"{self.vehicle.vehicle_number} {self.entry_date} {self.get_kind_display()}"


class VehicleDocument(BaseModel):
    """Insurance, PUC, fitness, permit, road tax -- held for the expiry date.

    One table for every kind rather than a column per kind, so "what expires
    in the next 30 days" is one query across the whole fleet, and a new kind
    of paper needs no migration.
    """

    UPLOAD_FOLDER = "documents"

    vehicle = models.ForeignKey(FleetVehicle, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=12, choices=DocumentKind.choices)
    document_number = models.CharField(max_length=60, blank=True, default="")
    issuing_authority = models.CharField(
        max_length=120, blank=True, default="", help_text="Insurance company, RTO, testing centre."
    )
    issue_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(db_index=True)
    amount = models.DecimalField(
        max_digits=11,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Premium or fee paid. Not counted as running cost -- it is "
        "shown on the document, not in the fuel and service totals.",
    )
    file = models.FileField(upload_to=_bill_upload_path, null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["expiry_date"]
        indexes = [models.Index(fields=["vehicle", "doc_type", "expiry_date"])]

    def __str__(self):
        return f"{self.vehicle.vehicle_number} {self.get_doc_type_display()}"
