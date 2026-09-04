"""Local audit trail for SAP invoice approvals.

SAP records every approve/reject against the shared Service Layer account, so it
cannot show *which* factory employee acted. This model records that here — one row
per approve/reject — stamped with the real ``created_by`` (via ``BaseModel``) and
the company the action was taken under.

The two custom permissions the whole module gates on
(``invoice_approval.view_invoice`` / ``invoice_approval.approve_invoice``) also
live on this model's ``Meta``.
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

    # SAP approval-request code (OWDD.WddCode) — a plain int, not an FK (the row
    # lives in SAP), plus the draft it covered for cross-referencing in SAP B1.
    approval_code = models.IntegerField(db_index=True)
    draft_entry = models.IntegerField(null=True, blank=True)
    so_number = models.CharField(max_length=100, blank=True)
    party_name = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES)
    rejection_reason = models.TextField(blank=True, default="")
    sap_message = models.CharField(max_length=255, blank=True, default="")

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="invoice_approval_audits",
    )

    class Meta:
        db_table = "invoice_approval_audit"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "approval_code"])]
        # Suppress the auto add/change/delete/view perms; this module only needs the two below.
        default_permissions = ()
        permissions = [
            ("view_invoice", "Can view SAP invoices for approval"),
            ("approve_invoice", "Can approve or reject SAP invoices"),
        ]

    def __str__(self):
        return f"InvoiceApprovalAudit#{self.approval_code} {self.decision}"
