from django.db import models
from django.conf import settings

User = settings.AUTH_USER_MODEL


class EmailType(models.TextChoices):
    GATE_ENTRY_CREATED = "GATE_ENTRY_CREATED", "Gate Entry Created"
    GATE_ENTRY_STATUS_CHANGED = "GATE_ENTRY_STATUS_CHANGED", "Gate Entry Status Changed"
    SECURITY_CHECK_DONE = "SECURITY_CHECK_DONE", "Security Check Completed"
    WEIGHMENT_RECORDED = "WEIGHMENT_RECORDED", "Weighment Recorded"
    ARRIVAL_SLIP_SUBMITTED = "ARRIVAL_SLIP_SUBMITTED", "Arrival Slip Submitted"
    ARRIVAL_SLIP_SENT_BACK = "ARRIVAL_SLIP_SENT_BACK", "Arrival Slip Sent Back to Gate"
    QC_INSPECTION_SUBMITTED = "QC_INSPECTION_SUBMITTED", "QC Inspection Submitted"
    QC_CHEMIST_APPROVED = "QC_CHEMIST_APPROVED", "QC Chemist Approved"
    QC_QAM_APPROVED = "QC_QAM_APPROVED", "QC QAM Approved"
    QC_REJECTED = "QC_REJECTED", "QC Rejected"
    QC_COMPLETED = "QC_COMPLETED", "QC Completed"
    PO_RECEIVED = "PO_RECEIVED", "PO Items Received"
    GATE_ENTRY_COMPLETED = "GATE_ENTRY_COMPLETED", "Gate Entry Completed"
    GRPO_POSTED = "GRPO_POSTED", "GRPO Posted to SAP"
    GRPO_FAILED = "GRPO_FAILED", "GRPO Posting Failed"
    WELCOME = "WELCOME", "Welcome Email"
    PASSWORD_RESET = "PASSWORD_RESET", "Password Reset"
    GENERAL_ANNOUNCEMENT = "GENERAL_ANNOUNCEMENT", "General Announcement"


class EmailLog(models.Model):
    """
    Stored email log for tracking every email sent through the system.
    Mirrors the Notification model pattern for consistency.
    """
    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="email_logs",
        null=True,
        blank=True,
    )
    recipient_email = models.EmailField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="email_logs",
    )

    subject = models.CharField(max_length=255)
    body = models.TextField()
    email_type = models.CharField(
        max_length=50,
        choices=EmailType.choices,
        default=EmailType.GENERAL_ANNOUNCEMENT,
    )

    # Deep linking
    click_action_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Frontend route to navigate to on click"
    )
    reference_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Entity type: vehicle_entry, inspection, grpo_posting, etc."
    )
    reference_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="ID of the referenced entity"
    )

    # Template
    template_name = models.CharField(max_length=100, blank=True)
    extra_data = models.JSONField(default=dict, blank=True)

    # Status
    class Status(models.TextChoices):
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.SENT,
    )
    error_message = models.TextField(blank=True)

    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails_sent",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient_email", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["email_type"]),
        ]
        permissions = [
            ("can_send_email", "Can send manual emails"),
            ("can_send_bulk_email", "Can send bulk/broadcast emails"),
        ]

    def __str__(self):
        return f"{self.subject} -> {self.recipient_email} [{self.status}]"
