from django.db import models
from django.conf import settings

User = settings.AUTH_USER_MODEL


class LabourVerificationRequest(models.Model):
    STATUS_CHOICES = (
        ("OPEN", "Open"),
        ("CLOSED", "Closed"),
    )

    date = models.DateField(unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="OPEN")
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="verification_requests_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-date"]
        permissions = [
            ("can_create_verification_request", "Can create labour verification request"),
            ("can_view_verification_request", "Can view labour verification requests"),
            ("can_close_verification_request", "Can close labour verification request"),
        ]

    def __str__(self):
        return f"Verification {self.date} - {self.status}"


class DepartmentLabourResponse(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("SUBMITTED", "Submitted"),
    )

    verification_request = models.ForeignKey(
        LabourVerificationRequest,
        on_delete=models.CASCADE,
        related_name="responses",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.CASCADE,
        related_name="labour_responses",
    )
    labour_count = models.PositiveIntegerField(default=0)
    labour_details = models.JSONField(default=list, blank=True)
    remarks = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labour_responses_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("verification_request", "department")
        ordering = ["department__name"]
        permissions = [
            ("can_submit_department_labour", "Can submit department labour count"),
        ]

    def __str__(self):
        return f"{self.department.name} - {self.status} ({self.labour_count})"
