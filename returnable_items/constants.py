"""Choice enums for the Returnable Gate Pass module."""

from django.db import models


class ReturnableStatus(models.TextChoices):
    """Lifecycle of a returnable gate pass.

    Note that OVERDUE is deliberately NOT a status: a pass past its expected
    return date is still physically OUT or PARTIALLY_RETURNED. Overdue is a
    boolean flag so it composes with every status filter instead of masking it.
    """

    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending Approval"
    PENDING_GATE_OUT = "PENDING_GATE_OUT", "Pending Gate Out"
    OUT = "OUT", "Out"
    PARTIALLY_RETURNED = "PARTIALLY_RETURNED", "Partially Returned"
    RETURNED = "RETURNED", "Returned"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


#: Statuses from which a pass may still be cancelled.
CANCELLABLE_STATUSES = [
    ReturnableStatus.DRAFT,
    ReturnableStatus.PENDING_APPROVAL,
    ReturnableStatus.PENDING_GATE_OUT,
    ReturnableStatus.OUT,
    ReturnableStatus.PARTIALLY_RETURNED,
]

#: Statuses where material is physically outside the gate.
OUTSTANDING_STATUSES = [
    ReturnableStatus.OUT,
    ReturnableStatus.PARTIALLY_RETURNED,
]


class ReturnablePurpose(models.TextChoices):
    REPAIR = "REPAIR", "Repair"
    EXCHANGE = "EXCHANGE", "Exchange / Replacement"
    CALIBRATION = "CALIBRATION", "Calibration"
    JOB_WORK = "JOB_WORK", "Job Work"
    WARRANTY_CLAIM = "WARRANTY_CLAIM", "Warranty Claim"
    TESTING = "TESTING", "Testing / Inspection"
    DEMO_TRIAL = "DEMO_TRIAL", "Demo / Trial"
    OTHER = "OTHER", "Other"


class ItemConditionOut(models.TextChoices):
    """Condition of the item at the moment it leaves the gate."""

    WORKING = "WORKING", "Working"
    FAULTY = "FAULTY", "Faulty"
    DAMAGED = "DAMAGED", "Damaged"
    NEW = "NEW", "New / Unused"
    OTHER = "OTHER", "Other"


class ItemReturnCondition(models.TextChoices):
    """Condition of the item at the moment it comes back through the gate."""

    OK = "OK", "OK"
    REPAIRED = "REPAIRED", "Repaired"
    REPLACED = "REPLACED", "Replaced"
    NOT_REPAIRED = "NOT_REPAIRED", "Not Repaired"
    DAMAGED = "DAMAGED", "Damaged"
    SCRAP = "SCRAP", "Scrap"


class ReturnableLogAction(models.TextChoices):
    """Timeline entries. Written on every transition."""

    CREATED = "CREATED", "Created"
    UPDATED = "UPDATED", "Updated"
    SUBMITTED = "SUBMITTED", "Submitted for Approval"
    APPROVED = "APPROVED", "Approved"
    APPROVAL_REJECTED = "APPROVAL_REJECTED", "Rejected by Approver"
    GATE_OUT = "GATE_OUT", "Gate Out"
    REJECTED_AT_GATE = "REJECTED_AT_GATE", "Rejected at Gate"
    RETURN_RECORDED = "RETURN_RECORDED", "Return Recorded"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Receipt Acknowledged"
    CLOSED = "CLOSED", "Closed"
    SHORT_CLOSED = "SHORT_CLOSED", "Short Closed"
    CANCELLED = "CANCELLED", "Cancelled"
    DUE_TODAY = "DUE_TODAY", "Return Due Today"
    OVERDUE_FLAGGED = "OVERDUE_FLAGGED", "Flagged Overdue"


class AttachmentDocType(models.TextChoices):
    CHALLAN = "CHALLAN", "Delivery Challan"
    PHOTO = "PHOTO", "Photo"
    INVOICE = "INVOICE", "Invoice"
    OTHER = "OTHER", "Other"


#: Permission codenames, grouped by the auth group that should hold them.
RETURNABLE_ROLE_PERMISSIONS = {
    "returnable_admin": "__all__",
    # The higher authority who signs off a pass before the gate ever sees it.
    # Deliberately a separate group from the department that raises passes —
    # nobody should approve their own request.
    "returnable_approver": [
        "can_view_returnable_module",
        "can_view_returnable_gatepass",
        "can_approve_returnable_gatepass",
        "can_cancel_returnable",
        "can_view_returnable_reports",
        "view_returnablegatepass",
        "view_returnablegatepassitem",
        "view_returnablereturnevent",
        "view_returnablereturneventitem",
        "view_returnablegatepassattachment",
        "view_returnablegatepasslog",
    ],
    # Raises passes and sends them for approval — nothing else. Cannot approve,
    # cannot acknowledge collection, cannot close or cancel. The narrowest role
    # that can still get a gate pass moving.
    "returnable_requester": [
        "can_view_returnable_module",
        "can_view_returnable_gatepass",
        "can_manage_returnable_gatepass",
        "can_submit_returnable_gatepass",
        "add_returnablegatepass",
        "view_returnablegatepass",
        "change_returnablegatepass",
        "delete_returnablegatepass",
        "add_returnablegatepassitem",
        "view_returnablegatepassitem",
        "change_returnablegatepassitem",
        "delete_returnablegatepassitem",
        "view_returnablereturnevent",
        "view_returnablereturneventitem",
        "add_returnablegatepassattachment",
        "view_returnablegatepassattachment",
        "delete_returnablegatepassattachment",
        "view_returnablegatepasslog",
    ],
    "returnable_department": [
        "can_view_returnable_module",
        "can_view_returnable_gatepass",
        "can_manage_returnable_gatepass",
        "can_submit_returnable_gatepass",
        "can_acknowledge_returnable",
        "can_close_returnable",
        "can_cancel_returnable",
        "can_view_returnable_reports",
        "add_returnablegatepass",
        "view_returnablegatepass",
        "change_returnablegatepass",
        "delete_returnablegatepass",
        "add_returnablegatepassitem",
        "view_returnablegatepassitem",
        "change_returnablegatepassitem",
        "delete_returnablegatepassitem",
        "view_returnablereturnevent",
        "view_returnablereturneventitem",
        "add_returnablegatepassattachment",
        "view_returnablegatepassattachment",
        "delete_returnablegatepassattachment",
        "view_returnablegatepasslog",
    ],
    "returnable_gate": [
        "can_view_returnable_module",
        "can_view_returnable_gatepass",
        "can_gate_out_returnable",
        "can_gate_in_returnable",
        "can_reject_returnable_at_gate",
        "view_returnablegatepass",
        "view_returnablegatepassitem",
        "add_returnablereturnevent",
        "view_returnablereturnevent",
        "add_returnablereturneventitem",
        "view_returnablereturneventitem",
        "view_returnablegatepassattachment",
        "view_returnablegatepasslog",
    ],
    "returnable_viewer": [
        "can_view_returnable_module",
        "can_view_returnable_gatepass",
        "can_view_returnable_reports",
        "view_returnablegatepass",
        "view_returnablegatepassitem",
        "view_returnablereturnevent",
        "view_returnablereturneventitem",
        "view_returnablegatepassattachment",
        "view_returnablegatepasslog",
    ],
}
