from django.db import models
from django.conf import settings
from driver_management.models import VehicleEntry
from raw_material_gatein.models import POReceipt, POItemReceipt


class GRPOStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "Failed"
    PARTIALLY_POSTED = "PARTIALLY_POSTED", "Partially Posted"


class SAPAttachmentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending Upload"
    UPLOADED = "UPLOADED", "Uploaded to SAP"
    LINKED = "LINKED", "Linked to GRPO Document"
    FAILED = "FAILED", "Upload Failed"


class GRPOPosting(models.Model):
    """
    Tracks GRPO postings to SAP for completed gate entries.
    Supports merging multiple POs from the same supplier into a single GRPO.
    """
    vehicle_entry = models.ForeignKey(
        VehicleEntry,
        on_delete=models.PROTECT,
        related_name="grpo_postings"
    )

    # Legacy single PO reference (nullable for merged GRPOs)
    po_receipt = models.ForeignKey(
        POReceipt,
        on_delete=models.PROTECT,
        related_name="grpo_postings",
        null=True,
        blank=True,
        help_text="Legacy single PO link. Use po_receipts M2M for merged GRPOs."
    )

    # M2M for merged GRPO: links multiple POs from the same supplier
    po_receipts = models.ManyToManyField(
        POReceipt,
        related_name="merged_grpo_postings",
        blank=True,
        help_text="All PO receipts included in this GRPO (same supplier)."
    )

    # SAP Response Fields
    sap_doc_entry = models.IntegerField(null=True, blank=True, help_text="SAP DocEntry")
    sap_doc_num = models.IntegerField(null=True, blank=True, help_text="SAP DocNum")
    sap_doc_total = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    status = models.CharField(
        max_length=20,
        choices=GRPOStatus.choices,
        default=GRPOStatus.PENDING
    )

    error_message = models.TextField(blank=True, null=True)

    # Audit fields
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="grpo_postings"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [
            ("can_view_pending_grpo", "Can view pending grpo entries"),
            ("can_preview_grpo", "Can preview grpo data"),
            ("can_view_grpo_history", "Can view GRPO posting history"),
        ]

    def __str__(self):
        po_numbers = ", ".join(
            self.po_receipts.values_list("po_number", flat=True)
        ) if self.pk and self.po_receipts.exists() else (
            self.po_receipt.po_number if self.po_receipt else "N/A"
        )
        return f"GRPO for {po_numbers} - {self.status}"


class GRPOLinePosting(models.Model):
    """
    Tracks individual line items posted in a GRPO.
    Links to POItemReceipt for traceability.
    """
    grpo_posting = models.ForeignKey(
        GRPOPosting,
        on_delete=models.CASCADE,
        related_name="lines"
    )

    po_item_receipt = models.ForeignKey(
        POItemReceipt,
        on_delete=models.PROTECT,
        related_name="grpo_lines"
    )

    # Quantities posted
    quantity_posted = models.DecimalField(max_digits=12, decimal_places=3)

    # SAP line details
    base_entry = models.IntegerField(null=True, blank=True, help_text="PO DocEntry in SAP")
    base_line = models.IntegerField(null=True, blank=True, help_text="PO Line Number in SAP")
    

    def __str__(self):
        return f"{self.po_item_receipt.item_name} - {self.quantity_posted}"


class GRPOAttachment(models.Model):
    """
    Stores files attached to a GRPO posting.
    Files are saved locally and then uploaded to SAP Attachments2 endpoint.
    """
    grpo_posting = models.ForeignKey(
        GRPOPosting,
        on_delete=models.CASCADE,
        related_name="attachments"
    )

    file = models.FileField(upload_to="grpo_attachments/")
    original_filename = models.CharField(
        max_length=255,
        help_text="Original uploaded filename"
    )

    # SAP sync tracking
    sap_attachment_status = models.CharField(
        max_length=20,
        choices=SAPAttachmentStatus.choices,
        default=SAPAttachmentStatus.PENDING
    )
    sap_absolute_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Attachments2 AbsoluteEntry after upload"
    )
    sap_error_message = models.TextField(blank=True, null=True)

    # Audit fields
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="grpo_attachments"
    )

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"Attachment for GRPO {self.grpo_posting_id} - {self.original_filename}"
