from django.db import models
from django.conf import settings

User = settings.AUTH_USER_MODEL


class UserDevice(models.Model):
    """
    Stores FCM tokens per user per device/browser.
    One user can have multiple active tokens (multi-device support).
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="fcm_devices"
    )
    fcm_token = models.TextField(unique=True)
    device_type = models.CharField(
        max_length=20,
        choices=[
            ("WEB", "Web Browser"),
            ("ANDROID", "Android"),
            ("IOS", "iOS"),
        ],
        default="WEB",
    )
    device_info = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_used_at"]
        verbose_name = "User Device"
        verbose_name_plural = "User Devices"

    def __str__(self):
        return f"{self.user.email} - {self.device_type}"


class NotificationType(models.TextChoices):
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
    STOCK_ALERT = "STOCK_ALERT", "Stock Level Alert"
    GENERAL_ANNOUNCEMENT = "GENERAL_ANNOUNCEMENT", "General Announcement"


class Notification(models.Model):
    """
    Stored notification for in-app notification center.
    Supports both targeted (user-specific) and broadcast notifications.
    """
    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications"
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications"
    )

    title = models.CharField(max_length=255)
    body = models.TextField()
    notification_type = models.CharField(
        max_length=50,
        choices=NotificationType.choices,
        default=NotificationType.GENERAL_ANNOUNCEMENT,
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

    # Status
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    extra_data = models.JSONField(default=dict, blank=True)

    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications_sent"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"]),
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["notification_type"]),
        ]
        permissions = [
            ("can_send_notification", "Can send manual notifications"),
            ("can_send_bulk_notification", "Can send bulk/broadcast notifications"),
        ]

    def __str__(self):
        return f"{self.title} -> {self.recipient.email}"
