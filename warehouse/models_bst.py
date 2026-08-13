"""Branch Stock Transfer (BST) — warehouse-driven, scan-based, two-sided transfer.

A warehouse user (sender) creates a BST against a SAP document and scans the
boxes/pallets being moved, then dispatches it. When the destination sits outside
the factory the Gate marks the vehicle out and in. The destination user
(receiver) then scans the arriving boxes and resolves each as accepted or
rejected.

A BST has one of two `source_type`s, which decides how the stock settles:

* STOCK_TRANSFER (default) — **intra-company**: a SAP stock transfer moves stock
  between two warehouses of the same company, so the source and destination
  warehouses both come from the SAP document. Accepted boxes change
  `current_warehouse` to the SAP to-warehouse (no company change).

* INVOICE — **cross-company** (e.g. JIVO OIL → JIVO MART): a SAP AR invoice sells
  stock to another company. The source warehouse comes from the invoice lines and
  the destination is that company (`destination_company`). Accepted boxes are
  reassigned to the destination company on receipt, reusing the shared
  `barcode.services.box_ownership` handoff (with the JIVO MART item-code remap
  where the catalogues differ) — the same ownership move the standalone
  intercompany-transfer flow uses. No SAP posting yet.

Lives in the `warehouse` app alongside BOM Requests / FG Receipts.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class BSTTransferStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SCANNING = "SCANNING", "Scanning"
    DISPATCHED = "DISPATCHED", "Dispatched"
    AWAITING_GATE_OUT = "AWAITING_GATE_OUT", "Awaiting Gate Out"
    GATED_OUT = "GATED_OUT", "Gated Out"
    IN_TRANSIT = "IN_TRANSIT", "In Transit"
    AWAITING_GATE_IN = "AWAITING_GATE_IN", "Awaiting Gate In"
    GATED_IN = "GATED_IN", "Gated In"
    ARRIVED = "ARRIVED", "Arrived"
    RECEIVING = "RECEIVING", "Receiving"
    RECEIVED = "RECEIVED", "Received"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially Received"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class BSTReceiveStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"


class BSTSourceType(models.TextChoices):
    # The SAP document a BST entry is sourced from — this also decides how the
    # stock settles on receipt (see BSTTransfer.source_type).
    STOCK_TRANSFER = "STOCK_TRANSFER", "Stock Transfer"
    INVOICE = "INVOICE", "Invoice"


class BSTTransfer(models.Model):
    """Head record for one branch-stock-transfer shipment (cross-company)."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="bst_transfers",
        help_text="The company the transfer belongs to (BST is intra-company).",
    )
    entry_no = models.CharField(max_length=50, unique=True)

    # A BST entry can combine several SAP stock-transfer documents that share the
    # same source and destination warehouse (one physical shipment on one
    # vehicle). Each document is a row in `docs`; the fields below mirror the
    # first/primary document for backward compatibility and quick display, while
    # sap_from_warehouse/sap_to_warehouse are the entry's shared route.
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.CharField(max_length=50, blank=True, default="")
    sap_doc_date = models.DateField(null=True, blank=True)
    sap_from_warehouse = models.CharField(max_length=50, blank=True, default="")
    sap_to_warehouse = models.CharField(max_length=50, blank=True, default="")
    sap_reference = models.CharField(max_length=100, blank=True, default="")
    invoice_no = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Invoice / document number the warehouse user typed to look up the BST.",
    )

    # What SAP document this entry is sourced from, and therefore how it settles:
    #   STOCK_TRANSFER — intra-company move; on receipt boxes only change
    #     `current_warehouse` (company never changes).
    #   INVOICE — cross-company sale (e.g. JIVO OIL → JIVO MART); on receipt the
    #     accepted boxes are reassigned to `destination_company` (with the
    #     JIVO MART item-code remap where applicable).
    source_type = models.CharField(
        max_length=20,
        choices=BSTSourceType.choices,
        default=BSTSourceType.STOCK_TRANSFER,
    )
    # For an INVOICE transfer the destination is a different company (the SAP
    # invoice's customer). Null for STOCK_TRANSFER (intra-company) transfers.
    destination_company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="bst_transfers_incoming",
        null=True,
        blank=True,
        help_text="Receiving company for an INVOICE (cross-company) transfer.",
    )
    # Customer snapshot from the SAP invoice (INVOICE transfers only), for display.
    customer_code = models.CharField(max_length=100, blank=True, default="")
    customer_name = models.CharField(max_length=255, blank=True, default="")

    # Vehicle + driver are only relevant when the transfer leaves the factory
    # (requires_gate). For an internal move the stock is already at the dock, so
    # these stay null.
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="bst_transfers",
        null=True,
        blank=True,
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        related_name="bst_transfers",
        null=True,
        blank=True,
    )

    requires_gate = models.BooleanField(
        default=False,
        help_text="True when source/destination is outside the factory and the Gate must mark the vehicle out/in.",
    )

    status = models.CharField(
        max_length=20,
        choices=BSTTransferStatus.choices,
        default=BSTTransferStatus.DRAFT,
    )
    remarks = models.TextField(blank=True, default="")

    # Lifecycle audit.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_created",
    )
    # Warehouse review: the scanning was checked and approved as correct.
    scan_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_scan_approved",
    )
    scan_approved_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_dispatched",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)

    # Gate stamps (only when requires_gate).
    gated_out_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_gated_out",
    )
    gated_out_at = models.DateTimeField(null=True, blank=True)
    gated_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_gated_in",
    )
    gated_in_at = models.DateTimeField(null=True, blank=True)

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_received_by",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    cancel_reason = models.TextField(blank=True, default="")
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_transfers_cancelled",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "BST Transfer"
        verbose_name_plural = "BST Transfers"
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["sap_doc_num"]),
        ]
        permissions = [
            ("can_create_bst", "Can create a branch stock transfer"),
            ("can_scan_bst", "Can scan boxes onto a branch stock transfer"),
            ("can_dispatch_bst", "Can dispatch a branch stock transfer"),
            ("can_receive_bst", "Can receive a branch stock transfer"),
            ("can_gate_bst", "Can mark a branch stock transfer out/in at the gate"),
        ]

    def __str__(self):
        return self.entry_no

    @staticmethod
    def generate_entry_no():
        prefix = f"BST-{timezone.now().strftime('%Y%m%d')}"
        last = (
            BSTTransfer.objects
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


class BSTTransferDoc(models.Model):
    """One SAP stock-transfer document included in a BST entry.

    A BST entry may combine several SAP documents; all of them share the entry's
    source and destination warehouse (enforced on create). Each document keeps
    its own SAP identity here, and its expected lines are the `BSTTransferItem`
    rows linked to it.
    """

    transfer = models.ForeignKey(
        BSTTransfer, on_delete=models.CASCADE, related_name="docs",
    )
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.CharField(max_length=50, blank=True, default="")
    sap_doc_date = models.DateField(null=True, blank=True)
    sap_reference = models.CharField(max_length=100, blank=True, default="")
    invoice_no = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Invoice / document number the warehouse user typed to look up this document.",
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "BST Transfer Document"
        verbose_name_plural = "BST Transfer Documents"
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "sap_doc_entry"],
                name="unique_bst_transfer_doc",
            ),
        ]

    def __str__(self):
        return f"{self.transfer.entry_no} - {self.sap_doc_num or self.sap_doc_entry}"


