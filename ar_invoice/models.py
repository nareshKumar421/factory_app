"""Local records for A/R invoices raised from the factory app.

Mirror of ``ap_invoice.models`` for the sales side. The SAP document is the
source of truth; these rows hold the operator's submission (which open Sales
Order lines it invoices), remember the ObjType-13 approval draft SAP turned the
post into, and keep SO lines locked while an invoice for them is in flight —
a draft pending approval does not reduce ``RDR1.OpenQty``, so SAP alone cannot
show that a line is already spoken for.
"""
from django.db import models

from company.models import Company
from gate_core.models import BaseModel


class ARInvoiceStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    # SAP intercepted the post into an approval draft (ODRF + OWDD).
    PENDING_APPROVAL = "PENDING_APPROVAL", "Awaiting SAP approval"
    # The approver cleared it but the draft is not an OINV document yet.
    APPROVED = "APPROVED", "Approved — not yet posted"
    POSTED = "POSTED", "Posted to SAP"
    REJECTED = "REJECTED", "Rejected in approval"
    FAILED = "FAILED", "Failed"
    # Abandoned by the operator before reaching SAP — releases its SO lines.
    CANCELLED = "CANCELLED", "Cancelled"


class ARInvoicePosting(BaseModel):
    """One A/R invoice submitted against open Sales Order lines."""

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="ar_invoice_postings",
    )
    customer_code = models.CharField(max_length=50)
    customer_name = models.CharField(max_length=200, blank=True, default="")
    # The customer's PO / reference number — becomes OINV.NumAtCard (optional).
    customer_ref = models.CharField(max_length=100, blank=True, default="")
    doc_date = models.DateField(null=True, blank=True)
    doc_due_date = models.DateField(null=True, blank=True)
    tax_date = models.DateField(null=True, blank=True)
    # Sum of the selected SO lines' open row totals (pre-tax) at submission.
    selected_total = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    branch_id = models.IntegerField()
    comments = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=ARInvoiceStatus.choices,
        default=ARInvoiceStatus.PENDING,
    )
    error_message = models.TextField(blank=True, null=True)

    # Approval-side identifiers (set when SAP holds the post as a draft).
    sap_draft_entry = models.IntegerField(null=True, blank=True)
    sap_approval_code = models.IntegerField(null=True, blank=True)  # OWDD.WddCode
    approval_remarks = models.TextField(blank=True, default="")

    # Final document identifiers (set once OINV exists).
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    sap_doc_total = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ar_invoice_postings",
    )

    class Meta:
        db_table = "ar_invoice_posting"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "customer_code"]),
            models.Index(fields=["company", "status"]),
            models.Index(fields=["sap_draft_entry"]),
            models.Index(fields=["sap_doc_entry"]),
        ]
        default_permissions = ()
        permissions = [
            ("view_ar_invoice_posting", "Can view A/R invoice postings"),
            ("create_ar_invoice_posting", "Can create and post A/R invoices"),
        ]

    def __str__(self):
        return f"{self.company.code} {self.customer_code} AR invoice #{self.pk}"


