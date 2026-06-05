from django.db import models


class MaintenancePriority(models.TextChoices):
    NORMAL = "NORMAL", "Normal"
    HIGH = "HIGH", "High"
    CRITICAL = "CRITICAL", "Critical"


class PMFrequency(models.TextChoices):
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    QUARTERLY = "QUARTERLY", "Quarterly"
    HALF_YEARLY = "HALF_YEARLY", "Half-Yearly"
    YEARLY = "YEARLY", "Yearly"


class PMExecutionStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"
    SKIPPED = "SKIPPED", "Skipped"
    OVERDUE = "OVERDUE", "Overdue"


class ChecklistInputType(models.TextChoices):
    CHECKBOX = "CHECKBOX", "Checkbox"
    PASS_FAIL = "PASS_FAIL", "Pass / Fail"
    NUMBER = "NUMBER", "Number"
    TEXT = "TEXT", "Text"


class WorkType(models.TextChoices):
    COMPLAINT = "COMPLAINT", "Complaint"
    BREAKDOWN = "BREAKDOWN", "Breakdown"
    GENERAL = "GENERAL", "General Maintenance"
    PREVENTIVE = "PREVENTIVE", "Preventive Maintenance"
    INSPECTION = "INSPECTION", "Inspection"
    CALIBRATION = "CALIBRATION", "Calibration"
    AMC_VENDOR = "AMC_VENDOR", "AMC / Vendor Visit"
    PROJECT = "PROJECT", "Project / Improvement"


class WorkOrderStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    OPEN = "OPEN", "Open"
    ASSIGNED = "ASSIGNED", "Assigned"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    WAITING_SPARE = "WAITING_SPARE", "Waiting Spare"
    WAITING_VENDOR = "WAITING_VENDOR", "Waiting Vendor"
    ON_HOLD = "ON_HOLD", "On Hold"
    COMPLETED = "COMPLETED", "Completed"
    APPROVED = "APPROVED", "Approved"
    CLOSED = "CLOSED", "Closed"


class WorkImpact(models.TextChoices):
    NO_IMPACT = "NO_IMPACT", "No Production Impact"
    DEGRADED = "DEGRADED", "Reduced Performance"
    STOPPAGE = "STOPPAGE", "Production Stoppage"
    SAFETY_RISK = "SAFETY_RISK", "Safety Risk"


class WorkOrderPhotoType(models.TextChoices):
    BEFORE = "BEFORE", "Before"
    AFTER = "AFTER", "After"
    GENERAL = "GENERAL", "General"


class AssetStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    IDLE = "IDLE", "Idle"
    BREAKDOWN = "BREAKDOWN", "Breakdown"
    UNDER_PM = "UNDER_PM", "Under PM"
    UNDER_REPAIR = "UNDER_REPAIR", "Under Repair"
    RETIRED = "RETIRED", "Retired"


class AssetHierarchyLevel(models.TextChoices):
    PLANT = "PLANT", "Plant"
    AREA = "AREA", "Area"
    LINE = "LINE", "Line"
    MACHINE = "MACHINE", "Machine"
    COMPONENT = "COMPONENT", "Component"
    UTILITY = "UTILITY", "Utility"


class AssetDocumentType(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    WARRANTY = "WARRANTY", "Warranty"
    AMC = "AMC", "AMC"
    SERVICE_REPORT = "SERVICE_REPORT", "Service Report"
    CALIBRATION = "CALIBRATION", "Calibration"
    OTHER = "OTHER", "Other"


class SpareRequestStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED", "Partially Issued"
    ISSUED = "ISSUED", "Issued"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED", "Partially Consumed"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class SpareMovementType(models.TextChoices):
    RECEIPT = "RECEIPT", "Receipt from Gate"
    ISSUE = "ISSUE", "Issue to Work Order"
    CONSUME = "CONSUME", "Consume on Work Order"
    RETURN = "RETURN", "Return Unused Spare"
    ADJUSTMENT = "ADJUSTMENT", "Stock Adjustment"


class GateQCStatus(models.TextChoices):
    NOT_REQUIRED = "NOT_REQUIRED", "Not Required"
    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"
    WAIVED = "WAIVED", "Waived"


class GateReceiptStatus(models.TextChoices):
    NOT_RECEIVED = "NOT_RECEIVED", "Not Received"
    RECEIVED = "RECEIVED", "Received"
    BLOCKED = "BLOCKED", "Blocked"


class VendorVisitStatus(models.TextChoices):
    PLANNED = "PLANNED", "Planned"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


def choices_payload(choices):
    return [{"value": value, "label": label} for value, label in choices]
