"""Branch Stock Transfer (BST) — warehouse-driven, scan-based, two-sided transfer.

A warehouse user (sender) creates a BST against a SAP stock-transfer document and
scans the boxes/pallets being moved, then dispatches it. BST is **intra-company**:
a SAP stock transfer moves stock between two warehouses of the same company, so
the source and destination warehouses both come from the SAP document. (True
cross-company moves use the separate intercompany-transfer flow.) When the
destination warehouse sits outside the factory the Gate marks the vehicle out and
in. The destination warehouse user (receiver) then scans the arriving boxes and
resolves each as accepted or rejected. Accepted boxes change `current_warehouse`
to the SAP to-warehouse in our DB (no company change, no SAP posting yet).

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


class BSTTransfer(models.Model):
    """Head record for one branch-stock-transfer shipment (cross-company)."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="bst_transfers",
        help_text="The company the transfer belongs to (BST is intra-company).",
    )
    entry_no = models.CharField(max_length=50, unique=True)

    # SAP stock-transfer document the user selected.
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


class BSTTransferItem(models.Model):
    """Expected line, snapshot from the SAP stock-transfer document."""

    transfer = models.ForeignKey(
        BSTTransfer, on_delete=models.CASCADE, related_name="items",
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
            models.UniqueConstraint(
                fields=["transfer", "line_num"],
                name="unique_bst_transfer_line",
            ),
        ]

    def __str__(self):
        return f"{self.transfer.entry_no} - {self.item_code}"


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
