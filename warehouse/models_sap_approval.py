"""Local audit trail for SAP transfer-approval decisions taken in this app.

SAP records the decision against the *authorizer* whose credentials signed it
(``USER37``, ``USER24``, …) — it has no idea which factory employee actually
clicked Approve, and several employees may share one authorizer's mandate. This
model records the pairing: one row per decision, with the real ``created_by``
(via ``BaseModel``) alongside the SAP user it was signed as.

It is deliberately separate from ``invoice_approval.InvoiceApprovalAudit``:
that table is invoice-shaped (SO number, party, amount) and its
``approval_code`` id-space is filtered by an OMS/SAP ``source`` flag, whereas a
transfer approval is identified by warehouse route and object type.
"""
from django.db import models

from gate_core.models import BaseModel


class SapApprovalAudit(BaseModel):
    """One row per SAP transfer-approval decision made through the app."""

    DECISION_APPROVED = "APPROVED"
    DECISION_REJECTED = "REJECTED"
    DECISION_CHOICES = [
        (DECISION_APPROVED, "Approved"),
        (DECISION_REJECTED, "Rejected"),
    ]

    # SAP approval-request code (OWDD.WddCode) and the draft it covered — plain
    # ints, not FKs; the documents live in SAP.
    approval_code = models.IntegerField(db_index=True)
    draft_entry = models.IntegerField(null=True, blank=True)
    # '67' inventory transfer or '1250000001' transfer request.
    obj_type = models.CharField(max_length=20, blank=True, default="")
    doc_num = models.BigIntegerField(null=True, blank=True)
    from_warehouse = models.CharField(max_length=20, blank=True, default="")
    to_warehouse = models.CharField(max_length=20, blank=True, default="")

    # The SAP authorizer the decision was signed as, and the stage it settled.
    sap_approver = models.CharField(max_length=50, blank=True, default="")
    stage_code = models.IntegerField(null=True, blank=True)

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES)
    rejection_reason = models.TextField(blank=True, default="")
    sap_message = models.CharField(max_length=255, blank=True, default="")

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sap_approval_audits",
    )

    class Meta:
        db_table = "warehouse_sap_approval_audit"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "approval_code"])]
        default_permissions = ()

    def __str__(self):
        return f"SapApprovalAudit#{self.approval_code} {self.decision}"
