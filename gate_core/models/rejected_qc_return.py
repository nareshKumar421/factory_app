from django.db import models

from .base import BaseModel


class RejectedQCReturnStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class RejectedQCReturnEntry(BaseModel):
    """Gate-out entry for rejected QC material returned to vendor."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="rejected_qc_return_entries",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="rejected_qc_return_entries",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        related_name="rejected_qc_return_entries",
    )
    gate_out_date = models.DateField()
    out_time = models.TimeField(null=True, blank=True)
    challan_no = models.CharField(max_length=100, blank=True)
    eway_bill_no = models.CharField(max_length=100, blank=True)
    manual_sap_reference = models.CharField(max_length=100, blank=True)
    security_name = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=RejectedQCReturnStatus.choices,
        default=RejectedQCReturnStatus.COMPLETED,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "gate_out_date"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return self.entry_no

    @staticmethod
    def generate_entry_no():
        from django.utils import timezone

        today = timezone.now()
        prefix = f"RQC-{today.strftime('%Y%m%d')}"
        last = (
            RejectedQCReturnEntry.objects
            .filter(entry_no__startswith=prefix)
            .order_by("-entry_no")
            .first()
        )

        if last:
            try:
                next_number = int(last.entry_no.split("-")[-1]) + 1
            except ValueError:
                next_number = 1
        else:
            next_number = 1

        return f"{prefix}-{next_number:04d}"


class RejectedQCReturnItem(BaseModel):
    """QC rejected item included in a gate-out vendor return entry."""

    entry = models.ForeignKey(
        RejectedQCReturnEntry,
        on_delete=models.CASCADE,
        related_name="items",
    )
    inspection = models.OneToOneField(
        "quality_control.RawMaterialInspection",
        on_delete=models.PROTECT,
        related_name="rejected_qc_return_item",
    )
    gate_entry_no = models.CharField(max_length=50, blank=True)
    report_no = models.CharField(max_length=50, blank=True)
    internal_lot_no = models.CharField(max_length=50, blank=True)
    item_name = models.CharField(max_length=255, blank=True)
    supplier_name = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.entry.entry_no} - {self.item_name or self.report_no}"