class ARInvoiceLine(models.Model):
    """One invoice line: either a Sales Order line the invoice consumes
    (``base_entry``/``base_line`` set, snapshot at submit) or a free line of a
    direct/cash sale (base fields empty; item, quantity and price entered)."""

    ar_invoice = models.ForeignKey(
        ARInvoicePosting,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    base_entry = models.IntegerField(null=True, blank=True)  # ORDR.DocEntry
    base_line = models.IntegerField(null=True, blank=True)   # RDR1.LineNum
    base_doc_num = models.IntegerField(null=True, blank=True)
    item_code = models.CharField(max_length=50, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    # The SO line's OPEN quantity at submit — what the invoice will carry.
    quantity = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    line_total = models.DecimalField(max_digits=18, decimal_places=2)
    tax_code = models.CharField(max_length=50, blank=True, default="")
    warehouse_code = models.CharField(max_length=20, blank=True, default="")
    # Dimension-1 profit centre (OcrCode/CostingCode) — set on direct-sale
    # lines; SO-copied lines inherit theirs from the base document.
    cost_center = models.CharField(max_length=30, blank=True, default="")

    class Meta:
        db_table = "ar_invoice_line"
        indexes = [models.Index(fields=["base_entry", "base_line"])]

    def __str__(self):
        return f"AR invoice line SO {self.base_doc_num or self.base_entry}/{self.base_line}"


class ARInvoiceAttachment(models.Model):
    """Optional supporting document; uploaded to SAP Attachments2 before the post."""

    ar_invoice = models.ForeignKey(
        ARInvoicePosting,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="ar_invoice_attachments/")
    original_filename = models.CharField(max_length=255)
    sap_attachment_status = models.CharField(
        max_length=20,
        choices=[
            ("PENDING", "Pending Upload"),
            ("UPLOADED", "Uploaded to SAP"),
            ("LINKED", "Linked to SAP Document"),
            ("FAILED", "Upload Failed"),
        ],
        default="PENDING",
    )
    sap_absolute_entry = models.IntegerField(null=True, blank=True)
    sap_error_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ar_invoice_attachments",
    )

    class Meta:
        db_table = "ar_invoice_attachment"
        ordering = ["id"]

    def __str__(self):
        return f"Attachment for AR invoice {self.ar_invoice_id} - {self.original_filename}"


class ARPaymentStatus(models.TextChoices):
    """Whether the money for an invoice has come in, as this app records it.

    Deliberately the app's own book, not SAP's. SAP calls an invoice "closed"
    only once accounts apply an incoming payment against it, which for a counter
    cash sale can be days after the cash was actually taken — so its status
    answers "has the receipt been keyed?", not "did we get paid?". This answers
    the second question, which is the one the person raising the bill has.
    """

    PENDING = "PENDING", "Payment pending"
    PARTIAL = "PARTIAL", "Partly received"
    RECEIVED = "RECEIVED", "Payment received"


class ARPaymentMode(models.TextChoices):
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    BANK = "BANK", "Bank transfer"
    CHEQUE = "CHEQUE", "Cheque"
    CARD = "CARD", "Card"
    OTHER = "OTHER", "Other"


class ARInvoicePayment(BaseModel):
    """One invoice's payment-received record, keyed on SAP's ``DocEntry``.

    History shows two books — the invoices this app raised and the cash sales
    SAP holds (most of which the counter raised in SAP directly) — and the same
    bill can appear in both. Keying on ``sap_doc_entry`` rather than on the app
    record means one mark covers the bill wherever it is seen, and the counter's
    own bills, which have no record here, can be tracked at all.

    ``ar_invoice`` is set whenever this app did raise the bill; it exists so the
    app-side History can prefetch the marks instead of a second lookup, and is
    null for the counter's invoices.

    No row means untracked, which reads as unpaid; an explicit ``PENDING`` row
    is different — somebody looked and the money is still outstanding.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="ar_invoice_payments",
    )
    sap_doc_entry = models.IntegerField()
    # Snapshot of the human-facing bill number, so a mark is still readable in
    # the admin without a SAP round-trip.
    sap_doc_num = models.IntegerField(null=True, blank=True)
    ar_invoice = models.ForeignKey(
        ARInvoicePosting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
    )

    status = models.CharField(
        max_length=20,
        choices=ARPaymentStatus.choices,
        default=ARPaymentStatus.PENDING,
    )
    # The day the money came in — required once anything is received.
    received_on = models.DateField(null=True, blank=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )
    mode = models.CharField(
        max_length=20, choices=ARPaymentMode.choices, blank=True, default=""
    )
    # UPI ref, cheque no., bank UTR — whatever proves the receipt.
    reference = models.CharField(max_length=100, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    class Meta:
        db_table = "ar_invoice_payment"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sap_doc_entry"],
                name="uniq_ar_payment_per_invoice",
            )
        ]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "sap_doc_entry"]),
        ]
        default_permissions = ()
        permissions = [
            (
                "mark_ar_invoice_payment",
                "Can record whether an A/R invoice has been paid",
            ),
        ]

    def __str__(self):
        return f"{self.company.code} invoice {self.sap_doc_num or self.sap_doc_entry}: {self.status}"
