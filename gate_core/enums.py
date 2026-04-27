# gate_core/enums.py

from django.db import models


class GateEntryStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SECURITY_CHECK_DONE = "SECURITY_CHECK_DONE", "Security Check Done"
    ARRIVAL_SLIP_SUBMITTED = "ARRIVAL_SLIP_SUBMITTED", "Arrival Slip Submitted"
    ARRIVAL_SLIP_REJECTED = "ARRIVAL_SLIP_REJECTED", "Arrival Slip Rejected"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    QC_PENDING = "QC_PENDING", "QC Pending"
    QC_IN_REVIEW = "QC_IN_REVIEW", "QC In Review"
    QC_AWAITING_QAM = "QC_AWAITING_QAM", "Awaiting QAM Approval"
    QC_REJECTED = "QC_REJECTED", "QC Rejected"
    QC_COMPLETED = "QC_COMPLETED", "QC Completed"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


class EntryPhase(models.TextChoices):
    """High-level phase of a gate entry, derived from GateEntryStatus."""
    GATE = "GATE", "Gate"
    QC = "QC", "QC"
    DONE = "DONE", "Done"
    CANCELLED = "CANCELLED", "Cancelled"


GATE_PHASE_STATUSES = frozenset({
    GateEntryStatus.DRAFT,
    GateEntryStatus.SECURITY_CHECK_DONE,
    GateEntryStatus.ARRIVAL_SLIP_SUBMITTED,
    GateEntryStatus.ARRIVAL_SLIP_REJECTED,
    GateEntryStatus.IN_PROGRESS,
})

QC_PHASE_STATUSES = frozenset({
    GateEntryStatus.QC_PENDING,
    GateEntryStatus.QC_IN_REVIEW,
    GateEntryStatus.QC_AWAITING_QAM,
    GateEntryStatus.QC_REJECTED,
})

GRPO_READY_STATUSES = frozenset({
    GateEntryStatus.QC_COMPLETED,
    GateEntryStatus.COMPLETED,
})


def get_entry_phase(status: str) -> str:
    """Map a GateEntryStatus value to its high-level phase for the GRPO view."""
    if status in GRPO_READY_STATUSES:
        return EntryPhase.DONE.value
    if status in GATE_PHASE_STATUSES:
        return EntryPhase.GATE.value
    if status in QC_PHASE_STATUSES:
        return EntryPhase.QC.value
    if status == GateEntryStatus.CANCELLED:
        return EntryPhase.CANCELLED.value
    return EntryPhase.GATE.value