class BSTTransferItem(models.Model):
    """Expected line, snapshot from a SAP stock-transfer document."""

    transfer = models.ForeignKey(
        BSTTransfer, on_delete=models.CASCADE, related_name="items",
    )
    doc = models.ForeignKey(
        BSTTransferDoc, on_delete=models.CASCADE, related_name="items",
        null=True, blank=True,
        help_text="The SAP document this line came from.",
    )
    line_num = models.IntegerField()
    item_code = models.CharField(max_length=100, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=50, blank=True, default="")
    from_warehouse = models.CharField(max_length=50, blank=True, default="")
    to_warehouse = models.CharField(max_length=50, blank=True, default="")
    expected_boxes = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["line_num"]
        constraints = [
            # line_num is unique within a document (it repeats across documents).
            models.UniqueConstraint(
                fields=["transfer", "doc", "line_num"],
                name="unique_bst_transfer_line",
            ),
        ]

    def __str__(self):
        return f"{self.transfer.entry_no} - {self.item_code}"


class BSTManualItemEntry(models.Model):
    """Hand-typed quantity for a scan-exempt (PM) line on a BST.

    Packaging material isn't barcode-tracked, so a PM line has no box scans to
    record what physically moved — the sender types it here instead. Keyed by
    **item code**, the same grain the bill table and ``compute_scan_status``
    aggregate on (a BST entry may repeat an item code across its SAP documents),
    so one row is the transfer's entered quantity for that material.

    Recording only: a PM line still never gates sealing (see
    ``bst_service.compute_scan_status``), so a missing entry can't block a
    dispatch that used to go through.
    """

    transfer = models.ForeignKey(
        BSTTransfer, on_delete=models.CASCADE, related_name="manual_entries",
    )
    item_code = models.CharField(max_length=100)
    item_name = models.CharField(max_length=255, blank=True, default="")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=50, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_manual_entries",
    )
    entered_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item_code"]
        verbose_name = "BST Manual Item Entry"
        verbose_name_plural = "BST Manual Item Entries"
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "item_code"],
                name="unique_bst_manual_item_entry",
            ),
        ]
        indexes = [
            models.Index(fields=["transfer"]),
        ]

    def __str__(self):
        return f"{self.transfer.entry_no} - {self.item_code} ({self.quantity})"


class BSTPartialTransferStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class BSTPartialTransferApproval(models.Model):
    """Admin approval to *seal* a BST whose scanned quantity is short of the bill.

    The sender's completeness gate (``bst_service.compute_scan_status`` +
    ``approve()``) blocks sealing a short load. An operator raises this request from
    the review page; an admin approves it, which releases the shortfall so the
    sender can finish sending. Mirrors ``docking_admin.DockingPartialScanRequest``
    (the sales-dispatch equivalent) and shares its PENDING/APPROVED/REJECTED
    lifecycle. ``partial_transfer_requests`` is the reverse relation
    ``bst_service.partial_transfer_approved`` reads.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="bst_partial_transfer_requests",
    )
    transfer = models.ForeignKey(
        BSTTransfer,
        on_delete=models.CASCADE,
        related_name="partial_transfer_requests",
    )

    scanned_qty = models.DecimalField(
        max_digits=18, decimal_places=3, default=0,
        help_text="Total scanned quantity when the request was raised.",
    )
    expected_qty = models.DecimalField(
        max_digits=18, decimal_places=3, default=0,
        help_text="Expected (bill) quantity when the request was raised.",
    )
    reason = models.TextField(help_text="Why the transfer is sealed with a partial scan.")
    status = models.CharField(
        max_length=20,
        choices=BSTPartialTransferStatus.choices,
        default=BSTPartialTransferStatus.PENDING,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="bst_partial_transfer_requests",
    )
    requested_at = models.DateTimeField(default=timezone.now)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="bst_partial_transfer_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-requested_at", "-id"]
        verbose_name = "BST Partial Transfer Approval"
        verbose_name_plural = "BST Partial Transfer Approvals"
        constraints = [
            models.UniqueConstraint(
                fields=["transfer"],
                condition=models.Q(status="PENDING"),
                name="unique_pending_bst_partial_transfer",
            ),
        ]
        permissions = [
            ("can_request_bst_partial_transfer", "Can request to seal a BST with a partial scan"),
            ("can_view_bst_partial_transfer", "Can view BST partial transfer requests"),
            ("can_approve_bst_partial_transfer", "Can approve or reject BST partial transfer requests"),
        ]

    def __str__(self):
        return f"BSTPartialTransfer #{self.id} - {self.transfer_id} ({self.status})"

    @property
    def is_pending(self):
        return self.status == BSTPartialTransferStatus.PENDING

    @property
    def is_approved(self):
        return self.status == BSTPartialTransferStatus.APPROVED

    def mark_reviewed(self, *, status, reviewer, notes=""):
        self.status = status
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.review_notes = notes or ""
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"])


class BSTBoxScan(models.Model):
    """One physical box on a transfer. Holds both the send and the receive state.

    A row is created when the sender scans a box. The receiver later stamps the
    receive fields on the same row (accept/reject). Dispatched boxes never
    receive-scanned by close are treated as short/missing (derived, no row).
    """

    transfer = models.ForeignKey(
        BSTTransfer, on_delete=models.CASCADE, related_name="box_scans",
    )
    box = models.ForeignKey(
        "barcode.Box", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_box_scans",
    )
    pallet = models.ForeignKey(
        "barcode.Pallet", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_box_scans",
    )
    box_barcode = models.CharField(max_length=50)

    # Denormalized from the box at scan time.
    item_code = models.CharField(max_length=100, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    batch_number = models.CharField(max_length=100, blank=True, default="")
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=50, blank=True, default="")
    warehouse_code = models.CharField(max_length=50, blank=True, default="")
    pallet_code = models.CharField(max_length=50, blank=True, default="")

    # Send side.
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_box_scans",
    )
    scanned_at = models.DateTimeField(auto_now_add=True)

    # Receive side.
    receive_status = models.CharField(
        max_length=20,
        choices=BSTReceiveStatus.choices,
        default=BSTReceiveStatus.PENDING,
    )
    reject_reason = models.TextField(blank=True, default="")
    is_unexpected = models.BooleanField(
        default=False,
        help_text="Receiver scanned this box but the sender never dispatched it.",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="bst_box_scans_received",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["scanned_at"]
        verbose_name = "BST Box Scan"
        verbose_name_plural = "BST Box Scans"
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "box_barcode"],
                name="unique_bst_transfer_box",
            ),
        ]
        indexes = [
            models.Index(fields=["transfer"]),
            models.Index(fields=["transfer", "receive_status"]),
            models.Index(fields=["box_barcode"]),
        ]

    def __str__(self):
        return f"{self.transfer.entry_no} - {self.box_barcode}"
