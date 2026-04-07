from django.db import models
from django.conf import settings


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class BOMRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially Approved"
    REJECTED = "REJECTED", "Rejected"


class BOMLineStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class FGReceiptStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RECEIVED = "RECEIVED", "Received"
    SAP_POSTED = "SAP_POSTED", "SAP Posted"
    FAILED = "FAILED", "Failed"


class MaterialIssueStatus(models.TextChoices):
    NOT_ISSUED = "NOT_ISSUED", "Not Issued"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED", "Partially Issued"
    FULLY_ISSUED = "FULLY_ISSUED", "Fully Issued"


# ---------------------------------------------------------------------------
# BOM Request — Production submits to Warehouse for approval
# ---------------------------------------------------------------------------

class BOMRequest(models.Model):
    """
    A request from the production team to the warehouse team
    for raw materials needed for a production run.
    """
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='bom_requests'
    )
    production_run = models.ForeignKey(
        'production_execution.ProductionRun', on_delete=models.CASCADE,
        related_name='bom_requests'
    )
    sap_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Production Order DocEntry"
    )
    required_qty = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Required production quantity (units to produce)"
    )
    status = models.CharField(
        max_length=25, choices=BOMRequestStatus.choices,
        default=BOMRequestStatus.PENDING
    )
    remarks = models.TextField(blank=True, default='')
    rejection_reason = models.TextField(
        blank=True, default='',
        help_text="Reason for rejection (filled by warehouse)"
    )

    # Material Issue tracking (after approval)
    material_issue_status = models.CharField(
        max_length=25, choices=MaterialIssueStatus.choices,
        default=MaterialIssueStatus.NOT_ISSUED
    )
    sap_issue_doc_entries = models.JSONField(
        default=list, blank=True,
        help_text="List of SAP Goods Issue DocEntries"
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='bom_requests_created'
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='bom_requests_reviewed'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'BOM Request'
        verbose_name_plural = 'BOM Requests'

    def __str__(self):
        return f"BOM Request #{self.id} — Run #{self.production_run_id}"


class BOMRequestLine(models.Model):
    """
    Individual material line in a BOM request.
    Quantities are scaled based on required_qty.
    """
    bom_request = models.ForeignKey(
        BOMRequest, on_delete=models.CASCADE,
        related_name='lines'
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255)
    per_unit_qty = models.DecimalField(
        max_digits=12, decimal_places=4, default=0,
        help_text="BOM quantity per single unit of finished good"
    )
    required_qty = models.DecimalField(
        max_digits=12, decimal_places=3,
        help_text="Scaled quantity = per_unit_qty * production required_qty"
    )
    available_stock = models.DecimalField(
        max_digits=12, decimal_places=3, default=0,
        help_text="Warehouse stock at the time of review"
    )
    approved_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0,
        help_text="Quantity approved by warehouse"
    )
    issued_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0,
        help_text="Quantity actually issued to production"
    )
    warehouse = models.CharField(max_length=20, blank=True, default='')
    uom = models.CharField(max_length=20, blank=True, default='')
    base_line = models.IntegerField(
        default=0,
        help_text="WOR1 LineNum (0-based) for SAP issue linking"
    )
    status = models.CharField(
        max_length=20, choices=BOMLineStatus.choices,
        default=BOMLineStatus.PENDING
    )
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['base_line']
        verbose_name = 'BOM Request Line'
        verbose_name_plural = 'BOM Request Lines'

    def __str__(self):
        return f"{self.item_code} — {self.required_qty} {self.uom}"


# ---------------------------------------------------------------------------
# Finished Goods Receipt — Warehouse receives finished goods post-production
# ---------------------------------------------------------------------------

class FinishedGoodsReceipt(models.Model):
    """
    After production is completed, warehouse receives the finished goods
    and posts the receipt to SAP.
    """
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='fg_receipts'
    )
    production_run = models.ForeignKey(
        'production_execution.ProductionRun', on_delete=models.CASCADE,
        related_name='fg_receipts'
    )
    sap_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Production Order DocEntry"
    )
    item_code = models.CharField(max_length=50, blank=True, default='')
    item_name = models.CharField(max_length=255, blank=True, default='')
    produced_qty = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Total quantity produced"
    )
    good_qty = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Good quantity (produced - rejected)"
    )
    rejected_qty = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )
    warehouse = models.CharField(max_length=20, blank=True, default='')
    uom = models.CharField(max_length=20, blank=True, default='')
    posting_date = models.DateField()

    status = models.CharField(
        max_length=20, choices=FGReceiptStatus.choices,
        default=FGReceiptStatus.PENDING
    )
    sap_receipt_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Goods Receipt DocEntry after posting"
    )
    sap_error = models.TextField(blank=True, default='')

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='fg_receipts_received'
    )
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Finished Goods Receipt'
        verbose_name_plural = 'Finished Goods Receipts'

    def __str__(self):
        return f"FG Receipt #{self.id} — {self.item_code} x {self.good_qty}"
