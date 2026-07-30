"""
Activity registry — the single source of truth for "what work is pending, and whose is it".

Each :class:`ActivitySource` is one *job* a user is responsible for, expressed as a live
query against an existing module's records. Nothing is duplicated or manually ticked:
a job is pending because a real record is sitting in a status that needs that action,
and it is complete because the record carries the acting user and a timestamp.

Two ways a pending item is attributed to a user:

``OWNED``
    The record names the user (``owner_field`` — e.g. a work order's ``assigned_to``,
    a returnable pass's ``recipient``, or the ``created_by`` of an unsubmitted draft).
    It is that person's job and nobody else's.

``QUEUE``
    The record names nobody yet — it waits for whoever holds ``permission``
    (e.g. any indent approver). Everyone with the permission sees it, and the first
    to act clears it for all of them. Queue items are flagged so the UI can say
    "shared queue" rather than implying sole ownership.

Adding a job means adding a row here — no view or serializer changes.
"""

from dataclasses import dataclass, field
from typing import Optional

OWNED = "OWNED"
QUEUE = "QUEUE"


@dataclass(frozen=True)
class ActivitySource:
    """One pending-work definition mapped onto an existing module's model."""

    key: str
    """Stable identifier, safe to use in URLs and UI filters."""

    label: str
    """The job, in the words used on the user's job sheet."""

    module: str
    """Grouping shown in the UI, e.g. ``Maintenance - Work Orders``."""

    permission: str
    """``app_label.codename`` that makes a user responsible for this job."""

    model: str
    """``app_label.ModelName`` holding the records."""

    pending_filter: dict
    """ORM kwargs selecting records still awaiting this action."""

    mode: str = QUEUE
    """:data:`OWNED` or :data:`QUEUE` — see the module docstring."""

    owner_field: Optional[str] = None
    """User FK that owns a pending record. Required when ``mode`` is :data:`OWNED`."""

    actor_field: Optional[str] = None
    """User FK stamped when the job is done — drives the "completed" side."""

    actor_date_field: Optional[str] = None
    """Timestamp paired with ``actor_field``. Falls back to ``updated_at``."""

    reference_field: Optional[str] = None
    """Human-readable document number shown in the list."""

    age_field: str = "created_at"
    """Timestamp the pending age / overdue calculation is measured from."""

    company_field: Optional[str] = "company"
    """FK to ``company.Company``, or ``None`` for models that are not company-scoped."""

    url_template: Optional[str] = None
    """Frontend deep link, ``{id}`` substituted. ``None`` renders a non-clickable row."""

    overdue_after_days: int = 2
    """A pending item older than this is surfaced as overdue."""

    extra_select: tuple = field(default_factory=tuple)
    """Extra ``select_related`` hops needed by ``reference_field``."""


# ---------------------------------------------------------------------------
# Maintenance - Work Orders
# ---------------------------------------------------------------------------
_WORK_ORDERS = [
    ActivitySource(
        key="wo_submit_draft",
        label="Submit your draft work order",
        module="Maintenance - Work Orders",
        permission="maintenance.can_create_work_order",
        model="maintenance.MaintenanceWorkOrder",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="work_order_no",
        url_template="/maintenance/work-orders/{id}",
    ),
    ActivitySource(
        key="wo_approve",
        label="Approve and assign the work order",
        module="Maintenance - Work Orders",
        permission="maintenance.can_approve_work_order",
        model="maintenance.MaintenanceWorkOrder",
        pending_filter={"status": "OPEN"},
        actor_field="approved_by",
        actor_date_field="approved_at",
        reference_field="work_order_no",
        url_template="/maintenance/work-orders/{id}",
    ),
    ActivitySource(
        key="wo_start",
        label="Start the work order assigned to you",
        module="Maintenance - Work Orders",
        permission="maintenance.can_start_work_order",
        model="maintenance.MaintenanceWorkOrder",
        pending_filter={"status": "ASSIGNED"},
        mode=OWNED,
        owner_field="assigned_to",
        reference_field="work_order_no",
        url_template="/maintenance/work-orders/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="wo_complete",
        label="Complete the job you are working on",
        module="Maintenance - Work Orders",
        permission="maintenance.can_complete_work_order",
        model="maintenance.MaintenanceWorkOrder",
        pending_filter={
            "status__in": ["IN_PROGRESS", "WAITING_SPARE", "WAITING_VENDOR", "ON_HOLD"]
        },
        mode=OWNED,
        owner_field="assigned_to",
        reference_field="work_order_no",
        age_field="start_time",
        url_template="/maintenance/work-orders/{id}",
        overdue_after_days=3,
    ),
    ActivitySource(
        key="wo_close",
        label="Close the completed work order",
        module="Maintenance - Work Orders",
        permission="maintenance.can_close_work_order",
        model="maintenance.MaintenanceWorkOrder",
        pending_filter={"status__in": ["COMPLETED", "APPROVED"]},
        actor_field="closed_by",
        actor_date_field="closed_at",
        reference_field="work_order_no",
        age_field="completed_at",
        url_template="/maintenance/work-orders/{id}",
    ),
]

