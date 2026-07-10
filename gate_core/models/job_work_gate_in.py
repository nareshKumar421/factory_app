from django.db import models
from django.utils import timezone

from .base import BaseModel


class JobWorkGateInStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class JobWorkGateIn(BaseModel):
    """Gate record for job-work/refinery movement.

    The SAP GRPO fields are retained for older entries. New oil-refining
    entries are created at the gate without a SAP source document and can be
    linked to a SAP production order later.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="job_work_gate_ins",
    )
    entry_no = models.CharField(max_length=50, unique=True)
    vehicle_entry = models.ForeignKey(
        "driver_management.VehicleEntry",
        on_delete=models.PROTECT,
        related_name="job_work_gate_ins",
    )
    vehicle = models.ForeignKey(
        "vehicle_management.Vehicle",
        on_delete=models.PROTECT,
        related_name="job_work_gate_ins",
    )
    driver = models.ForeignKey(
        "driver_management.Driver",
        on_delete=models.PROTECT,
        related_name="job_work_gate_ins",
    )
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.CharField(max_length=50, blank=True)
    sap_doc_date = models.DateField(null=True, blank=True)
    sap_doc_time = models.TimeField(null=True, blank=True)
    sap_supplier_code = models.CharField(max_length=50, blank=True)
    sap_supplier_name = models.CharField(max_length=255, blank=True)
    sap_reference = models.CharField(max_length=100, blank=True)
    sap_comments = models.TextField(blank=True)
    sap_branch_id = models.IntegerField(null=True, blank=True)
    production_order_doc_entry = models.IntegerField(null=True, blank=True)
    production_order_doc_num = models.CharField(max_length=50, blank=True)
    production_item_code = models.CharField(max_length=100, blank=True)
    production_item_name = models.CharField(max_length=255, blank=True)
    production_planned_qty = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    production_completed_qty = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    production_rejected_qty = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    production_remaining_qty = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True
    )
    production_start_date = models.DateField(null=True, blank=True)
    production_due_date = models.DateField(null=True, blank=True)
    production_warehouse = models.CharField(max_length=50, blank=True)
    production_status = models.CharField(max_length=20, blank=True)
    gate_in_date = models.DateField()
    in_time = models.TimeField()
    security_name = models.CharField(max_length=100, blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=JobWorkGateInStatus.choices,
        default=JobWorkGateInStatus.IN_PROGRESS,
    )

    class Meta:
        ordering = ["-gate_in_date", "-in_time", "-created_at"]
        indexes = [
            models.Index(fields=["company", "gate_in_date"]),
            models.Index(fields=["status"]),
            models.Index(fields=["vehicle"]),
            models.Index(fields=["sap_doc_entry"]),
            models.Index(fields=["sap_doc_num"]),
            models.Index(fields=["production_order_doc_entry"]),
            models.Index(fields=["production_order_doc_num"]),
        ]
        permissions = [
            ("can_view_job_work", "Can view job work gate entries"),
            ("can_create_job_work", "Can create/edit job work gate entries"),
            ("can_complete_job_work", "Can complete job work gate entries"),
        ]

    def __str__(self):
        return self.entry_no

    @staticmethod
    def generate_entry_no():
        today = timezone.now()
        prefix = f"JWIN-{today.strftime('%Y%m%d')}"
        last = (
            JobWorkGateIn.objects
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


class JobWorkGateInItem(BaseModel):
    """Snapshot of linked SAP production-order components or legacy GRPO lines."""

    job_work_gate_in = models.ForeignKey(
        JobWorkGateIn,
        on_delete=models.CASCADE,
        related_name="items",
    )
    line_num = models.IntegerField()
    item_code = models.CharField(max_length=100, blank=True)
    item_name = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    uom = models.CharField(max_length=50, blank=True)
    warehouse_code = models.CharField(max_length=50, blank=True)
    base_type = models.IntegerField(null=True, blank=True)
    base_entry = models.IntegerField(null=True, blank=True)
    base_line = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["line_num"]
        constraints = [
            models.UniqueConstraint(
                fields=["job_work_gate_in", "line_num"],
                name="unique_job_work_gate_in_line",
            ),
        ]

    def __str__(self):
        return f"{self.job_work_gate_in.entry_no} - {self.item_code}"
