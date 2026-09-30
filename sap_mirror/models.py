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
