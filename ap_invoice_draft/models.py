"""A/P invoice drafts -- a vendor's bill put into SAP against its GRPO.

The store receives the goods and posts the GRPO; accounts then turns that GRPO
into an A/P invoice. Until now that second step was done by hand in SAP: copy
the GRPO to an A/P invoice, attach the scanned bill, save. This module does the
copy from the warehouse side.

What SAP gets is a **draft** (``ODRF``, object 18), never a posted invoice: an
A/P invoice is a payable, and the person who adds it in SAP is still the one who
answers for it. A draft does not enter SAP's approval procedures either -- those
run when accounts adds it.

One entry per GRPO. The GRPO is referenced by its doc-entry and re-read from
SAP when the draft is made; only what SAP cannot tell us later is stored: the
bill as uploaded. The GRPO header is snapshotted so the list needs no SAP
round-trip.
"""

from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class SapDraftStatus(models.TextChoices):
    PENDING = "PENDING", "Not in SAP yet"
    CREATED = "CREATED", "Draft in SAP"
    # SAP refused it, or did not answer. A retry reads SAP back first, so a
    # draft SAP made without telling us is found rather than made twice.
    FAILED = "FAILED", "SAP did not take it"


class APInvoiceDraft(BaseModel):
    """One vendor bill and the A/P invoice draft SAP holds for it."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="ap_invoice_drafts",
    )
    entry_no = models.CharField(max_length=50, unique=True)

    # The GRPO the bill is for, as SAP had it when the entry was made.
    grpo_doc_entry = models.IntegerField()
    grpo_doc_num = models.CharField(max_length=50, blank=True)
    grpo_date = models.DateField(null=True, blank=True)
    # OPDN.NumAtCard: the bill number the store keyed in when it made the GRPO.
    grpo_reference = models.CharField(max_length=100, blank=True)
    vendor_code = models.CharField(max_length=50, blank=True)
    vendor_name = models.CharField(max_length=255, blank=True)
    grpo_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    # The bill as the user uploaded it.
    invoice_file = models.FileField(upload_to="ap_invoice_drafts/")
    invoice_filename = models.CharField(max_length=255, blank=True)

    sap_status = models.CharField(
        max_length=20,
        choices=SapDraftStatus.choices,
        default=SapDraftStatus.PENDING,
        db_index=True,
    )
    sap_draft_entry = models.IntegerField(null=True, blank=True)
    # True when SAP already held an open A/P draft for this GRPO (made by hand
    # in SAP) and the entry was linked to it instead of making a second one.
    sap_draft_adopted = models.BooleanField(default=False)
    sap_error = models.TextField(blank=True)
    sap_attachment_entry = models.IntegerField(null=True, blank=True)
    sap_attachment_error = models.TextField(blank=True)
    sap_created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["company", "-created_at"]),
            models.Index(fields=["grpo_doc_entry"]),
        ]
        constraints = [
            # One bill per GRPO. A withdrawn entry (is_active=False) frees it.
            models.UniqueConstraint(
                fields=["company", "grpo_doc_entry"],
                condition=models.Q(is_active=True),
                name="ap_invoice_draft_one_per_grpo",
            ),
        ]
        permissions = [
            ("can_view_ap_invoice_draft", "Can view A/P invoice drafts"),
            ("can_create_ap_invoice_draft", "Can create an A/P invoice draft in SAP"),
        ]

    def __str__(self):
        return f"{self.entry_no} - GRPO {self.grpo_doc_num}"

    @staticmethod
    def _next_number(prefix: str) -> int:
        last = (
            APInvoiceDraft.objects.filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )
        if not last:
            return 1
        try:
            return int(last.entry_no.split("-")[-1]) + 1
        except ValueError:
            return 1

    @classmethod
    def generate_entry_no(cls) -> str:
        prefix = f"APD-{timezone.now().strftime('%Y%m%d')}"
        return f"{prefix}-{cls._next_number(prefix):04d}"