# ---------------------------------------------------------------------------
# Maintenance / Fire - Work Permits
# ---------------------------------------------------------------------------
_WORK_PERMITS = [
    ActivitySource(
        key="wp_submit_draft",
        label="Submit your draft permit to work",
        module="Work Permits",
        permission="maintenance.can_manage_work_permit",
        model="maintenance.WorkPermit",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="serial_no",
        url_template="/fire/work-permits",
    ),
    ActivitySource(
        key="wp_approve",
        label="Approve the permit to work",
        module="Work Permits",
        permission="maintenance.can_approve_work_permit",
        model="maintenance.WorkPermit",
        pending_filter={"status": "SUBMITTED"},
        actor_field="approved_by",
        actor_date_field="approved_at",
        reference_field="serial_no",
        age_field="submitted_at",
        url_template="/fire/work-permits",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="wp_accept",
        label="Accept the approved permit before starting work",
        module="Work Permits",
        permission="maintenance.can_accept_work_permit",
        model="maintenance.WorkPermit",
        pending_filter={"status": "APPROVED"},
        actor_field="accepted_by",
        actor_date_field="accepted_at",
        reference_field="serial_no",
        age_field="approved_at",
        url_template="/fire/work-permits",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="wp_close",
        label="Close the permit on completion",
        module="Work Permits",
        permission="maintenance.can_close_work_permit",
        model="maintenance.WorkPermit",
        pending_filter={"status__in": ["IN_PROGRESS", "COMPLETED"]},
        reference_field="serial_no",
        url_template="/fire/work-permits",
    ),
    ActivitySource(
        key="wp_expired",
        label="Renew or close the expired permit",
        module="Work Permits",
        permission="maintenance.can_manage_work_permit",
        model="maintenance.WorkPermit",
        pending_filter={"status": "EXPIRED"},
        reference_field="serial_no",
        age_field="expired_at",
        url_template="/fire/work-permits",
        overdue_after_days=0,
    ),
]

# ---------------------------------------------------------------------------
# Maintenance - Material Indents (6-stage chain)
# ---------------------------------------------------------------------------
_INDENTS = [
    ActivitySource(
        key="mi_submit_draft",
        label="Submit your draft material indent",
        module="Maintenance - Indents",
        permission="maintenance.can_manage_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="indent_no",
        url_template="/maintenance/material-indents",
    ),
    ActivitySource(
        key="mi_review",
        label="Store review of the material indent",
        module="Maintenance - Indents",
        permission="maintenance.can_review_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status__in": ["SUBMITTED", "ISSUED"]},
        actor_field="reviewed_by",
        actor_date_field="reviewed_at",
        reference_field="indent_no",
        age_field="submitted_at",
        url_template="/maintenance/material-indents",
    ),
    ActivitySource(
        key="mi_approve",
        label="Approve the material indent",
        module="Maintenance - Indents",
        permission="maintenance.can_approve_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status": "PENDING_APPROVAL"},
        actor_field="approved_by",
        actor_date_field="approved_at",
        reference_field="indent_no",
        age_field="reviewed_at",
        url_template="/maintenance/material-indents",
    ),
    ActivitySource(
        key="mi_purchase",
        label="Raise the purchase against the approved indent",
        module="Maintenance - Indents",
        permission="maintenance.can_purchase_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status": "APPROVED"},
        actor_field="purchased_by",
        actor_date_field="purchased_at",
        reference_field="indent_no",
        age_field="approved_at",
        url_template="/maintenance/material-indents",
    ),
    ActivitySource(
        key="mi_gate_in",
        label="Gate-in the indent material",
        module="Maintenance - Indents",
        permission="maintenance.can_gatein_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status": "PURCHASED"},
        actor_field="gate_in_by",
        actor_date_field="gate_in_at",
        reference_field="indent_no",
        age_field="purchased_at",
        url_template="/maintenance/material-indents",
    ),
    ActivitySource(
        key="mi_receive",
        label="Receive the indent material into store",
        module="Maintenance - Indents",
        permission="maintenance.can_receive_material_indent",
        model="maintenance.MaterialIndent",
        pending_filter={"status": "GATE_IN"},
        actor_field="received_by",
        actor_date_field="received_at",
        reference_field="indent_no",
        age_field="gate_in_at",
        url_template="/maintenance/material-indents",
    ),
]

