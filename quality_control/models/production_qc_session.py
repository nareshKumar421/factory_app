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
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


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
        null=True,
        blank=True,
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

    # Keep these fields for audit trail (who submitted/reviewed)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="production_qc_sessions_submitted"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="production_qc_sessions_approved"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(blank=True, default="")

    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="production_qc_sessions_rejected"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_remarks = models.TextField(blank=True, default="")

    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["session_number"]
        unique_together = ("production_run", "session_number")
        indexes = [
            models.Index(
                fields=["workflow_status", "session_type"],
                name="prodqc_wfstatus_type_idx",
            ),
        ]
        permissions = [
            ("can_view_production_qc", "Can view production QC"),
            ("can_create_production_qc", "Can create production QC session"),
            ("can_submit_production_qc", "Can submit production QC session"),
            ("can_approve_production_qc", "Can approve production QC session"),
            ("can_view_line_clearance_qc", "Can view line clearance QC"),
            ("can_approve_line_clearance_qc", "Can approve line clearance QC"),
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
        self.save(update_fields=[
            "workflow_status", "overall_result", "submitted_by",
            "submitted_at", "updated_at",
        ])

    def approve(self, user, overall_result=None, remarks=""):
        """Approve a submitted production QC session."""
        from django.utils import timezone
        if self.workflow_status != ProductionQCWorkflowStatus.SUBMITTED:
            raise ValueError("Can only approve sessions in SUBMITTED status.")
        if overall_result:
            self.overall_result = overall_result
        if self.overall_result not in {"PASS", "FAIL"}:
            raise ValueError("QC result must be PASS or FAIL before approval.")
        self.workflow_status = ProductionQCWorkflowStatus.APPROVED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.approval_remarks = remarks
        self.updated_by = user
        self.save(update_fields=[
            "workflow_status", "overall_result", "approved_by",
            "approved_at", "approval_remarks", "updated_by", "updated_at",
        ])

    def reject(self, user, remarks=""):
        """Reject a submitted production QC session."""
        from django.utils import timezone
        if self.workflow_status != ProductionQCWorkflowStatus.SUBMITTED:
            raise ValueError("Can only reject sessions in SUBMITTED status.")
        self.workflow_status = ProductionQCWorkflowStatus.REJECTED
        self.rejected_by = user
        self.rejected_at = timezone.now()
        self.rejection_remarks = remarks
        self.updated_by = user
        self.save(update_fields=[
            "workflow_status", "rejected_by", "rejected_at",
            "rejection_remarks", "updated_by", "updated_at",
        ])
