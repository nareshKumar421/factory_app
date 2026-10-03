"""Copies of the SAP reads the floor cannot work without, for when HANA is down.

The app reads SAP live. A copy is only served when HANA does not answer, and the
screen then says how old it is -- so a copy never hides a change SAP has made,
it only keeps work going while SAP cannot be asked. ``manage.py sync_sap_copy``
takes the copies (see ``services.DATASETS``); each is replaced whole, so a copy
is always one SAP answer, never a mix of two.
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class MirrorDataset(models.Model):
    """One SAP list copied for one company, and how fresh the copy is."""

    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE, related_name="sap_mirror_datasets"
    )
    #: Which list, e.g. ``fg_items``; see ``services.DATASETS``.
    name = models.CharField(max_length=40)
    #: When SAP gave the answer the copy holds -- the "as of" a screen shows.
    synced_at = models.DateTimeField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    #: Why the last try did not replace the copy; empty after one that did.
    last_error = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_sap_mirror_dataset"
            ),
        ]
        ordering = ["company__code", "name"]

    def __str__(self):
        return f"{self.company.code} {self.name}"


class MirrorRow(models.Model):
    """One row of a copied list, exactly as the live read returns it."""

    dataset = models.ForeignKey(MirrorDataset, on_delete=models.CASCADE, related_name="rows")
    #: The row's SAP code (item code, warehouse code), unique in its list.
    key = models.CharField(max_length=100)
    #: Lower-cased code and name, for the same search the live read offers.
    search_text = models.CharField(max_length=400, blank=True)
    data = models.JSONField(encoder=DjangoJSONEncoder)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dataset", "key"], name="unique_sap_mirror_row"),
        ]
        ordering = ["key"]

    def __str__(self):
        return self.key


class MirroredBill(models.Model):
    """One A/R bill from the last 30 days, exactly as the dispatch reader gives it.

    ``bill``, ``lines`` and ``pickable_lines`` are the outputs of the reader's
    ``list_bills``, ``list_bill_lines`` and ``list_pickable_lines`` for this bill
    (packed by ``codec``); the columns beside them are what those reads filter
    and sort on, so a fallback read filters in SQL rather than in Python.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE, related_name="sap_mirror_bills"
    )
    doc_entry = models.PositiveIntegerField()
    doc_num = models.CharField(max_length=30)
    #: DocNum as a number, for SAP's own newest-first order.
    doc_num_sort = models.BigIntegerField(default=0)
    create_date = models.DateField(null=True)
    create_time = models.CharField(max_length=10, blank=True)
    branch_id = models.IntegerField(null=True)
    branch_name = models.CharField(max_length=200, blank=True)
    #: ``|BH-PC|BH-BT|``: every warehouse a line comes out of, upper-cased.
    warehouse_codes = models.CharField(max_length=1000, blank=True)
    #: SAP holds a dispatch date on it (``U_Dipatch_Date``).
    sap_dispatched = models.BooleanField(default=False)
    #: A live credit note is based on it.
    credited = models.BooleanField(default=False)
    #: ``UpdateDate|UpdateTS`` when copied: a different one means re-read it.
    version = models.CharField(max_length=40, blank=True)
    bill = models.JSONField()
    lines = models.JSONField(default=list)
    pickable_lines = models.JSONField(default=list)
    copied_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "doc_entry"], name="unique_sap_mirror_bill"
            ),
        ]
        indexes = [
            models.Index(fields=["company", "create_date"], name="sap_mirror_bill_created"),
            models.Index(fields=["company", "doc_num"], name="sap_mirror_bill_doc_num"),
        ]

    def __str__(self):
        return f"{self.company_id} bill {self.doc_num}"


class MirroredPurchaseOrder(models.Model):
    """One purchase order with open lines, as the PO reader reads it.

    ``rows`` are its open lines in the reader's own row shape
    (``po_reader.OPEN_LINE_COLUMNS`` plus a finished-goods flag), packed by
    ``codec``, so a copied answer goes through the same transform as a live one.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE, related_name="sap_mirror_purchase_orders"
    )
    doc_entry = models.PositiveIntegerField()
    doc_num = models.CharField(max_length=30)
    supplier_code = models.CharField(max_length=50)
    #: ``UpdateDate|UpdateTS|open qty|open lines`` when copied.
    version = models.CharField(max_length=80, blank=True)
    rows = models.JSONField(default=list)
    copied_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_entry"], name="unique_sap_mirror_po"),
        ]
        indexes = [
            models.Index(fields=["company", "supplier_code"], name="sap_mirror_po_supplier"),
            models.Index(fields=["company", "doc_num"], name="sap_mirror_po_doc_num"),
        ]

    def __str__(self):
        return f"{self.company_id} PO {self.doc_num}"

class ServedBillOutcome(models.TextChoices):
    PENDING = "PENDING", "Not checked yet"
    UNCHANGED = "UNCHANGED", "Unchanged in SAP"
    CHANGED = "CHANGED", "Changed in SAP"
    CANCELLED = "CANCELLED", "Cancelled in SAP"
    CREDITED = "CREDITED", "Credited in SAP"


class ServedBill(models.Model):
    """A bill the app handed out from the copy while HANA was down.

    Recorded when a single bill is read from the copy -- looked up by number or
    DocEntry, or its lines read -- which is what docking, barcode dispatch, a
    bill summary or a short dispatch does with it; a list a page merely shows
    is not. Once SAP answers again (``recheck``) it is compared with what SAP
    now holds, and anything cancelled, credited or changed is told to the
    people who started work on it.
    """

    company = models.ForeignKey(
        "company.Company", on_delete=models.CASCADE, related_name="sap_mirror_served_bills"
    )
    doc_entry = models.PositiveIntegerField()
    doc_num = models.CharField(max_length=30)
    served_at = models.DateTimeField()
    last_served_at = models.DateTimeField()
    #: When the copy it was served from was taken.
    copy_as_of = models.DateTimeField(null=True)
    #: What was handed out: ``{"bill": ..., "lines": [...]}``, packed by ``codec``.
    served = models.JSONField()
    outcome = models.CharField(
        max_length=20, choices=ServedBillOutcome.choices,
        default=ServedBillOutcome.PENDING, db_index=True,
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    #: What differs from what was handed out, one line per difference.
    differences = models.JSONField(default=list, blank=True)
    #: The app records started from it, as the alert named them.
    linked = models.JSONField(default=list, blank=True)
    #: How many people were told.
    notified = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "doc_entry"],
                condition=models.Q(outcome="PENDING"),
                name="unique_sap_mirror_pending_served_bill",
            ),
        ]
        ordering = ["-served_at"]

    def __str__(self):
        return f"bill {self.doc_num} ({self.get_outcome_display()})"