# ---------------------------------------------------------------------------
# Maintenance - PM / Fire & Safety
# ---------------------------------------------------------------------------
_MAINT_OTHER = [
    ActivitySource(
        key="pm_execute",
        label="Execute the preventive-maintenance plan",
        module="Maintenance - PM",
        permission="maintenance.can_manage_pm",
        model="maintenance.PreventiveMaintenanceExecution",
        pending_filter={"status__in": ["PENDING", "OVERDUE"]},
        actor_field="completed_by",
        actor_date_field="completed_at",
        url_template="/maintenance/pm",
    ),
    ActivitySource(
        key="pm_finish",
        label="Finish the PM you started",
        module="Maintenance - PM",
        permission="maintenance.can_manage_pm",
        model="maintenance.PreventiveMaintenanceExecution",
        pending_filter={"status": "IN_PROGRESS"},
        age_field="started_at",
        url_template="/maintenance/pm",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="fire_report_review",
        label="Review the submitted fire shift report",
        module="Fire & Safety",
        permission="maintenance.can_review_fire_report",
        model="maintenance.FireShiftReport",
        pending_filter={"status": "SUBMITTED"},
        actor_field="reviewed_by",
        actor_date_field="reviewed_at",
        url_template="/fire/reports",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="safety_fine_settle",
        label="Settle or waive the pending safety fine",
        module="Fire & Safety",
        permission="maintenance.can_manage_safety_fine",
        model="maintenance.SafetyFine",
        pending_filter={"status": "PENDING"},
        actor_field="settled_by",
        actor_date_field="settled_at",
        reference_field="fine_no",
        age_field="issued_at",
        url_template="/fire/safety-fines",
        overdue_after_days=7,
    ),
    ActivitySource(
        key="fire_equipment_return",
        label="Follow up fire equipment still issued out",
        module="Fire & Safety",
        permission="maintenance.can_manage_fire_issue",
        model="maintenance.FireEquipmentIssue",
        pending_filter={"status__in": ["ISSUED", "PARTIALLY_RETURNED"]},
        url_template="/fire/equipment",
        overdue_after_days=7,
    ),
]

