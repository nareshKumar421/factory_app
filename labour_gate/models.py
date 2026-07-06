from django.db import models
from django.conf import settings

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

    # Soft delete: a deleted row keeps its data (is_active=False) so the audit
    # trail survives and its labour is released back to "left" without losing
    # who/when. Active rows have is_active=True (BaseModel default).
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_gate_entries_deleted",
    )

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
            ("can_allocate_labour_department", "Can allocate labour to department"),
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


class LabourAuditAction(models.TextChoices):
    CREATE_IN = "CREATE_IN", "Recorded labour in"
    UPDATE_IN = "UPDATE_IN", "Updated labour-in count"
    MARK_OUT = "MARK_OUT", "Marked labour out"
    UNDO_OUT = "UNDO_OUT", "Undid labour-out batch"
    DELETE = "DELETE", "Deleted entry"
    RESTORE = "RESTORE", "Restored entry"


class LabourGateAudit(models.Model):
    """Append-only audit trail for a LabourGateEntry: who did what, when, and the
    count before/after where relevant. One row per action; never edited."""
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="labour_gate_audits"
    )
    entry = models.ForeignKey(
        LabourGateEntry, on_delete=models.CASCADE, related_name="audit_logs"
    )
    action = models.CharField(max_length=20, choices=LabourAuditAction.choices)
    detail = models.CharField(max_length=255, blank=True, default="")
    old_value = models.IntegerField(null=True, blank=True)
    new_value = models.IntegerField(null=True, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_gate_audits",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Labour Gate Audit"
        verbose_name_plural = "Labour Gate Audit"
        indexes = [
            models.Index(fields=["entry", "created_at"]),
        ]

    def __str__(self):
        return f"entry {self.entry_id}: {self.action}"
