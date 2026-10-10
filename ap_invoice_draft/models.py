"""A/P invoice drafts -- a vendor's bill put into SAP against its GRPO, and audited.

The store receives the goods and posts the GRPO; accounts then turns that GRPO
into an A/P invoice. Until now that second step was done by hand in SAP: copy
the GRPO to an A/P invoice, attach the scanned bill, save. This module does the
copy from the warehouse side and, for every bill, runs the audit checklist that
says whether it is fit to be paid (``checks.py`` holds the rules).

What SAP gets is a **draft** (``ODRF``, object 18), never a posted invoice: an
A/P invoice is a payable, and the person who adds it in SAP is still the one who
answers for it. A draft does not enter SAP's approval procedures either -- those
run when accounts adds it.

One entry per GRPO. The GRPO is referenced by its doc-entry and re-read from
SAP whenever the checks run; only what SAP cannot tell us later is stored: the
bill as uploaded, what was read off it, and what a person decided about each
check. The GRPO header is snapshotted so the list needs no SAP round-trip.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel


class SapDraftStatus(models.TextChoices):
    PENDING = "PENDING", "Not in SAP yet"
    CREATED = "CREATED", "Draft in SAP"
    # SAP refused it, or did not answer. A retry reads SAP back first, so a
    # draft SAP made without telling us is found rather than made twice.
    FAILED = "FAILED", "SAP did not take it"


class InvoiceReadStatus(models.TextChoices):
    PENDING = "PENDING", "Not read yet"
    READING = "READING", "Reading"
    READ = "READ", "Read"
    FAILED = "FAILED", "Could not read"


class APInvoiceDraft(BaseModel):
    """One vendor bill, the A/P invoice draft SAP holds for it, and its audit."""

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
    # The app's own record of that GRPO, when it was posted from here: the way
    # to the truck's arrival and its QC.
    grpo_posting = models.ForeignKey(
        "grpo.GRPOPosting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ap_invoice_drafts",
    )

    # The bill as the user uploaded it.
    invoice_file = models.FileField(upload_to="ap_invoice_drafts/")
    invoice_filename = models.CharField(max_length=255, blank=True)

    # What OCR read off the bill (``invoice_reader.py``): the printed text with
    # where it sits, and the Rate Check line's ink. Kept whole so the checks can
    # re-run without reading the bill again.
    invoice_read_status = models.CharField(
        max_length=20,
        choices=InvoiceReadStatus.choices,
        default=InvoiceReadStatus.PENDING,
    )
    invoice_data = models.JSONField(default=dict, blank=True)
    invoice_read_error = models.TextField(blank=True)
    invoice_read_model = models.CharField(max_length=100, blank=True)
    invoice_read_at = models.DateTimeField(null=True, blank=True)

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

    # The TDS the app put on the draft it made (``tds.py``): the withholding
    # code (blank for none), the base, what SAP worked out, and why. Left blank
    # on a draft made in SAP, which carries whatever accounts put on it.
    tds_code = models.CharField(max_length=20, blank=True)
    tds_taxable = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    tds_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    tds_note = models.TextField(blank=True)

    checks_run_at = models.DateTimeField(null=True, blank=True)

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
            ("can_review_ap_invoice_draft", "Can mark A/P invoice draft checks OK or not OK"),
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


class CheckStatus(models.TextChoices):
    PASS = "PASS", "OK"
    FAIL = "FAIL", "Not OK"
    # The app found something it cannot judge alone; a person has to look.
    REVIEW = "REVIEW", "Needs a look"
    # Nothing to judge yet: the bill is not read, or SAP did not answer.
    UNKNOWN = "UNKNOWN", "Not checked"


class ReviewDecision(models.TextChoices):
    OK = "OK", "OK"
    NOT_OK = "NOT_OK", "Not OK"


class APInvoiceDraftCheck(models.Model):
    """One line of an entry's audit checklist.

    ``status`` is the app's own finding and is rewritten every time the checks
    run. A person's decision sits beside it and outlives a re-run, because what
    they looked at (a signature, a QC register) is not something the app can
    see change.
    """

    draft = models.ForeignKey(
        APInvoiceDraft,
        on_delete=models.CASCADE,
        related_name="checks",
    )
    key = models.CharField(max_length=40)
    position = models.PositiveSmallIntegerField(default=0)
    label = models.CharField(max_length=200)

    status = models.CharField(
        max_length=10,
        choices=CheckStatus.choices,
        default=CheckStatus.UNKNOWN,
    )
    detail = models.TextField(blank=True)
    # What the finding was made from, for the page to show (per-line tables).
    facts = models.JSONField(default=dict, blank=True)

    review_decision = models.CharField(
        max_length=10,
        choices=ReviewDecision.choices,
        blank=True,
    )
    review_remark = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ap_invoice_draft_checks_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["draft", "key"], name="ap_invoice_draft_check_unique_key"
            ),
        ]

    def __str__(self):
        return f"{self.draft.entry_no} - {self.key}: {self.effective_status}"

    @property
    def effective_status(self) -> str:
        """A person's decision, when there is one, outranks the app's finding."""
        if self.review_decision == ReviewDecision.OK:
            return CheckStatus.PASS
        if self.review_decision == ReviewDecision.NOT_OK:
            return CheckStatus.FAIL
        return self.status
