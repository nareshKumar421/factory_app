"""Local audit trail for inventory-transfer drafts added to SAP from this app.

Adding a draft is the moment the stock actually moves, and SAP records it
against the Service Layer account the app logs in as — not the employee who
pressed the button. The same gap ``SapApprovalAudit`` fills for approval
decisions, this fills for the add.

Failures are recorded too, on purpose. The add runs
``SBO_SP_TransactionNotification``, which never ran at draft time, so a draft
that saved cleanly months ago can be refused today for a reason nobody sees on
screen twice — a row per attempt is what makes "it just won't post" answerable.
"""
from django.db import models

from gate_core.models import BaseModel


class SapTransferDraftPost(BaseModel):
    """One row per attempt to add an inventory-transfer draft in SAP."""

    RESULT_POSTED = "POSTED"
    RESULT_FAILED = "FAILED"
    RESULT_CHOICES = [
        (RESULT_POSTED, "Posted"),
        (RESULT_FAILED, "Failed"),
    ]

    # SAP ids — plain ints, not FKs: the documents live in SAP.
    draft_entry = models.IntegerField(db_index=True)
    draft_doc_num = models.BigIntegerField(null=True, blank=True)
    from_warehouse = models.CharField(max_length=20, blank=True, default="")
    to_warehouse = models.CharField(max_length=20, blank=True, default="")
    line_count = models.IntegerField(default=0)

    # The OWTR the draft became. Null on a failure, and also on the rare
    # success SAP confirmed only by the read-back after a timeout.
    doc_entry = models.IntegerField(null=True, blank=True)
    doc_num = models.BigIntegerField(null=True, blank=True)

    result = models.CharField(max_length=10, choices=RESULT_CHOICES)
    error_message = models.TextField(blank=True, default="")

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="sap_transfer_draft_posts",
    )

    class Meta:
        db_table = "warehouse_sap_transfer_draft_post"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "draft_entry"])]
        default_permissions = ()

    def __str__(self):
        return f"SapTransferDraftPost#{self.draft_entry} {self.result}"