# ---------------------------------------------------------------------------
# Quality Control
# ---------------------------------------------------------------------------
_QC = [
    ActivitySource(
        key="qc_slip_submit",
        label="Submit the material arrival slip",
        module="Quality Control",
        permission="quality_control.can_submit_arrival_slip",
        model="quality_control.MaterialArrivalSlip",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        actor_field="submitted_by",
        actor_date_field="submitted_at",
        reference_field="truck_no_as_per_bill",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="qc_slip_rejected",
        label="Correct the arrival slip sent back to you",
        module="Quality Control",
        permission="quality_control.can_submit_arrival_slip",
        model="quality_control.MaterialArrivalSlip",
        pending_filter={"status": "REJECTED"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="truck_no_as_per_bill",
        age_field="sent_back_at",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="qc_chemist_approve",
        label="Give chemist approval on the inspection",
        module="Quality Control",
        permission="quality_control.can_approve_as_chemist",
        model="quality_control.RawMaterialInspection",
        pending_filter={
            "qa_chemist_approved_at__isnull": True,
            "rejected_at__isnull": True,
        },
        actor_field="qa_chemist",
        actor_date_field="qa_chemist_approved_at",
        reference_field="report_no",
        company_field=None,
        url_template="/qc/arrival-slips/inspections/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="qc_qam_approve",
        label="Give final QAM approval on the inspection",
        module="Quality Control",
        permission="quality_control.can_approve_as_qam",
        model="quality_control.RawMaterialInspection",
        pending_filter={
            "qa_chemist_approved_at__isnull": False,
            "qam_approved_at__isnull": True,
            "rejected_at__isnull": True,
        },
        actor_field="qam",
        actor_date_field="qam_approved_at",
        reference_field="report_no",
        age_field="qa_chemist_approved_at",
        company_field=None,
        url_template="/qc/arrival-slips/inspections/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="qc_prod_submit",
        label="Submit the production QC session",
        module="Quality Control",
        permission="quality_control.can_submit_production_qc",
        model="quality_control.ProductionQCSession",
        pending_filter={"submitted_at__isnull": True},
        mode=OWNED,
        owner_field="created_by",
        actor_field="submitted_by",
        actor_date_field="submitted_at",
        reference_field="session_number",
        company_field=None,
        url_template="/qc/production/sessions/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="qc_prod_approve",
        label="Approve the production QC session",
        module="Quality Control",
        permission="quality_control.can_approve_production_qc",
        model="quality_control.ProductionQCSession",
        pending_filter={
            "submitted_at__isnull": False,
            "approved_at__isnull": True,
            "rejected_at__isnull": True,
        },
        actor_field="approved_by",
        actor_date_field="approved_at",
        reference_field="session_number",
        age_field="submitted_at",
        company_field=None,
        url_template="/qc/production/sessions/{id}",
        overdue_after_days=1,
    ),
]

# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------
_PRODUCTION = [
    ActivitySource(
        key="run_submit_draft",
        label="Start your draft production run",
        module="Production",
        permission="production_execution.can_create_production_run",
        model="production_execution.ProductionRun",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="run_number",
        url_template="/production/execution/runs/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="run_complete",
        label="Complete the production run you started",
        module="Production",
        permission="production_execution.can_complete_production_run",
        model="production_execution.ProductionRun",
        pending_filter={"status": "IN_PROGRESS"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="run_number",
        url_template="/production/execution/runs/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="lc_submit",
        label="Submit your line clearance",
        module="Production",
        permission="production_execution.can_create_line_clearance",
        model="production_execution.LineClearance",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        url_template="/production/execution/line-clearance/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="lc_qa_approve",
        label="Give QA sign-off on line clearance",
        module="Production",
        permission="production_execution.can_approve_line_clearance_qa",
        model="production_execution.LineClearance",
        pending_filter={"status__in": ["SUBMITTED", "ON_HOLD"]},
        actor_field="qa_approved_by",
        actor_date_field="qa_approved_at",
        url_template="/production/execution/line-clearance/{id}",
        overdue_after_days=0,
    ),
    ActivitySource(
        key="waste_engineer",
        label="Approve waste log - Engineer stage",
        module="Production - Waste",
        permission="production_execution.can_approve_waste_engineer",
        model="production_execution.WasteLog",
        pending_filter={"engineer_signed_at__isnull": True},
        actor_field="engineer_signed_by",
        actor_date_field="engineer_signed_at",
        reference_field="material_code",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="waste_am",
        label="Approve waste log - Area Manager stage",
        module="Production - Waste",
        permission="production_execution.can_approve_waste_am",
        model="production_execution.WasteLog",
        pending_filter={
            "engineer_signed_at__isnull": False,
            "am_signed_at__isnull": True,
        },
        actor_field="am_signed_by",
        actor_date_field="am_signed_at",
        reference_field="material_code",
        age_field="engineer_signed_at",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="waste_hod",
        label="Approve waste log - HOD stage",
        module="Production - Waste",
        permission="production_execution.can_approve_waste_hod",
        model="production_execution.WasteLog",
        pending_filter={
            "am_signed_at__isnull": False,
            "hod_signed_at__isnull": True,
        },
        actor_field="hod_signed_by",
        actor_date_field="hod_signed_at",
        reference_field="material_code",
        age_field="am_signed_at",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="waste_store",
        label="Approve waste log - Store stage",
        module="Production - Waste",
        permission="production_execution.can_approve_waste_store",
        model="production_execution.WasteLog",
        pending_filter={
            "hod_signed_at__isnull": False,
            "store_signed_at__isnull": True,
        },
        actor_field="store_signed_by",
        actor_date_field="store_signed_at",
        reference_field="material_code",
        age_field="hod_signed_at",
        company_field=None,
        overdue_after_days=1,
    ),
    ActivitySource(
        key="blowing_submit_draft",
        label="Start your draft blowing run",
        module="Blowing",
        permission="blowing.can_create_blowing_run",
        model="blowing.BlowingRun",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="run_number",
        url_template="/production/blowing/runs/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="blowing_complete",
        label="Complete the blowing run you started",
        module="Blowing",
        permission="blowing.can_complete_blowing_run",
        model="blowing.BlowingRun",
        pending_filter={"status": "IN_PROGRESS"},
        mode=OWNED,
        owner_field="created_by",
        reference_field="run_number",
        url_template="/production/blowing/runs/{id}",
        overdue_after_days=1,
    ),
]

# ---------------------------------------------------------------------------
# Returnable Items
# ---------------------------------------------------------------------------
_RETURNABLE = [
    ActivitySource(
        key="rgp_submit",
        label="Submit the returnable gate pass",
        module="Returnable Items",
        permission="returnable_items.can_submit_returnable_gatepass",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status": "DRAFT"},
        mode=OWNED,
        owner_field="created_by",
        actor_field="submitted_by",
        actor_date_field="submitted_at",
        reference_field="pass_no",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="rgp_approve",
        label="Approve the returnable gate pass",
        module="Returnable Items",
        permission="returnable_items.can_approve_returnable_gatepass",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status": "PENDING_APPROVAL"},
        actor_field="approved_by",
        actor_date_field="approved_at",
        reference_field="pass_no",
        age_field="submitted_at",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="rgp_gate_out",
        label="Gate-out the returnable material",
        module="Returnable Items",
        permission="returnable_items.can_gate_out_returnable",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status": "PENDING_GATE_OUT"},
        actor_field="gate_out_by",
        actor_date_field="gate_out_at",
        reference_field="pass_no",
        age_field="approved_at",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="rgp_return_own",
        label="Return the material issued in your name",
        module="Returnable Items",
        permission="returnable_items.can_view_returnable_gatepass",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status__in": ["OUT", "PARTIALLY_RETURNED"]},
        mode=OWNED,
        owner_field="recipient",
        reference_field="pass_no",
        age_field="gate_out_at",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=7,
    ),
    ActivitySource(
        key="rgp_gate_in",
        label="Gate-in the material when it returns",
        module="Returnable Items",
        permission="returnable_items.can_gate_in_returnable",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status__in": ["OUT", "PARTIALLY_RETURNED"]},
        reference_field="pass_no",
        age_field="gate_out_at",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=7,
    ),
    ActivitySource(
        key="rgp_close",
        label="Acknowledge and close the returned pass",
        module="Returnable Items",
        permission="returnable_items.can_close_returnable",
        model="returnable_items.ReturnableGatePass",
        pending_filter={"status": "RETURNED"},
        actor_field="closed_by",
        actor_date_field="closed_at",
        reference_field="pass_no",
        age_field="last_return_at",
        url_template="/maintenance/returnable/{id}",
        overdue_after_days=1,
    ),
]

# ---------------------------------------------------------------------------
# Warehouse & Stores
# ---------------------------------------------------------------------------
_WAREHOUSE = [
    ActivitySource(
        key="bom_approve",
        label="Approve the BOM request",
        module="Warehouse - BOM & FG",
        permission="warehouse.can_approve_bom_request",
        model="warehouse.BOMRequest",
        pending_filter={"status": "PENDING"},
        actor_field="reviewed_by",
        actor_date_field="reviewed_at",
        url_template="/warehouse/bom-requests/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="fg_receive",
        label="Receive the finished goods",
        module="Warehouse - BOM & FG",
        permission="warehouse.can_receive_fg",
        model="warehouse.FinishedGoodsReceipt",
        pending_filter={"status": "PENDING"},
        actor_field="received_by",
        actor_date_field="received_at",
        reference_field="item_code",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="fg_post_sap",
        label="Post the FG receipt to SAP",
        module="Warehouse - BOM & FG",
        permission="warehouse.can_post_fg_to_sap",
        model="warehouse.FinishedGoodsReceipt",
        pending_filter={"status": "RECEIVED"},
        reference_field="item_code",
        age_field="received_at",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="fg_failed",
        label="Fix and repost the failed FG receipt",
        module="Warehouse - BOM & FG",
        permission="warehouse.can_post_fg_to_sap",
        model="warehouse.FinishedGoodsReceipt",
        pending_filter={"status": "FAILED"},
        reference_field="item_code",
        overdue_after_days=0,
    ),
    ActivitySource(
        key="bst_scan",
        label="Scan boxes onto the BST",
        module="Warehouse - BST",
        permission="warehouse.can_scan_bst",
        model="warehouse.BSTTransfer",
        pending_filter={"status__in": ["DRAFT", "SCANNING"]},
        reference_field="entry_no",
        url_template="/warehouse/bst/{id}/scan",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bst_dispatch",
        label="Dispatch the scanned BST",
        module="Warehouse - BST",
        permission="warehouse.can_dispatch_bst",
        model="warehouse.BSTTransfer",
        pending_filter={"status": "AWAITING_GATE_OUT"},
        actor_field="dispatched_by",
        actor_date_field="dispatched_at",
        reference_field="entry_no",
        url_template="/warehouse/bst/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bst_gate_out",
        label="Gate-out the BST",
        module="Warehouse - BST",
        permission="warehouse.can_gate_bst",
        model="warehouse.BSTTransfer",
        pending_filter={"status": "DISPATCHED"},
        actor_field="gated_out_by",
        actor_date_field="gated_out_at",
        reference_field="entry_no",
        url_template="/gate/bst-out/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bst_gate_in",
        label="Gate-in the arriving BST",
        module="Warehouse - BST",
        permission="warehouse.can_gate_bst",
        model="warehouse.BSTTransfer",
        pending_filter={"status__in": ["IN_TRANSIT", "AWAITING_GATE_IN"]},
        actor_field="gated_in_by",
        actor_date_field="gated_in_at",
        reference_field="entry_no",
        url_template="/warehouse/bst/incoming/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bst_receive",
        label="Receive the BST into stock",
        module="Warehouse - BST",
        permission="warehouse.can_receive_bst",
        model="warehouse.BSTTransfer",
        pending_filter={
            "status__in": ["GATED_IN", "ARRIVED", "RECEIVING", "PARTIALLY_RECEIVED"]
        },
        actor_field="received_by",
        actor_date_field="received_at",
        reference_field="entry_no",
        url_template="/warehouse/bst/incoming/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="grpo_post",
        label="Post the pending GRPO",
        module="Warehouse - GRPO",
        permission="grpo.can_preview_grpo",
        model="grpo.GRPOPosting",
        pending_filter={"status__in": ["DRAFT", "PENDING"]},
        actor_field="posted_by",
        actor_date_field="posted_at",
        company_field=None,
        url_template="/warehouse/grpo/history/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="grpo_retry",
        label="Retry the failed GRPO posting",
        module="Warehouse - GRPO",
        permission="grpo.can_preview_grpo",
        model="grpo.GRPOPosting",
        pending_filter={"status__in": ["FAILED", "PARTIALLY_POSTED"]},
        company_field=None,
        url_template="/warehouse/grpo/history/{id}",
        overdue_after_days=0,
    ),
    ActivitySource(
        key="service_grpo_post",
        label="Post the pending Service GRPO",
        module="Warehouse - GRPO",
        permission="dispatch_plans.can_post_bilty_service_grpo",
        model="grpo.ServiceGRPOPosting",
        pending_filter={"status__in": ["DRAFT", "PENDING", "FAILED"]},
        actor_field="posted_by",
        actor_date_field="posted_at",
        company_field=None,
        url_template="/warehouse/grpo/service/history/{id}",
        overdue_after_days=1,
    ),
]

# ---------------------------------------------------------------------------
# Dispatch & Docking
# ---------------------------------------------------------------------------
_DISPATCH = [
    ActivitySource(
        key="sd_photo",
        label="Attach dispatch photos / challan weight",
        module="Dispatch - Gate Out",
        permission="gate_core.can_upload_sales_dispatch_photo",
        model="gate_core.SalesDispatchGateOut",
        pending_filter={"status": "DOCKED"},
        actor_field="photo_uploaded_by",
        actor_date_field="photo_uploaded_at",
        reference_field="entry_no",
        age_field="docked_at",
        url_template="/gate/sales-dispatch/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="sd_print",
        label="Print the dispatch gate pass",
        module="Dispatch - Gate Out",
        permission="gate_core.can_print_sales_dispatch_gatepass",
        model="gate_core.SalesDispatchGateOut",
        pending_filter={"status__in": ["PHOTO_ATTACHED", "READY_FOR_GATEPASS"]},
        actor_field="printed_by",
        actor_date_field="printed_at",
        reference_field="entry_no",
        url_template="/gate/sales-dispatch/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="sd_commit",
        label="Commit the printed gate pass",
        module="Dispatch - Gate Out",
        permission="gate_core.can_commit_sales_dispatch_print",
        model="gate_core.SalesDispatchGateOut",
        pending_filter={"status": "GATEPASS_PRINTED"},
        actor_field="print_committed_by",
        actor_date_field="print_committed_at",
        reference_field="entry_no",
        age_field="printed_at",
        url_template="/gate/sales-dispatch/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="sd_dispatch",
        label="Release / dispatch the vehicle",
        module="Dispatch - Gate Out",
        permission="gate_core.can_dispatch_sales_dispatch_out",
        model="gate_core.SalesDispatchGateOut",
        pending_filter={"status": "PRINT_COMMITTED"},
        actor_field="dispatched_by",
        actor_date_field="dispatched_at",
        reference_field="entry_no",
        age_field="print_committed_at",
        url_template="/gate/sales-dispatch/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="dock_partial_approve",
        label="Approve the docking partial-scan request",
        module="Dispatch - Docking",
        permission="docking_admin.can_approve_docking_partial_scan",
        model="docking_admin.DockingPartialScanRequest",
        pending_filter={"status": "PENDING"},
        actor_field="reviewed_by",
        actor_date_field="reviewed_at",
        age_field="requested_at",
        overdue_after_days=0,
    ),
    ActivitySource(
        key="dock_skip_approve",
        label="Approve the docking scan-skip request",
        module="Dispatch - Docking",
        permission="docking_admin.can_approve_docking_scan_skip",
        model="docking_admin.DockingScanSkipRequest",
        pending_filter={"status": "PENDING"},
        actor_field="reviewed_by",
        actor_date_field="reviewed_at",
        age_field="requested_at",
        overdue_after_days=0,
    ),
]

# ---------------------------------------------------------------------------
# Barcode
# ---------------------------------------------------------------------------
_BARCODE = [
    ActivitySource(
        key="bc_scan",
        label="Scan units into the open dispatch session",
        module="Barcode",
        permission="barcode.can_scan_barcode_dispatch",
        model="barcode.DispatchSession",
        pending_filter={"status__in": ["DRAFT", "ACTIVE", "PARTIAL"]},
        reference_field="bill_number",
        age_field="started_at",
        url_template="/barcode/dispatch/summary/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bc_complete",
        label="Complete the dispatch session",
        module="Barcode",
        permission="barcode.can_complete_barcode_dispatch",
        model="barcode.DispatchSession",
        pending_filter={"status": "READY_TO_DISPATCH"},
        actor_field="dispatched_by",
        actor_date_field="dispatched_at",
        reference_field="bill_number",
        url_template="/barcode/dispatch/summary/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bc_close",
        label="Close the completed dispatch session",
        module="Barcode",
        permission="barcode.can_close_barcode_dispatch",
        model="barcode.DispatchSession",
        pending_filter={"status": "COMPLETED"},
        actor_field="closed_by",
        actor_date_field="closed_at",
        reference_field="bill_number",
        age_field="completed_at",
        url_template="/barcode/dispatch/summary/{id}",
        overdue_after_days=1,
    ),
    ActivitySource(
        key="bc_sap_retry",
        label="Retry the failed barcode SAP posting",
        module="Barcode",
        permission="barcode.can_retry_barcode_dispatch_sap",
        model="barcode.DispatchSession",
        pending_filter={"status": "SAP_SYNC_FAILED"},
        reference_field="bill_number",
        url_template="/barcode/dispatch/summary/{id}",
        overdue_after_days=0,
    ),
]


ACTIVITY_SOURCES: tuple = tuple(
    _WORK_ORDERS
    + _WORK_PERMITS
    + _INDENTS
    + _MAINT_OTHER
    + _QC
    + _PRODUCTION
    + _RETURNABLE
    + _WAREHOUSE
    + _DISPATCH
    + _BARCODE
)

SOURCES_BY_KEY = {source.key: source for source in ACTIVITY_SOURCES}


def sources_for_permissions(held: set) -> list:
    """Sources the holder of ``held`` (a set of ``app.codename``) is responsible for."""
    return [source for source in ACTIVITY_SOURCES if source.permission in held]


def all_permissions() -> set:
    """Every permission referenced by the registry — used to build the holder map."""
    return {source.permission for source in ACTIVITY_SOURCES}
