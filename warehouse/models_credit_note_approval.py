"""Local audit trail for SAP credit-note approval decisions taken in this app.

SAP records the decision against the *authorizer* whose credentials signed it
(``USER37``, ``USER24``, …) — it has no idea which factory employee actually
clicked Approve, and several employees may share one authorizer's mandate. This
model records the pairing: one row per decision, with the real ``created_by``
(via ``BaseModel``) alongside the SAP user it was signed as.

Deliberately its own table rather than a reuse of
:class:`warehouse.models_sap_approval.SapApprovalAudit`: that one is
transfer-shaped (a route between two warehouses), while a credit note is party-
and money-shaped, and a service credit note has no warehouse at all. The two
answer different questions and the columns that answer them do not overlap.

It also carries this app's two credit-note permissions, for the same reason the
transfer permissions hang off the transfer model — a "Credit Note Approver"
group needs granting without handing over the rest of the warehouse module.
"""
from django.db import models

from gate_core.models import BaseModel


class CreditNoteApprovalAudit(BaseModel):
    """One row per SAP credit-note approval decision made through the app."""

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
    # '14' A/R credit note or '19' A/P credit note.
    obj_type = models.CharField(max_length=20, blank=True, default="")
    # The DRAFT's number, which is provisional — open drafts share it and the
    # posted document frequently keeps a different one. Recorded for tracing
    # back to the row the decision was taken on, not as a document reference.
    doc_num = models.BigIntegerField(null=True, blank=True)
    card_code = models.CharField(max_length=50, blank=True, default="")
    party_name = models.CharField(max_length=200, blank=True, default="")
    total_amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    # The SAP authorizer the decision was signed as, and the stage it settled.
    sap_approver = models.CharField(max_length=50, blank=True, default="")
    stage_code = models.IntegerField(null=True, blank=True)

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES)
    rejection_reason = models.TextField(blank=True, default="")
    sap_message = models.CharField(max_length=255, blank=True, default="")

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="credit_note_approval_audits",
    )

    class Meta:
        db_table = "warehouse_credit_note_approval_audit"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "approval_code"])]
        default_permissions = ()
        permissions = [
            ("can_view_credit_note_approval", "Can view the SAP credit-note approval queue"),
            ("can_approve_credit_note", "Can approve or reject a SAP credit note"),
        ]

    def __str__(self):
        return f"CreditNoteApprovalAudit#{self.approval_code} {self.decision}"
