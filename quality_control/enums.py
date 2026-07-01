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


class InspectionDecision(models.TextChoices):
    """Actor decision for raw material QC approval checkpoints."""
    APPROVED = "APPROVED", "Approved"
    HOLD = "HOLD", "Hold"
    REJECTED = "REJECTED", "Rejected"


class InspectionWorkflowStatus(models.TextChoices):
    """Workflow status for inspection approval chain"""
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    QA_CHEMIST_APPROVED = "QA_CHEMIST_APPROVED", "QA Chemist Approved"
    QAM_APPROVED = "QAM_APPROVED", "QAM Approved"
    REJECTED = "REJECTED", "Rejected"
    COMPLETED = "COMPLETED", "Completed"


class ParameterType(models.TextChoices):
    """Types of QC parameters"""
    NUMERIC = "NUMERIC", "Numeric"
    TEXT = "TEXT", "Text/Descriptive"
    BOOLEAN = "BOOLEAN", "Pass/Fail"
    RANGE = "RANGE", "Numeric Range"
