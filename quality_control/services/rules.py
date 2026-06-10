# quality_control/services/rules.py

from ..enums import InspectionStatus, InspectionWorkflowStatus


def can_complete_gate(po_items):
    """
    Gate can complete only if all PO items have completed QC inspection.

    A PO item's QC is complete when:
    1. It has an arrival slip
    2. The arrival slip has an inspection
    3. The inspection has final_status of ACCEPTED or REJECTED (not PENDING or HOLD)

    Args:
        po_items: List of POItemReceipt objects

    Returns:
        bool: True if all items have completed QC, False otherwise
    """
    if not po_items:
        return False

    for item in po_items:
        # Check if arrival slip exists
        if not hasattr(item, 'arrival_slip') or item.arrival_slip is None:
            return False

        arrival_slip = item.arrival_slip

        # Check if inspection exists
        if not hasattr(arrival_slip, 'inspection') or arrival_slip.inspection is None:
            return False

        inspection = arrival_slip.inspection

        # Check if inspection is completed (ACCEPTED or REJECTED)
        if inspection.final_status not in [InspectionStatus.ACCEPTED, InspectionStatus.REJECTED]:
            return False

    return True


def compute_entry_status(vehicle_entry):
    """
    Compute the correct GateEntryStatus based on the overall QC progress
    of ALL items in the vehicle entry.

    This is the single source of truth for entry status during QC flow.
    It looks at all PO items and picks the status that reflects the
    least-progressed item (the bottleneck).

    Returns the appropriate GateEntryStatus value.
    """
    from gate_core.enums import GateEntryStatus

    # Collect workflow and final statuses of all inspections. QAM approval
    # alone is not enough to complete QC; the item needs a final decision.
    statuses = []
    final_statuses = []
    has_items = False

    for po in vehicle_entry.po_receipts.all():
        for item in po.items.all():
            has_items = True
            slip = getattr(item, "arrival_slip", None)
            if slip is None:
                return GateEntryStatus.QC_PENDING

            inspection = getattr(slip, "inspection", None)
            if inspection is None:
                return GateEntryStatus.QC_PENDING

            statuses.append(inspection.workflow_status)
            final_statuses.append(inspection.final_status)

    if not has_items:
        return vehicle_entry.status  # no change

    final_decisions = {InspectionStatus.ACCEPTED, InspectionStatus.REJECTED}

    # If ALL items are terminal → QC_COMPLETED
    if all(s in final_decisions for s in final_statuses):
        return GateEntryStatus.QC_COMPLETED

    # If ANY item is rejected but others aren't done → QC_REJECTED
    if (
        InspectionWorkflowStatus.REJECTED in statuses
        or InspectionStatus.REJECTED in final_statuses
    ):
        return GateEntryStatus.QC_REJECTED

    # Otherwise, find the highest stage any item has reached
    # Priority order (highest first):
    stage_priority = {
        InspectionWorkflowStatus.QAM_APPROVED: 4,
        InspectionWorkflowStatus.QA_CHEMIST_APPROVED: 3,
        InspectionWorkflowStatus.SUBMITTED: 2,
        InspectionWorkflowStatus.DRAFT: 1,
    }

    max_stage = max(stage_priority.get(s, 0) for s in statuses)

    if max_stage >= 3:
        return GateEntryStatus.QC_AWAITING_QAM
    if max_stage >= 2:
        return GateEntryStatus.QC_IN_REVIEW

    return GateEntryStatus.QC_PENDING


def update_entry_status(vehicle_entry):
    """
    Compute and save the correct entry status based on QC progress.
    Only updates if the status actually changed.
    """
    new_status = compute_entry_status(vehicle_entry)
    if vehicle_entry.status != new_status:
        vehicle_entry.status = new_status
        vehicle_entry.save(update_fields=["status"])
    return new_status


def check_and_mark_qc_completed(vehicle_entry):
    """
    Check if all QC inspections for a vehicle entry are completed.
    If so, transition the entry status to QC_COMPLETED.
    """
    from gate_core.enums import GateEntryStatus

    # Collect all PO items
    po_items = []
    for po in vehicle_entry.po_receipts.all():
        po_items.extend(list(po.items.all()))

    if not po_items:
        return False

    # Check if all items have completed QC
    if not can_complete_gate(po_items):
        return False

    vehicle_entry.status = GateEntryStatus.QC_COMPLETED
    vehicle_entry.save(update_fields=["status"])

    return True
