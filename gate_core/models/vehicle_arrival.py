from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .base import BaseModel


class VehicleArrivalStatus(models.TextChoices):
    INSIDE = "INSIDE", "Inside"
    LOADING = "LOADING", "Loading"
    DEPARTED = "DEPARTED", "Departed"
    CANCELLED = "CANCELLED", "Cancelled"


class VehicleArrival(BaseModel):
    """One physical truck trip, anchoring its per-company dispatch chains.

    NOT company-scoped: a single truck can carry bills for several companies, so
    one arrival threads the per-company gate-ins (``EmptyVehicleGateIn``) and
    dockings (``SalesDispatchGateOut``) together — each references this arrival via
    a nullable ``arrival`` FK. ``Vehicle``/``Driver`` are global. The truck is
    weighed once (``tare_weight``) and leaves once (the physical exit fields). A
    single-company truck is just an arrival whose chain touches one company;
    legacy records keep ``arrival=null`` and behave as before.
    """

    arrival_no = models.CharField(max_length=50, unique=True)
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="arrivals",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        related_name="arrivals",
    )
    gate_in_date = models.DateField()
    in_time = models.TimeField()
    tare_weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    weighbridge_slip_no = models.CharField(max_length=50, blank=True)
    security_name = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=VehicleArrivalStatus.choices,
        default=VehicleArrivalStatus.INSIDE,
    )

    # Single physical exit for the whole trip.
    gate_out_date = models.DateField(null=True, blank=True)
    out_time = models.TimeField(null=True, blank=True)
    exit_security_name = models.CharField(max_length=100, blank=True)
    departed_at = models.DateTimeField(null=True, blank=True)
    departed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_arrivals_departed",
    )

    cancel_reason = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_arrivals_cancelled",
    )

    # One combined gate-exit gatepass for the whole truck trip (its own number);
    # each per-company docking still carries its DCK/... number for SAP/GST records.
    gatepass_no = models.CharField(max_length=80, unique=True, null=True, blank=True)
    gatepass_random_code = models.CharField(max_length=50, blank=True)
    gatepass_printed_at = models.DateTimeField(null=True, blank=True)
    gatepass_printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_arrivals_gatepass_printed",
    )
    gatepass_committed_at = models.DateTimeField(null=True, blank=True)
    gatepass_committed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vehicle_arrivals_gatepass_committed",
    )

    class Meta:
        ordering = ["-gate_in_date", "-in_time", "-created_at"]
        indexes = [
            models.Index(fields=["vehicle"]),
            models.Index(fields=["status"]),
            models.Index(fields=["gate_in_date"]),
        ]
        constraints = [
            # One physical truck has at most one open trip at a time. This is the
            # DB backstop behind ``resolve_open_arrival_for_vehicle`` that stops the
            # "multiple entries for one truck" duplication under any race.
            models.UniqueConstraint(
                fields=["vehicle"],
                condition=models.Q(
                    is_active=True, status__in=["INSIDE", "LOADING"]
                ),
                name="uniq_open_arrival_per_vehicle",
            ),
        ]

    def __str__(self):
        return self.arrival_no

    @property
    def is_open(self):
        return self.status in (
            VehicleArrivalStatus.INSIDE,
            VehicleArrivalStatus.LOADING,
        )

    @property
    def company_ids(self):
        return list(
            self.gate_ins.filter(is_active=True)
            .values_list("company_id", flat=True)
            .distinct()
        )

    @staticmethod
    def generate_arrival_no():
        today = timezone.now()
        prefix = f"ARV-{today.strftime('%Y%m%d')}"
        last = (
            VehicleArrival.objects
            .filter(arrival_no__startswith=prefix)
            .order_by("-arrival_no")
            .first()
        )
        if last:
            try:
                next_number = int(last.arrival_no.split("-")[-1]) + 1
            except ValueError:
                next_number = 1
        else:
            next_number = 1
        return f"{prefix}-{next_number:04d}"


class ArrivalGatepassSequence(models.Model):
    """Company-agnostic running number for the combined truck gatepass.

    Mirrors ``SalesDispatchGatepassSequence`` but not company-scoped: one physical
    truck trip gets one ``ARV/{FY}/{seq}`` number regardless of how many companies
    it carries.
    """

    financial_year = models.CharField(max_length=9, unique=True)
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ARV {self.financial_year}: {self.last_number}"

    @classmethod
    def next_gatepass_no(cls):
        today = timezone.localdate()
        start_year = today.year if today.month >= 4 else today.year - 1
        financial_year = f"{start_year}-{str(start_year + 1)[-2:]}"
        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(
                financial_year=financial_year, defaults={"last_number": 0}
            )
            sequence.last_number += 1
            sequence.save(update_fields=["last_number", "updated_at"])
            return f"ARV/{financial_year}/{sequence.last_number:06d}"
