"""
Request Labour -- what each department needs on the NEXT day's shifts.

This is the evening half of the labour story. ``labour_gate`` records what a
contractor actually brought through the gate today and how it was split across
departments; this app records what the departments say they will need tomorrow,
so whoever calls the contractors in the evening has a number to call with.

One row per (company, department, work_date, shift). A department raises it, an
approver decides it, and the approved figure -- not the asked-for figure -- is
what the plant is expected to arrange. A rejected or partly approved request
keeps both numbers so the difference is visible the next morning.

Edits are soft: deleting keeps the row (``is_active=False``) so the audit trail
and the released number survive, exactly as ``labour_gate.LabourGateEntry``
does, and a delete can be undone inside a short grace window.
"""

from django.conf import settings
from django.db import models

from accounts.models import Department
from company.models import Company
from gate_core.models.base import BaseModel


class LabourShift(models.TextChoices):
    DAY = "DAY", "Day"
    NIGHT = "NIGHT", "Night"


class LabourRequestStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class LabourRequest(BaseModel):
    """One department's ask for one shift of one day.

    ``requested_count`` is what the department wants; ``approved_count`` is what
    the approver granted and is only meaningful once ``status`` is APPROVED --
    it may be lower than the ask (a partial approval) and is never higher,
    because approving more than was asked for is a new request, not a decision.
    """

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="labour_requests"
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="labour_requests"
    )
    work_date = models.DateField(help_text="The day the labour is needed")
    shift = models.CharField(
        max_length=5, choices=LabourShift.choices, default=LabourShift.DAY
    )
    requested_count = models.PositiveIntegerField(default=0)
    note = models.CharField(
        max_length=255, blank=True, default="", help_text="What the labour is for"
    )

    status = models.CharField(
        max_length=10,
        choices=LabourRequestStatus.choices,
        default=LabourRequestStatus.PENDING,
    )
    approved_count = models.PositiveIntegerField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_requests_decided",
    )

    # Soft delete -- see the module docstring.
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_requests_deleted",
    )

    class Meta:
        unique_together = ("company", "department", "work_date", "shift")
        ordering = ["-work_date", "shift", "department_id"]
        verbose_name = "Labour Request"
        verbose_name_plural = "Labour Requests"
        indexes = [
            models.Index(fields=["work_date", "shift"]),
            models.Index(fields=["status"]),
        ]
        permissions = [
            ("can_view_labour_request", "Can view labour requests"),
            ("can_raise_labour_request", "Can raise a labour request for a department"),
            ("can_decide_labour_request", "Can approve or reject a labour request"),
        ]

    def __str__(self):
        return f"{self.department} {self.work_date} {self.shift}: {self.requested_count}"

    @property
    def is_decided(self):
        return self.status != LabourRequestStatus.PENDING

    @property
    def effective_count(self):
        """The number the plant should actually arrange.

        The approved figure once a decision exists, otherwise the ask -- a
        pending request is still the department's best statement of need, and a
        rejected one is zero.
        """
        if self.status == LabourRequestStatus.APPROVED:
            return self.approved_count if self.approved_count is not None else self.requested_count
        if self.status == LabourRequestStatus.REJECTED:
            return 0
        return self.requested_count


class LabourRequestAction(models.TextChoices):
    CREATE = "CREATE", "Raised request"
    UPDATE = "UPDATE", "Updated request"
    APPROVE = "APPROVE", "Approved request"
    REJECT = "REJECT", "Rejected request"
    REOPEN = "REOPEN", "Reopened request"
    DELETE = "DELETE", "Deleted request"
    RESTORE = "RESTORE", "Restored request"


class LabourRequestAudit(models.Model):
    """Append-only trail for one request: who did what, when, and the count
    before/after where relevant. One row per action; never edited."""

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="labour_request_audits"
    )
    request = models.ForeignKey(
        LabourRequest, on_delete=models.CASCADE, related_name="audit_logs"
    )
    action = models.CharField(max_length=20, choices=LabourRequestAction.choices)
    detail = models.CharField(max_length=255, blank=True, default="")
    old_value = models.IntegerField(null=True, blank=True)
    new_value = models.IntegerField(null=True, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_request_audits",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Labour Request Audit"
        verbose_name_plural = "Labour Request Audit"
        indexes = [
            models.Index(fields=["request", "created_at"]),
        ]

    def __str__(self):
        return f"request {self.request_id}: {self.action}"
