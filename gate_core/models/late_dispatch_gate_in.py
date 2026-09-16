from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .base import BaseModel


class LateDispatchGateInApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class LateDispatchGateInApproval(BaseModel):
    """Authorisation to gate a DISPATCH empty vehicle in after the evening cutoff.

    A truck that arrives to load after the cutoff (5 PM by default, see
    ``gate_core.services.late_dispatch_gate_in``) cannot realistically be loaded,
    docked and gate-passed the same evening, so letting it in is a decision
    somebody has to own. The gate raises this request from the Empty Vehicle In
    board; an approver clears it from Admin > Late Dispatch Gate-In Approvals; the
    gate then starts the entry.

    Only DISPATCH empty-ins are covered — a repair, job-work or other movement is
    not loading anything and has never been time-bound.

    Scoped to a *vehicle and a date*, not to a gate-in: it is granted **before** the
    ``EmptyVehicleGateIn`` exists, and is spent (``consumed_at``) by the gate-in it
    lets through. The bill snapshot is taken server-side from the truck's booked
    plans at request time, so the approver reads what the truck is actually
    carrying rather than whatever the client claimed.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="late_dispatch_gate_in_approvals",
    )
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="late_dispatch_gate_in_approvals",
    )
    # The date/time the gate is asking to record, which is what tripped the cutoff.
    gate_in_date = models.DateField()
    in_time = models.TimeField()

    # Snapshot of the load the truck is booked to carry, resolved from its BOOKED,
    # unlinked dispatch plans when the request is raised. Kept as a snapshot so the
    # queue still reads sensibly after the plans churn.
    bill_doc_nums = models.TextField(
        blank=True, help_text="Comma-separated SAP invoice numbers the truck is booked to carry."
    )
    customer_names = models.TextField(
        blank=True, help_text="Comma-separated customers on those bills."
    )
    bill_count = models.PositiveIntegerField(default=0)

    reason = models.TextField(help_text="Why this truck is being let in after the cutoff.")
    status = models.CharField(
        max_length=20,
        choices=LateDispatchGateInApprovalStatus.choices,
        default=LateDispatchGateInApprovalStatus.PENDING,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="late_dispatch_gate_in_requests",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="late_dispatch_gate_in_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    # Spent by the gate-in it let through: an approval is good for one entry, so a
    # second late truck on the same approval is impossible.
    empty_vehicle_gate_in = models.ForeignKey(
        "gate_core.EmptyVehicleGateIn",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="late_dispatch_approvals",
    )
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        verbose_name = "Late Dispatch Gate-In Approval"
        verbose_name_plural = "Late Dispatch Gate-In Approvals"
        constraints = [
            # One live request per truck per day: clicking "Start Entry" twice asks
            # the same question twice, and the second copy is what an approver ends
            # up rejecting by mistake.
            models.UniqueConstraint(
                fields=["vehicle", "gate_in_date"],
                condition=Q(status="PENDING", is_active=True),
                name="unique_pending_late_dispatch_gate_in",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["vehicle", "gate_in_date"]),
        ]
        permissions = [
            (
                "can_view_late_dispatch_gate_in",
                "Can view late dispatch gate-in approval requests",
            ),
            (
                "can_approve_late_dispatch_gate_in",
                "Can approve or reject late dispatch gate-in approval requests",
            ),
        ]

    def __str__(self):
        return (
            f"LateDispatchGateIn #{self.id} - {self.vehicle_id} "
            f"{self.gate_in_date} ({self.status})"
        )

    @property
    def is_pending(self):
        return self.status == LateDispatchGateInApprovalStatus.PENDING

    @property
    def is_approved(self):
        return self.status == LateDispatchGateInApprovalStatus.APPROVED

    def mark_reviewed(self, *, status, reviewer, notes=""):
        self.status = status
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.review_notes = notes or ""
        self.updated_by = reviewer
        self.save(
            update_fields=[
                "status",
                "reviewed_by",
                "reviewed_at",
                "review_notes",
                "updated_by",
                "updated_at",
            ]
        )
