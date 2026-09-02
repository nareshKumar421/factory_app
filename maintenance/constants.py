from django.db import models

#: Assets are created without a category (the form no longer asks), so the
#: non-null FK is satisfied with this per-company bucket.
DEFAULT_ASSET_CATEGORY_NAME = "General"


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
    #: Work done, waiting for the raiser to verify it on the floor.
    COMPLETED = "COMPLETED", "Completed"
    #: The raiser was not satisfied and sent the job back to the technician.
    REOPENED = "REOPENED", "Reopened"
    APPROVED = "APPROVED", "Approved"
    CLOSED = "CLOSED", "Closed"


class WorkOrderLogAction(models.TextChoices):
    """One line in a work order's hand-off trail."""

    ASSIGNED = "ASSIGNED", "Assigned"
    STARTED = "STARTED", "Started"
    STATUS = "STATUS", "Status Changed"
    COMPLETED = "COMPLETED", "Completed"
    SENT_BACK = "SENT_BACK", "Sent Back"
    VERIFIED = "VERIFIED", "Verified"
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


class WorkOrderAttachmentDocType(models.TextChoices):
    """What a file attached to a work order actually is.

    Kept separate from WorkOrderPhotoType: photos document the before/after
    state of the job, attachments are the paperwork around it (the fault note
    the raiser scanned, a vendor quote, the service sheet, the bill).
    """

    COMPLAINT = "COMPLAINT", "Complaint / Fault Note"
    QUOTATION = "QUOTATION", "Quotation"
    SERVICE_REPORT = "SERVICE_REPORT", "Service Report"
    INVOICE = "INVOICE", "Invoice / Bill"
    DRAWING = "DRAWING", "Drawing"
    OTHER = "OTHER", "Other"


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


class FireShiftType(models.TextChoices):
    DAY = "DAY", "Day Shift"
    NIGHT = "NIGHT", "Night Shift"


class FireReportStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted"
    REVIEWED = "REVIEWED", "Reviewed"


class FireEquipmentStatus(models.TextChoices):
    OK = "OK", "OK"
    NOT_OK = "NOT_OK", "Not Okay"
    NEEDS_ATTENTION = "NEEDS_ATTENTION", "Needs Attention"


class FireEquipmentType(models.TextChoices):
    PUMP = "PUMP", "Pump"
    HYDRANT = "HYDRANT", "Hydrant"
    EXTINGUISHER = "EXTINGUISHER", "Extinguisher"
    SPRINKLER = "SPRINKLER", "Sprinkler"
    ALARM_PANEL = "ALARM_PANEL", "Alarm / Panel"
    HOSE = "HOSE", "Hose / Reel"
    OTHER = "OTHER", "Other"


class FireIssueStatus(models.TextChoices):
    ISSUED = "ISSUED", "Issued"
    PARTIALLY_RETURNED = "PARTIALLY_RETURNED", "Partially Returned"
    RETURNED = "RETURNED", "Returned"


class FireReturnCondition(models.TextChoices):
    OK = "OK", "OK"
    DAMAGED = "DAMAGED", "Damaged"
    LOST = "LOST", "Lost"


class WorkPermitType(models.TextChoices):
    GENERAL = "GENERAL", "General"
    HEIGHT = "HEIGHT", "Height Work"
    HOT_WORK = "HOT_WORK", "Hot Work"
    COLD_WORK = "COLD_WORK", "Cold Work"
    CONFINED_SPACE = "CONFINED_SPACE", "Confined Space"
    LINE_BREAKING = "LINE_BREAKING", "Line Breaking"
    HAZARDOUS_ENERGY_CONTROL = "HAZARDOUS_ENERGY_CONTROL", "Hazardous Energy Control"
    EXCAVATION = "EXCAVATION", "Excavation"
    LOADING_UNLOADING_HAZMAT = "LOADING_UNLOADING_HAZMAT", "Loading / Unloading of Hazardous Material"


class WorkPermitStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted for Approval"
    APPROVED = "APPROVED", "Approved"
    IN_PROGRESS = "IN_PROGRESS", "Work In Progress"
    COMPLETED = "COMPLETED", "Completed"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"


class WorkPermitApprovalRole(models.TextChoices):
    FIRE_DEPARTMENT_HEAD = "FIRE_DEPARTMENT_HEAD", "Fire Department Head"
    ISSUER = "ISSUER", "Issuer"
    AREA_INCHARGE = "AREA_INCHARGE", "Area Incharge"
    SAFETY_COORDINATOR = "SAFETY_COORDINATOR", "Safety Co-ordinator"
    FACTORY_MANAGER = "FACTORY_MANAGER", "Factory / Plant Manager"


class WorkCompletionType(models.TextChoices):
    ABANDONED = "ABANDONED", "Abandoned"
    VERIFIED = "VERIFIED", "Verified Closure"


class SafetyFineStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PAID = "PAID", "Paid"
    WAIVED = "WAIVED", "Waived"


class MaterialIndentStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted to Store"
    ISSUED = "ISSUED", "Issued from Store"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Purchase Approval"
    APPROVED = "APPROVED", "Approved for Purchase"
    #: Purchaser collected quotations from several companies and sent them back
    #: to the approver, who picks the company to buy from.
    PENDING_QUOTATION_SELECTION = (
        "PENDING_QUOTATION_SELECTION",
        "Pending Company Selection",
    )
    #: Approver picked a company; the purchaser now buys and closes the indent.
    QUOTATION_SELECTED = "QUOTATION_SELECTED", "Company Selected"
    PURCHASED = "PURCHASED", "Purchased"
    GATE_IN = "GATE_IN", "Arrived at Gate"
    RECEIVED = "RECEIVED", "Received into Store"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class MaterialIndentDocType(models.TextChoices):
    INVOICE = "INVOICE", "Invoice"
    BILL = "BILL", "Bill"
    #: The written quote a company sent, attached to its quotation row.
    QUOTATION = "QUOTATION", "Quotation"
    OTHER = "OTHER", "Other"


class MaterialIndentPriority(models.TextChoices):
    LOW = "LOW", "Low"
    NORMAL = "NORMAL", "Normal"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


def choices_payload(choices):
    return [{"value": value, "label": label} for value, label in choices]
