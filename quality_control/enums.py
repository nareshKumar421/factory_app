# quality_control/enums.py

from django.db import models


class ArrivalSlipStatus(models.TextChoices):
    """Status for Material Arrival Slip"""
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted to QA"
    REJECTED = "REJECTED", "Rejected by QA"


class InspectionStatus(models.TextChoices):
    """Final status for Raw Material Inspection"""
    PENDING = "PENDING", "Pending"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"
    HOLD = "HOLD", "On Hold"


class InspectionWorkflowStatus(models.TextChoices):
    """Workflow status for inspection approval chain"""
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    QA_CHEMIST_APPROVED = "QA_CHEMIST_APPROVED", "QA Chemist Approved"
    QAM_APPROVED = "QAM_APPROVED", "QAM Approved"
    REJECTED = "REJECTED", "Rejected"
    COMPLETED = "COMPLETED", "Completed"


class FactoryHeadDecision(models.TextChoices):
    """Factory head decision after a QA Manager rejection."""
    ACCEPT_QC_OVERRIDE = "ACCEPT_QC_OVERRIDE", "Accept QC Override"
    RETURN_TO_VENDOR = "RETURN_TO_VENDOR", "Return to Vendor"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW", "Hold for Review"
    SEND_FOR_RECHECK = "SEND_FOR_RECHECK", "Send for Recheck"
    SCRAP = "SCRAP", "Scrap"


class ParameterType(models.TextChoices):
    """Types of QC parameters"""
    NUMERIC = "NUMERIC", "Numeric"
    TEXT = "TEXT", "Text/Descriptive"
    BOOLEAN = "BOOLEAN", "Pass/Fail"
    RANGE = "RANGE", "Numeric Range"
