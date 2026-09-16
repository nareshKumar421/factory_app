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
    # The BILL this approval covers. One request is raised per bill that is short, so the
    # admin approves the goods that are actually missing: a truck carrying a fully scanned
    # Mart bill and two short Oil ones produced a single request filed against the Mart
    # docking, which asked the admin to approve the one bill that was complete.
    # Null on rows written before approvals named a bill -- those still cover the whole load.
    document = models.ForeignKey(
        "gate_core.SalesDispatchGateOutDocument",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="partial_scan_requests",
        help_text="The bill this partial-dispatch approval covers.",
    )

    scanned_boxes = models.PositiveIntegerField(
        default=0, help_text="Boxes scanned against this bill when the request was raised."
    )
    expected_boxes = models.PositiveIntegerField(
        default=0, help_text="Boxes the bill expects, as the scan page counts them."
    )
    # A bill of goods SAP does not box (SalFactor2 = 1) has no box target at all, so the
    # box pair alone reads "0 of 0" on an approval covering 300 unscanned tins. The
    # quantity pair is the one that is always meaningful -- pieces, or cartons for CSD.
    scanned_pieces = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Quantity scanned against this bill when the request was raised.",
    )
    expected_pieces = models.DecimalField(
        max_digits=14, decimal_places=3, default=0,
        help_text="Quantity the bill invoices, excluding scan-exempt lines.",
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
        constraints = [
            # One live request per BILL. Postgres treats NULLs as distinct, so this index
            # does not constrain the legacy load-wide shape -- the second constraint does.
            models.UniqueConstraint(
                fields=["sales_dispatch", "document"],
                condition=Q(status="PENDING"),
                name="unique_pending_partial_scan_per_bill",
            ),
            models.UniqueConstraint(
                fields=["sales_dispatch"],
                condition=Q(status="PENDING", document__isnull=True),
                name="unique_pending_partial_scan_per_dispatch",
            ),
        ]
        permissions = [
            ("can_request_docking_partial_scan", "Can request to dispatch a docking with a partial box scan"),
            ("can_view_docking_partial_scan", "Can view docking partial scan requests"),
            ("can_approve_docking_partial_scan", "Can approve or reject docking partial scan requests"),
        ]

    def __str__(self):
        bill = self.document_id or self.sales_dispatch_id
        return f"PartialScan #{self.id} - bill {bill} ({self.status})"

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


def approval_attachment_path(instance, filename):
    """Keep skip-request and partial-request evidence in separate folders."""
    bucket = "scan-skip" if instance.scan_skip_request_id else "partial-scan"
    return f"docking_admin/approval_attachments/{bucket}/{filename}"


class DockingApprovalAttachment(models.Model):
    """A file the approver attached while reviewing a docking scan approval.

    An approver is being asked to let goods leave the gate that nobody scanned, so the
    paperwork behind that decision -- the mail authorising it, a signed slip, a photo of
    the load -- belongs on the approval itself rather than in the approver's inbox. One
    row per file; one review may carry several.

    Exactly one of the two request FKs is set: the zero-scan (skip) and partial-scan
    queues are separate models sharing one review flow, so this table serves both rather
    than being duplicated per queue.
    """

    scan_skip_request = models.ForeignKey(
        DockingScanSkipRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    partial_scan_request = models.ForeignKey(
        DockingPartialScanRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )

    file = models.FileField(upload_to=approval_attachment_path)
    # Storage mangles a clashing name (foo_a8Fz1.pdf); the approver should still see the
    # name they picked, and the size lets the queue warn before a slow download.
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="docking_approval_attachments",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]
        verbose_name = "Docking Approval Attachment"
        verbose_name_plural = "Docking Approval Attachments"
        indexes = [
            models.Index(fields=["scan_skip_request"]),
            models.Index(fields=["partial_scan_request"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scan_skip_request__isnull=False, partial_scan_request__isnull=True)
                    | Q(scan_skip_request__isnull=True, partial_scan_request__isnull=False)
                ),
                name="approval_attachment_belongs_to_one_request",
            )
        ]

    def __str__(self):
        owner = (
            f"skip #{self.scan_skip_request_id}"
            if self.scan_skip_request_id
            else f"partial #{self.partial_scan_request_id}"
        )
        return f"{owner} - {self.original_filename or self.file.name}"
