# quality_control/models/production_qc_session.py

from django.db import models
from django.conf import settings
from gate_core.models import BaseModel


class ProductionQCSessionType(models.TextChoices):
    IN_PROCESS = "IN_PROCESS", "In-Process"
    FINAL = "FINAL", "Final"


class ProductionQCWorkflowStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"


class ProductionQCSession(BaseModel):
    """
    A QC inspection session (round) for a production run.
    Each session checks all parameters for the product being produced.
    Draft = editable, Submitted = finalized and locked.
    """
    production_run = models.ForeignKey(
        "production_execution.ProductionRun",
        on_delete=models.CASCADE,
        related_name="qc_sessions"
    )

    material_type = models.ForeignKey(
        "quality_control.MaterialType",
        on_delete=models.PROTECT,
        related_name="production_qc_sessions",
        help_text="Product/material type defining which QC parameters to check"
    )

    session_number = models.PositiveSmallIntegerField(
        help_text="Auto-incremented round number per run (Round 1, 2, 3...)"
    )

    session_type = models.CharField(
        max_length=15,
        choices=ProductionQCSessionType.choices,
        default=ProductionQCSessionType.IN_PROCESS
    )

    checked_at = models.DateTimeField(
        help_text="When this QC round was performed"
    )

    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="production_qc_sessions_checked"
    )

    overall_result = models.CharField(
        max_length=10,
        choices=[("PASS", "Pass"), ("FAIL", "Fail")],
        blank=True, default="",
        help_text="Set by QC person on submission"
    )

    workflow_status = models.CharField(
        max_length=15,
        choices=ProductionQCWorkflowStatus.choices,
        default=ProductionQCWorkflowStatus.DRAFT
    )

    # Keep these fields for audit trail (who submitted)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="production_qc_sessions_submitted"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["session_number"]
        unique_together = ("production_run", "session_number")
        permissions = [
            ("can_view_production_qc", "Can view production QC"),
            ("can_create_production_qc", "Can create production QC session"),
            ("can_submit_production_qc", "Can submit production QC session"),
        ]

    def __str__(self):
        return f"Run #{self.production_run.run_number} — {self.get_session_type_display()} Round {self.session_number}"

    def submit(self, user, overall_result):
        """Submit and finalize session with PASS/FAIL result. Cannot be changed after."""
        from django.utils import timezone
        if self.workflow_status != ProductionQCWorkflowStatus.DRAFT:
            raise ValueError("Can only submit sessions in DRAFT status.")
        self.workflow_status = ProductionQCWorkflowStatus.SUBMITTED
        self.overall_result = overall_result
        self.submitted_by = user
        self.submitted_at = timezone.now()
        self.save()
