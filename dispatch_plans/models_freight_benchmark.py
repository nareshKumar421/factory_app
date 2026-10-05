"""
dispatch_plans/models_freight_benchmark.py

What a truckload SHOULD cost to move, by destination and vehicle size -- the
benchmark an actual freight is held against when a vehicle is linked.

These are the company's own benchmark rates, not any transporter's quote. The
dispatch desk keeps them in the "UPDATED TRANSPORT FARE" workbook, where each
sheet puts the benchmark columns first (5 MT, 10 MT, ... or, for Delhi NCR, kg
bands) and the transporters' own rates to the right of them (DELHI PUNJAB,
MAHAVIR, BHARGAVE, ABHIMAN, ARNAV, AIR TRANS). Only the benchmark is kept here.

A slab is a band of vehicle capacity, open at the bottom and closed at the top:
"10 MT" is anything over 5,000 kg up to 10,000 kg. That is what lets a vehicle
of any size fall into exactly one of a destination's slabs, and why a
destination may not hold rates on two slabs whose bands overlap.
"""

from django.conf import settings
from django.db import models


class FreightSlab(models.Model):
    """A band of vehicle capacity a benchmark is quoted for, e.g. "10 MT"."""

    label = models.CharField(max_length=40, unique=True)
    # Exclusive lower bound, inclusive upper bound, both in kg.
    above_kg = models.PositiveIntegerField(default=0)
    up_to_kg = models.PositiveIntegerField()
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "up_to_kg", "id"]
        default_permissions = ()
        constraints = [
            models.CheckConstraint(
                condition=models.Q(up_to_kg__gt=models.F("above_kg")),
                name="freight_slab_band_not_empty",
            )
        ]

    def __str__(self):
        return self.label

    def overlaps(self, other: "FreightSlab") -> bool:
        return self.above_kg < other.up_to_kg and other.above_kg < self.up_to_kg


class FreightDestination(models.Model):
    """A place the plant ships to, with its benchmark freight per slab."""

    # The state, or "DELHI NCR" for the capital-region groups the workbook
    # quotes as one (e.g. "NCR-GURUGRAM/NOIDA/GHAZIABAD/FARIDABAD").
    state = models.CharField(max_length=60)
    district = models.CharField(max_length=80, blank=True, default="")
    name = models.CharField(max_length=160)
    pin_code = models.CharField(max_length=10, blank=True, default="")
    distance_km = models.PositiveIntegerField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["state", "district", "name"]
        # Nothing gates on the model's own add/change/view rows; the two rights
        # below are the only ones the page and its API read.
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["state", "name"],
                name="unique_freight_destination_per_state",
            )
        ]
        permissions = [
            ("can_view_freight_benchmarks", "Can view the Freight Benchmarks"),
            ("can_manage_freight_benchmarks", "Can edit the Freight Benchmarks"),
        ]

    def __str__(self):
        return f"{self.name} ({self.state})"


class FreightRateBasis(models.TextChoices):
    # A flat amount for the whole truck, which is how nearly every benchmark is
    # quoted.
    PER_TRIP = "PER_TRIP", "Per trip"
    # Rupees per kg of load -- Delhi NCR's 5,001-8,000 kg band ("1.20/KG").
    PER_KG = "PER_KG", "Per kg"


class FreightBenchmark(models.Model):
    """The benchmark freight for one destination in one slab."""

    destination = models.ForeignKey(
        FreightDestination,
        on_delete=models.CASCADE,
        related_name="benchmarks",
    )
    slab = models.ForeignKey(
        FreightSlab,
        on_delete=models.PROTECT,
        related_name="benchmarks",
    )
    basis = models.CharField(
        max_length=10,
        choices=FreightRateBasis.choices,
        default=FreightRateBasis.PER_TRIP,
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["destination", "slab"],
                name="unique_freight_benchmark_per_slab",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="freight_benchmark_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.destination.name} · {self.slab.label}: {self.amount}"
