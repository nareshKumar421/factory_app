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
