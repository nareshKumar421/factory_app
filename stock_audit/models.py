"""A physical stock audit of one SAP warehouse, against SAP's own figures.

An audit is started for one warehouse of the signed-in company (Oil, Mart or
Beverages each have their own SAP). Starting it copies SAP's on-hand for every
item the warehouse holds into :class:`StockAuditLine` — the stores are locked in
SAP while an audit runs, so that copy is the figure being audited, and it stays
readable if SAP goes down half way through the count.

Auditors then **add** what they find (:class:`StockAuditCount`): 10 caps on one
rack and 20 on another make 30. Every entry is kept, so a mistake is voided
rather than overwritten, and the line's counted figure is always the sum of its
live entries.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class AuditStatus(models.TextChoices):
    """Open -> Completed (awaiting approval) -> Approved.

    A rejected audit goes back to Open, with the reason, for the auditors to
    correct and complete again. Closed is only the older audits', from before
    audits were approved.
    """
    OPEN = 'OPEN', 'Open'
    SUBMITTED = 'SUBMITTED', 'Completed'
    APPROVED = 'APPROVED', 'Approved'
    CLOSED = 'CLOSED', 'Closed'


#: While an audit is in one of these, its warehouse is being audited.
ACTIVE_STATUSES = (AuditStatus.OPEN, AuditStatus.SUBMITTED)


class SapPosting(models.TextChoices):
    """How an approved audit's Inventory Posting to SAP went.

    Claimed (POSTING) before SAP is asked, so a second press cannot post it
    twice. UNKNOWN is a request that got no answer: SAP may have posted it, so
    it is only sent again once somebody has checked SAP and says it did not.
    """
    NONE = '', 'Not posted'
    POSTING = 'POSTING', 'Posting'
    DONE = 'DONE', 'Posted'
    FAILED = 'FAILED', 'SAP refused'
    UNKNOWN = 'UNKNOWN', 'No answer from SAP'


class ItemCategory(models.TextChoices):
    """By SAP item group, which is the same in all three companies."""
    RM = 'RM', 'Raw Material'
    PM = 'PM', 'Packing Material'
    FG = 'FG', 'Finished Goods'
    OTHER = 'OTHER', 'Other'


#: SAP OITM.ItmsGrpCod -> category (see warehouse.services.rm_stock_service).
ITEM_GROUP_CATEGORY = {106: ItemCategory.RM, 105: ItemCategory.PM, 102: ItemCategory.FG}


def category_for(item_group) -> str:
    try:
        return ITEM_GROUP_CATEGORY.get(int(item_group), ItemCategory.OTHER)
    except (TypeError, ValueError):
        return ItemCategory.OTHER


class StockAudit(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT, related_name='stock_audits')
    warehouse_code = models.CharField(max_length=20)
    warehouse_name = models.CharField(max_length=150, blank=True, default='')
    status = models.CharField(
        max_length=10, choices=AuditStatus.choices, default=AuditStatus.OPEN)
    notes = models.CharField(max_length=300, blank=True, default='')
    snapshot_at = models.DateTimeField(help_text="When SAP's figures were copied.")
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='stock_audits_started')
    started_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audits_closed')
    closed_at = models.DateTimeField(null=True, blank=True)

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audits_completed')
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audits_approved')
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_comment = models.CharField(max_length=300, blank=True, default='')
    # The last rejection, kept on the reopened audit so the auditors see why.
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audits_rejected')
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=300, blank=True, default='')

    # The Inventory Posting of its RM and PM differences, once approved.
    sap_posting = models.CharField(
        max_length=10, choices=SapPosting.choices, blank=True, default=SapPosting.NONE)
    sap_posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audits_posted')
    sap_posted_at = models.DateTimeField(null=True, blank=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.CharField(max_length=30, blank=True, default='')
    sap_posting_error = models.CharField(max_length=500, blank=True, default='')
    sap_posting_payload = models.JSONField(null=True, blank=True)
    # What was sent, line by line and batch by batch, as the page shows it.
    sap_posted_lines = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        constraints = [
            # Two audits of one warehouse in progress at once would split the
            # count between them, and neither would be the audit.
            models.UniqueConstraint(
                fields=['company', 'warehouse_code'],
                condition=models.Q(status__in=['OPEN', 'SUBMITTED']),
                name='uniq_active_stock_audit_per_warehouse',
            ),
        ]
        permissions = [
            ('can_view_stock_audit', 'Can view stock audits'),
            ('can_count_stock_audit', 'Can enter physical counts in a stock audit'),
            ('can_view_audit_sap_qty', "Can see SAP's quantity and the difference in a stock audit"),
            ('can_manage_stock_audit', 'Can start and refresh stock audits'),
            ('can_approve_stock_audit', 'Can approve or reject a completed stock audit'),
            ('can_post_stock_audit_to_sap',
             "Can post an approved stock audit's RM and PM differences to SAP"),
        ]

    def __str__(self):
        return f"{self.company.code} {self.warehouse_code} audit #{self.pk} ({self.status})"


class StockAuditLine(models.Model):
    """One item of the audited warehouse: SAP's figure, and what was counted."""
    audit = models.ForeignKey(StockAudit, on_delete=models.CASCADE, related_name='lines')
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, default='')
    item_group = models.IntegerField(null=True, blank=True)
    # SAP's name for the group (TRADING ITEMS, SEMI FINISHED GOODS...): the
    # tabs on screen. The codes differ between companies; the names do not.
    item_group_name = models.CharField(max_length=100, blank=True, default='')
    is_batch = models.BooleanField(
        default=False, help_text="SAP manages the item by batch (OITM.ManBtchNum).")
    category = models.CharField(
        max_length=10, choices=ItemCategory.choices, default=ItemCategory.OTHER)
    uom = models.CharField(max_length=20, blank=True, default='')
    sap_qty = models.DecimalField(max_digits=18, decimal_places=3, default=Decimal('0'))
    # The sum of the line's live counts; null until somebody has counted it,
    # which is not the same as having counted nothing.
    counted_qty = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    in_sap = models.BooleanField(
        default=True,
        help_text="False for an item found on the floor that SAP's copy did not list.")

    class Meta:
        ordering = ['category', 'item_code']
        unique_together = ('audit', 'item_code')
        indexes = [models.Index(fields=['audit', 'category'])]

    def __str__(self):
        return f"{self.item_code}: SAP {self.sap_qty}, counted {self.counted_qty}"

    @property
    def difference(self):
        """Counted less SAP; None while uncounted."""
        return None if self.counted_qty is None else self.counted_qty - self.sap_qty


class StockAuditCount(models.Model):
    """One find: '20 more caps on rack B'. Added to the line, never overwriting it."""
    line = models.ForeignKey(StockAuditLine, on_delete=models.CASCADE, related_name='counts')
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    note = models.CharField(max_length=200, blank=True, default='')
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='stock_audit_counts')
    counted_at = models.DateTimeField(auto_now_add=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stock_audit_counts_voided')
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['counted_at', 'id']

    def __str__(self):
        return f"{self.line.item_code} +{self.qty}"
