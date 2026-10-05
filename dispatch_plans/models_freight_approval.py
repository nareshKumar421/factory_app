"""
dispatch_plans/models_freight_approval.py

A truck's freight held against the benchmark for where it is going.

When dispatch links a vehicle to its bills it picks the destination (from the
Freight Benchmarks list), the slab (defaulting to the one the vehicle's capacity
falls in, but the operator's to change) and enters the freight actually agreed.
The benchmark for that destination and slab is written down beside it, and:

  - freight at or under the benchmark needs nothing: the row is kept, marked
    WITHIN_BENCHMARK, so the queue can show what was checked, not only what was
    flagged;
  - freight over it -- or a destination with no benchmark for that slab, where
    there is nothing to be under -- is PENDING until an approver in Admin >
    Freight Approvals clears or refuses it.

Until it is cleared the gate refuses the truck's DISPATCH empty-vehicle gate-in,
the same way it refuses a late one without approval: dispatch owns the question,
an approver owns the answer, the gate only reads it.

Everything the approver reads is a snapshot taken at linking time -- the rate,
the capacity, the bills -- so the decision still reads as it was made after the
benchmarks are revised or the plans churn. A relink that changes the freight,
the destination, the slab or the bills supersedes the row rather than editing
it, so an approval is never quietly carried over to a price nobody approved.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .models_freight_benchmark import FreightDestination, FreightRateBasis, FreightSlab


class FreightApprovalStatus(models.TextChoices):
    WITHIN_BENCHMARK = "WITHIN_BENCHMARK", "Within benchmark"
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    # Replaced by a later link of the same truck's bills at a different freight,
    # destination, slab or set of bills. Kept for the trail; never read by the gate.
    SUPERSEDED = "SUPERSEDED", "Superseded"


# The states that still speak for the truck. SUPERSEDED rows are history.
LIVE_FREIGHT_APPROVAL_STATUSES = (
    FreightApprovalStatus.WITHIN_BENCHMARK,
    FreightApprovalStatus.PENDING,
    FreightApprovalStatus.APPROVED,
    FreightApprovalStatus.REJECTED,
)

# What lets the truck in. PENDING waits for a decision; REJECTED waits for a
# relink at a freight somebody will clear.
CLEARED_FREIGHT_APPROVAL_STATUSES = (
    FreightApprovalStatus.WITHIN_BENCHMARK,
    FreightApprovalStatus.APPROVED,
)


class DispatchFreightApproval(models.Model):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="dispatch_freight_approvals",
    )
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="dispatch_freight_approvals",
    )
    transporter = models.ForeignKey(
        "vehicle_management.Transporter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_freight_approvals",
    )

    destination = models.ForeignKey(
        FreightDestination,
        on_delete=models.PROTECT,
        related_name="freight_approvals",
    )
    destination_label = models.CharField(max_length=220)
    slab = models.ForeignKey(
        FreightSlab,
        on_delete=models.PROTECT,
        related_name="freight_approvals",
    )
    slab_label = models.CharField(max_length=40)
    # The slab the vehicle's capacity fell in, when there was one. Different from
    # `slab` when the operator chose another, which the approver is shown.
    suggested_slab = models.ForeignKey(
        FreightSlab,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    vehicle_capacity_kg = models.PositiveIntegerField(null=True, blank=True)

    # The benchmark as it stood when the truck was linked. All three empty when
    # the destination has no rate on the chosen slab.
    rate_basis = models.CharField(
        max_length=10, choices=FreightRateBasis.choices, blank=True, default=""
    )
    rate_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # The load a per-kg rate was multiplied by: the bills' invoice weight.
    load_kg = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    benchmark_freight = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    actual_freight = models.DecimalField(max_digits=18, decimal_places=2)

    # The load, as the approver needs to read it after the plans have moved on.
    bill_doc_nums = models.TextField(blank=True, default="")
    customer_names = models.TextField(blank=True, default="")
    bill_count = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=20,
        choices=FreightApprovalStatus.choices,
        default=FreightApprovalStatus.PENDING,
    )
    # Dispatch's word on why the freight is over the benchmark.
    reason = models.TextField(blank=True, default="")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_freight_requests",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatch_freight_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        default_permissions = ()
        indexes = [
            models.Index(fields=["status", "requested_at"]),
            models.Index(fields=["vehicle", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(actual_freight__gte=0),
                name="freight_approval_actual_not_negative",
            ),
        ]
        permissions = [
            ("can_view_freight_approvals", "Can view dispatch freight approvals"),
            (
                "can_approve_freight_approvals",
                "Can approve or reject dispatch freight over its benchmark",
            ),
        ]

    def __str__(self):
        return f"{self.vehicle_id} → {self.destination_label}: {self.actual_freight} ({self.status})"

    @property
    def excess(self):
        if self.benchmark_freight is None:
            return None
        return self.actual_freight - self.benchmark_freight

    @property
    def is_cleared(self) -> bool:
        return self.status in CLEARED_FREIGHT_APPROVAL_STATUSES

    def mark_reviewed(self, *, status, reviewer, notes=""):
        self.status = status
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.review_notes = notes or ""
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"])
