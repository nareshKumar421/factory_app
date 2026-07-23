"""Local audit trail for OMS invoice approvals.

OMS attributes every approve/reject to a single shared "Factory" service account,
so it cannot show *which* JI employee acted. This model records that here — one row
per approve/reject — stamped with the real ``created_by`` (via ``BaseModel``) and the
company the action was taken under.

The two custom permissions the whole module gates on (``oms.view_invoice`` /
``oms.approve_invoice``) also live on this model's ``Meta``.
"""
from django.db import models

from gate_core.models import BaseModel


class InvoiceApprovalAudit(BaseModel):
    """One row per approve/reject action taken through the factory approver page."""

    DECISION_APPROVED = "APPROVED"
    DECISION_REJECTED = "REJECTED"
    DECISION_CHOICES = [
        (DECISION_APPROVED, "Approved"),
        (DECISION_REJECTED, "Rejected"),
    ]

    # OMS invoice-log id — a plain int, not an FK (the row lives in another system).
    invoice_log_id = models.IntegerField(db_index=True)
    so_number = models.CharField(max_length=100, blank=True)
    party_name = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES)
    rejection_reason = models.TextField(blank=True, default="")
    oms_message = models.CharField(max_length=255, blank=True, default="")

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="oms_invoice_audits",
    )

    class Meta:
        db_table = "oms_invoice_approval_audit"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "invoice_log_id"])]
        # Suppress the auto add/change/delete/view perms; this module only needs the two below.
        default_permissions = ()
        permissions = [
            ("view_invoice", "Can view OMS invoices for approval"),
            ("approve_invoice", "Can approve or reject OMS invoices"),
        ]

    def __str__(self):
        return f"InvoiceApprovalAudit#{self.invoice_log_id} {self.decision}"
