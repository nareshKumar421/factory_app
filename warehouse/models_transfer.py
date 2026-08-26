"""Warehouse transfer requests — the two-party ask that becomes a SAP transfer.

The flow this models:

    01  source warehouse raises a request      -> SAP inventory transfer request
    02  receiving warehouse approves/rejects   -> app only (reject closes the ITR)
    03  source warehouse posts the transfer    -> SAP inventory transfer (leg 1)
    04  BST is seeded from the posted transfer -> existing scan flow, unchanged
    05  receipt completes                      -> SAP leg 2, cross-branch only

Approval lives entirely here, not in SAP: the Service Layer bypasses SAP's own
approval procedures (proven by posting on a route covered by two active
templates), so nothing shadows the app's decision.

Deliberately mirrors `BOMRequest` in `warehouse/models.py` — the same
requested/approved/transferred triple per line, and the same split between a
business status and a separate SAP-posting status.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class TransferRouteType(models.TextChoices):
    """Decides how many SAP documents the move takes.

    SAP refuses a cross-branch transfer whose destination is not an in-transit
    warehouse (error 5900002), and refuses one leaving in-transit whose branches
    differ (6700001). So a cross-branch move is structurally two documents, and
    the second one is the act of receiving.
    """
    INTRA_BRANCH = "INTRA_BRANCH", "Within one branch"
    CROSS_BRANCH = "CROSS_BRANCH", "Between branches (via in-transit)"


class TransferRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class TransferPostingStatus(models.TextChoices):
    """Where the request has got to in SAP, tracked apart from approval."""
    NOT_POSTED = "NOT_POSTED", "Not Posted"
    IN_TRANSIT = "IN_TRANSIT", "Leg 1 posted — in transit"
    POSTED = "POSTED", "Posted"
    FAILED = "FAILED", "Failed"


class TransferLineStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class WarehouseTransferRequest(models.Model):
    """One ask to move stock between two warehouses of the same company."""

    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='transfer_requests',
    )
    entry_no = models.CharField(max_length=30, unique=True, db_index=True)

    # --- route -------------------------------------------------------------
    from_warehouse = models.CharField(max_length=20)
    to_warehouse = models.CharField(max_length=20)
    route_type = models.CharField(
        max_length=15, choices=TransferRouteType.choices,
        default=TransferRouteType.INTRA_BRANCH,
    )
    from_branch_id = models.IntegerField(
        null=True, blank=True, help_text="OWHS.BPLid of the source warehouse"
    )
    to_branch_id = models.IntegerField(
        null=True, blank=True, help_text="OWHS.BPLid of the destination warehouse"
    )
    intransit_warehouse = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Cross-branch only: the *-INT warehouse leg 1 ships into.",
    )

    # --- approval (app-owned) ---------------------------------------------
    status = models.CharField(
        max_length=25, choices=TransferRequestStatus.choices,
        default=TransferRequestStatus.PENDING,
    )
    remarks = models.TextField(blank=True, default='')
    rejection_reason = models.TextField(blank=True, default='')

    # --- SAP: the request document (OWTQ) ---------------------------------
    sap_request_doc_entry = models.IntegerField(null=True, blank=True)
    sap_request_doc_num = models.CharField(max_length=30, blank=True, default='')
    sap_request_closed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the ITR was closed in SAP, releasing its reservation.",
    )

    # --- SAP: the transfer documents (OWTR) -------------------------------
    posting_status = models.CharField(
        max_length=15, choices=TransferPostingStatus.choices,
        default=TransferPostingStatus.NOT_POSTED,
    )
    sap_transfer_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="Intra-branch: the whole move. Cross-branch: leg 1.",
    )
    sap_transfer_doc_num = models.CharField(max_length=30, blank=True, default='')
    sap_leg2_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="Cross-branch only: in-transit to destination, posted at receipt.",
    )
    sap_leg2_doc_num = models.CharField(max_length=30, blank=True, default='')
    posting_error = models.TextField(
        blank=True, default='',
        help_text="Last SAP rejection, kept so the operator can see why.",
    )

    # --- link to the physical movement ------------------------------------
    bst_transfer = models.ForeignKey(
        'warehouse.BSTTransfer', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='transfer_requests',
        help_text="The BST seeded from the posted transfer.",
    )

    # --- audit -------------------------------------------------------------
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='transfer_requests_raised',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='transfer_requests_reviewed',
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='transfer_requests_posted',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Warehouse Transfer Request"
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['company', 'posting_status']),
            models.Index(fields=['sap_request_doc_entry']),
            models.Index(fields=['sap_transfer_doc_entry']),
        ]
        permissions = [
            ("can_view_transfer_request", "Can view warehouse transfer requests"),
            ("can_create_transfer_request", "Can raise a warehouse transfer request"),
            ("can_approve_transfer_request", "Can approve or reject a transfer request"),
            ("can_post_transfer_to_sap", "Can post an approved transfer to SAP"),
        ]

    def __str__(self):
        return f"{self.entry_no} — {self.from_warehouse} → {self.to_warehouse}"

    @staticmethod
    def generate_entry_no():
        prefix = f"TR-{timezone.now().strftime('%Y%m%d')}"
        last = (
            WarehouseTransferRequest.objects
            .filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )
        if last:
            try:
                next_number = int(last.entry_no.split("-")[-1]) + 1
            except ValueError:
                next_number = 1
        else:
            next_number = 1
        return f"{prefix}-{next_number:04d}"

    # --- derived state -----------------------------------------------------

    @property
    def is_cross_branch(self) -> bool:
        return self.route_type == TransferRouteType.CROSS_BRANCH

    @property
    def is_approved(self) -> bool:
        return self.status in (
            TransferRequestStatus.APPROVED,
            TransferRequestStatus.PARTIALLY_APPROVED,
        )

    @property
    def awaits_second_leg(self) -> bool:
        """Cross-branch stock sitting in the in-transit warehouse."""
        return (
            self.is_cross_branch
            and self.posting_status == TransferPostingStatus.IN_TRANSIT
        )

    @property
    def leg1_destination(self) -> str:
        """Where leg 1 actually ships to — in-transit when crossing branches."""
        if self.is_cross_branch:
            return self.intransit_warehouse
        return self.to_warehouse


# ---------------------------------------------------------------------------
# Request line
# ---------------------------------------------------------------------------

class WarehouseTransferRequestLine(models.Model):
    """One item on a request, carrying the requested/approved/moved triple."""

    request = models.ForeignKey(
        WarehouseTransferRequest, on_delete=models.CASCADE, related_name='lines',
    )
    line_num = models.IntegerField(
        help_text="Zero-based, mirroring the SAP document line number.",
    )

    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, default='')
    uom = models.CharField(max_length=20, blank=True, default='')

    # Per-line overrides. SAP genuinely uses these — 387 live documents ship
    # from more than one source warehouse — so they are stored, not assumed.
    from_warehouse = models.CharField(max_length=20, blank=True, default='')
    to_warehouse = models.CharField(max_length=20, blank=True, default='')

    requested_qty = models.DecimalField(max_digits=15, decimal_places=3)
    approved_qty = models.DecimalField(
        max_digits=15, decimal_places=3, default=0,
        help_text="Set by the receiving warehouse; may be less than requested.",
    )
    transferred_qty = models.DecimalField(
        max_digits=15, decimal_places=3, default=0,
        help_text="Actually moved by a posted transfer.",
    )

    is_batch_managed = models.BooleanField(
        default=False,
        help_text="From OITM.ManBtchNum — batch-managed lines must carry a split.",
    )
    batch_allocation = models.JSONField(
        default=list, blank=True,
        help_text="The BatchNumbers split sent to SAP, kept for reconciliation "
                  "against IBT1 (a Service Layer GET returns an empty list).",
    )

    status = models.CharField(
        max_length=15, choices=TransferLineStatus.choices,
        default=TransferLineStatus.PENDING,
    )
    notes = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['line_num']
        unique_together = [('request', 'line_num')]
        verbose_name = "Warehouse Transfer Request Line"

    def __str__(self):
        return f"{self.item_code} × {self.requested_qty}"

    @property
    def outstanding_qty(self):
        """Approved but not yet moved."""
        return max(self.approved_qty - self.transferred_qty, 0)

    @property
    def source_warehouse(self) -> str:
        return self.from_warehouse or self.request.from_warehouse

    @property
    def destination_warehouse(self) -> str:
        return self.to_warehouse or self.request.to_warehouse
