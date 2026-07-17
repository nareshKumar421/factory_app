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
    QC_HOLD = "QC_HOLD", "QC On Hold"
    QC_COMPLETED = "QC_COMPLETED", "QC Completed"
    FACTORY_HEAD_DECISION_REQUIRED = (
        "FACTORY_HEAD_DECISION_REQUIRED",
        "Factory Head Decision Required",
    )
    FACTORY_HEAD_DECISION_RECORDED = (
        "FACTORY_HEAD_DECISION_RECORDED",
        "Factory Head Decision Recorded",
    )
    PO_RECEIVED = "PO_RECEIVED", "PO Items Received"
    GATE_ENTRY_COMPLETED = "GATE_ENTRY_COMPLETED", "Gate Entry Completed"
    DAILY_NEED_ENTRY_CREATED = "DAILY_NEED_ENTRY_CREATED", "Daily Need Gate Entry Created"
    MAINTENANCE_ENTRY_CREATED = "MAINTENANCE_ENTRY_CREATED", "Maintenance Gate Entry Created"
    CONSTRUCTION_ENTRY_CREATED = "CONSTRUCTION_ENTRY_CREATED", "Construction Gate Entry Created"
    PERSON_ENTRY_CREATED = "PERSON_ENTRY_CREATED", "Person Gate Entry Created"
    PERSON_ENTRY_EXITED = "PERSON_ENTRY_EXITED", "Person Gate Exit Recorded"
    GRPO_POSTED = "GRPO_POSTED", "GRPO Posted to SAP"
    GRPO_FAILED = "GRPO_FAILED", "GRPO Posting Failed"
    SERVICE_GRPO_POSTED = "SERVICE_GRPO_POSTED", "Service GRPO Posted to SAP"
    SERVICE_GRPO_FAILED = "SERVICE_GRPO_FAILED", "Service GRPO Posting Failed"
    BOM_REQUEST_CREATED = "BOM_REQUEST_CREATED", "BOM Request Submitted to Warehouse"
    BOM_REQUEST_REVIEWED = "BOM_REQUEST_REVIEWED", "BOM Request Reviewed"
    FG_RECEIPT_POSTED = "FG_RECEIPT_POSTED", "Finished Goods Receipt Posted"
    FG_RECEIPT_FAILED = "FG_RECEIPT_FAILED", "Finished Goods Receipt Failed"
    PRODUCTION_RUN_SAP_POSTED = "PRODUCTION_RUN_SAP_POSTED", "Production Run Posted to SAP"
    PRODUCTION_RUN_SAP_FAILED = "PRODUCTION_RUN_SAP_FAILED", "Production Run SAP Posting Failed"
    DISPATCH_PLAN_BOOKED = "DISPATCH_PLAN_BOOKED", "Dispatch Plan Booked"
    DISPATCH_PLAN_DISPATCHED = "DISPATCH_PLAN_DISPATCHED", "Dispatch Plan Dispatched"
    INTERCOMPANY_TRANSFER_COMPLETED = "INTERCOMPANY_TRANSFER_COMPLETED", "Intercompany Transfer Completed"
    INTERCOMPANY_TRANSFER_FAILED = "INTERCOMPANY_TRANSFER_FAILED", "Intercompany Transfer SAP Failed"
    STOCK_ALERT = "STOCK_ALERT", "Stock Level Alert"
    DOCKING_SCAN_SKIP_REQUESTED = "DOCKING_SCAN_SKIP_REQUESTED", "Docking Scan Skip Requested"
    DOCKING_SCAN_SKIP_REVIEWED = "DOCKING_SCAN_SKIP_REVIEWED", "Docking Scan Skip Reviewed"
    WORK_PERMIT_SUBMITTED = "WORK_PERMIT_SUBMITTED", "Work Permit Submitted for Approval"
    WORK_PERMIT_APPROVED = "WORK_PERMIT_APPROVED", "Work Permit Approved"
    WORK_PERMIT_EXPIRED = "WORK_PERMIT_EXPIRED", "Work Permit Expired"
    MATERIAL_INDENT_SUBMITTED = "MATERIAL_INDENT_SUBMITTED", "Material Indent Submitted to Store"
    MATERIAL_INDENT_ISSUED = "MATERIAL_INDENT_ISSUED", "Material Indent Items Issued"
    MATERIAL_INDENT_FORWARDED = "MATERIAL_INDENT_FORWARDED", "Material Indent Forwarded for Approval"
    MATERIAL_INDENT_APPROVED = "MATERIAL_INDENT_APPROVED", "Material Indent Approved for Purchase"
    MATERIAL_INDENT_REJECTED = "MATERIAL_INDENT_REJECTED", "Material Indent Rejected"
    MATERIAL_INDENT_PURCHASED = "MATERIAL_INDENT_PURCHASED", "Material Indent Purchased"
    RETURNABLE_SUBMITTED = "RETURNABLE_SUBMITTED", "Returnable Gate Pass Awaiting Approval"
    RETURNABLE_APPROVED = "RETURNABLE_APPROVED", "Returnable Gate Pass Approved"
    RETURNABLE_APPROVAL_REJECTED = (
        "RETURNABLE_APPROVAL_REJECTED",
        "Returnable Gate Pass Rejected by Approver",
    )
    RETURNABLE_GATE_OUT = "RETURNABLE_GATE_OUT", "Returnable Items Gated Out"
    RETURNABLE_REJECTED_AT_GATE = "RETURNABLE_REJECTED_AT_GATE", "Returnable Gate Pass Rejected at Gate"
    RETURNABLE_RETURN_RECORDED = "RETURNABLE_RETURN_RECORDED", "Returnable Items Returned"
    RETURNABLE_ACKNOWLEDGED = "RETURNABLE_ACKNOWLEDGED", "Returnable Items Collected by Department"
    RETURNABLE_DUE_TODAY = "RETURNABLE_DUE_TODAY", "Returnable Items Due for Return Today"
    RETURNABLE_OVERDUE = "RETURNABLE_OVERDUE", "Returnable Items Overdue"
    RETURNABLE_CLOSED = "RETURNABLE_CLOSED", "Returnable Gate Pass Closed"
    RETURNABLE_CANCELLED = "RETURNABLE_CANCELLED", "Returnable Gate Pass Cancelled"
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


class NotificationPreference(models.Model):
    """
    Per-user opt-in/out for each notification type.
    Missing rows are treated as enabled so existing users keep receiving notifications.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notification_preferences",
    )
    notification_type = models.CharField(
        max_length=50,
        choices=NotificationType.choices,
    )
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["notification_type"]
        unique_together = ("user", "notification_type")
        indexes = [
            models.Index(fields=["user", "notification_type"]),
        ]
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"

    def __str__(self):
        state = "enabled" if self.is_enabled else "disabled"
        return f"{self.user.email} - {self.notification_type} ({state})"
