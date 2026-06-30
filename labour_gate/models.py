from django.db import models

from gate_core.models.base import BaseModel
from company.models import Company
from accounts.models import Department
from person_gatein.models import Contractor


class LabourGateEntry(BaseModel):
    """
    One day's casual-labour headcount for a (company, contractor): how many
    labourers a contractor brought IN. People leave incrementally through the
    day, recorded as related ``LabourOutBatch`` rows; ``remaining`` is what is
    still inside.

    Deliberately simple (no department / shift / supervisor workflow) — this is
    the gate's own in/out tally, separate from the ``labour_count`` man-day
    register.
    """
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="labour_gate_entries"
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="labour_gate_entries",
        null=True,
        blank=True,
    )
    contractor = models.ForeignKey(
        Contractor, on_delete=models.PROTECT, related_name="labour_gate_entries"
    )
    work_date = models.DateField()
    count_in = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("company", "department", "contractor", "work_date")
        ordering = ["-work_date", "department_id", "contractor_id"]
        verbose_name = "Labour Gate Entry"
        verbose_name_plural = "Labour Gate Entries"
        indexes = [
            models.Index(fields=["work_date"]),
        ]
        permissions = [
            ("can_record_labour_in", "Can record labour in"),
            ("can_record_labour_out", "Can record labour out"),
        ]

    def __str__(self):
        return f"{self.contractor} {self.work_date}: in={self.count_in}"

    @property
    def total_out(self):
        return sum(batch.count for batch in self.out_batches.all())

    @property
    def remaining(self):
        return self.count_in - self.total_out


class LabourGateOutBatch(BaseModel):
    """One batch of labour marked out at the gate as a group leaves."""
    entry = models.ForeignKey(
        LabourGateEntry, on_delete=models.CASCADE, related_name="out_batches"
    )
    count = models.PositiveIntegerField()

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Labour Out Batch"
        verbose_name_plural = "Labour Out Batches"

    def __str__(self):
        return f"entry {self.entry_id}: +{self.count} out"
