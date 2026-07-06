from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from gate_core.models.base import BaseModel


class DockingScanSkipStatus(models.TextChoices):
    """Lifecycle of an operator's request to skip box scanning for a docking entry."""

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class DockingScanSkipRequest(BaseModel):
    """
    Operator request to skip box scanning for a whole Docking (sales-dispatch) entry.

    Raised from the docking scanning page when boxes cannot be scanned. An admin
    reviews it from Admin > Docking. Approval lets the operator continue past the
    scanning step without scanning; while pending the operator is hard-gated.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="docking_scan_skip_requests",
    )
    sales_dispatch = models.ForeignKey(
        "gate_core.SalesDispatchGateOut",
        on_delete=models.CASCADE,
        related_name="scan_skip_requests",
    )

    reason = models.TextField(help_text="Why box scanning should be skipped for this docking entry.")
    status = models.CharField(
        max_length=20,
        choices=DockingScanSkipStatus.choices,
        default=DockingScanSkipStatus.PENDING,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="docking_scan_skip_requests",
    )
    requested_at = models.DateTimeField(default=timezone.now)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="docking_scan_skip_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        verbose_name = "Docking Scan Skip Request"
        verbose_name_plural = "Docking Scan Skip Requests"
        indexes = [
            models.Index(
                fields=["company", "status"],
                name="dockskip_co_status_idx",
            ),
            models.Index(
                fields=["company", "-requested_at"],
                name="dockskip_co_reqat_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch"],
                condition=Q(status="PENDING"),
                name="unique_pending_scan_skip_per_dispatch",
            )
        ]
        permissions = [
            ("can_request_docking_scan_skip", "Can request to skip docking box scanning"),
            ("can_view_docking_scan_skip", "Can view docking scan skip requests"),
            ("can_approve_docking_scan_skip", "Can approve or reject docking scan skip requests"),
        ]

    def __str__(self):
        return f"ScanSkip #{self.id} - {self.sales_dispatch_id} ({self.status})"

    @property
    def is_pending(self):
        return self.status == DockingScanSkipStatus.PENDING

    @property
    def is_approved(self):
        return self.status == DockingScanSkipStatus.APPROVED

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


class DockingPartialScanRequest(BaseModel):
    """
    Operator request to dispatch a Docking with only *some* of its boxes scanned.

    Raised from the docking scanning page once at least one box is scanned but the
    full expected count is not reached. An admin reviews it from Admin > Partial
    Dispatch Approvals. Approval lets the load proceed to gatepass with the partial
    scan; while pending the operator is hard-gated. Mirrors ``DockingScanSkipRequest``
    (which instead covers the zero-scan case) and shares its PENDING/APPROVED/REJECTED
    lifecycle.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="docking_partial_scan_requests",
    )
    sales_dispatch = models.ForeignKey(
        "gate_core.SalesDispatchGateOut",
        on_delete=models.CASCADE,
        related_name="partial_scan_requests",
    )

    scanned_boxes = models.PositiveIntegerField(
        default=0, help_text="Boxes scanned when the request was raised."
    )
    expected_boxes = models.PositiveIntegerField(
        default=0, help_text="Expected box count for the docking when the request was raised."
    )
    reason = models.TextField(help_text="Why the load is dispatched with a partial box scan.")
    status = models.CharField(
        max_length=20,
        choices=DockingScanSkipStatus.choices,
        default=DockingScanSkipStatus.PENDING,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="docking_partial_scan_requests",
    )
    requested_at = models.DateTimeField(default=timezone.now)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="docking_partial_scan_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        verbose_name = "Docking Partial Scan Request"
        verbose_name_plural = "Docking Partial Scan Requests"
        indexes = [
            models.Index(
                fields=["company", "status"],
                name="dockpart_co_status_idx",
            ),
            models.Index(
                fields=["company", "-requested_at"],
                name="dockpart_co_reqat_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_dispatch"],
                condition=Q(status="PENDING"),
                name="unique_pending_partial_scan_per_dispatch",
            )
        ]
        permissions = [
            ("can_request_docking_partial_scan", "Can request to dispatch a docking with a partial box scan"),
            ("can_view_docking_partial_scan", "Can view docking partial scan requests"),
            ("can_approve_docking_partial_scan", "Can approve or reject docking partial scan requests"),
        ]

    def __str__(self):
        return f"PartialScan #{self.id} - {self.sales_dispatch_id} ({self.status})"

    @property
    def is_pending(self):
        return self.status == DockingScanSkipStatus.PENDING

    @property
    def is_approved(self):
        return self.status == DockingScanSkipStatus.APPROVED

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
