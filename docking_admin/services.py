import logging

from gate_core.serializers_sales_dispatch import user_display_name
from notifications.models import NotificationType
from notifications.services import NotificationService

logger = logging.getLogger(__name__)

# Frontend routes (see FactoryFlow admin + dispatch modules).
APPROVALS_URL = "/admin/docking/scan-approvals"
PARTIAL_APPROVALS_URL = "/admin/docking/partial-dispatch-approvals"

# Codename only — NotificationService matches on the bare permission codename.
PERM_APPROVE_CODENAME = "can_approve_docking_scan_skip"
PERM_APPROVE_PARTIAL_CODENAME = "can_approve_docking_partial_scan"


def _entry_label(skip_request):
    entry = skip_request.sales_dispatch
    return getattr(entry, "entry_no", "") or f"#{skip_request.sales_dispatch_id}"


def _scan_page_url(skip_request):
    vehicle_entry_id = getattr(skip_request.sales_dispatch, "vehicle_entry_id", None)
    if not vehicle_entry_id:
        return APPROVALS_URL
    return f"/dispatch/docking/new/barcode-scan?entryId={vehicle_entry_id}"


def notify_approvers_of_new_request(skip_request):
    """Notify users who can approve scan skips that a new request needs review."""
    requester = user_display_name(skip_request.requested_by) or "An operator"
    try:
        NotificationService.send_notification_by_permission(
            permission_codename=PERM_APPROVE_CODENAME,
            title="Docking scan skip requested",
            body=(
                f"{requester} requested to skip box scanning for Docking "
                f"{_entry_label(skip_request)}. Reason: {skip_request.reason}"
            ),
            notification_type=NotificationType.DOCKING_SCAN_SKIP_REQUESTED,
            click_action_url=APPROVALS_URL,
            company=skip_request.company,
            extra_data={
                "scan_skip_request_id": skip_request.id,
                "sales_dispatch_id": skip_request.sales_dispatch_id,
            },
            created_by=skip_request.requested_by,
        )
    except Exception as exc:  # best-effort: never block the request
        logger.error(
            f"Failed to notify approvers of scan skip request {skip_request.id}: {exc}"
        )


def notify_requester_of_review(skip_request):
    """Tell the operator their skip request was approved or rejected."""
    recipient = skip_request.requested_by
    if not recipient:
        return

    approved = skip_request.status == "APPROVED"
    decision = "approved" if approved else "rejected"
    if approved:
        body = (
            f"Your scan skip for Docking {_entry_label(skip_request)} was approved. "
            "You can continue without scanning boxes."
        )
    else:
        note = skip_request.review_notes or "No reason provided."
        body = (
            f"Your scan skip for Docking {_entry_label(skip_request)} was rejected. "
            f"Please scan boxes to continue. Reason: {note}"
        )

    try:
        NotificationService.send_notification_to_user(
            user=recipient,
            title=f"Scan skip {decision}",
            body=body,
            notification_type=NotificationType.DOCKING_SCAN_SKIP_REVIEWED,
            click_action_url=_scan_page_url(skip_request),
            reference_type="docking_scan_skip_request",
            reference_id=skip_request.id,
            company=skip_request.company,
            created_by=skip_request.reviewed_by,
        )
    except Exception as exc:  # best-effort: never block the review
        logger.error(
            f"Failed to notify requester of scan skip review {skip_request.id}: {exc}"
        )


def notify_approvers_of_new_partial_request(partial_request):
    """Notify approvers that a new partial-dispatch (partial box scan) request needs review."""
    requester = user_display_name(partial_request.requested_by) or "An operator"
    try:
        NotificationService.send_notification_by_permission(
            permission_codename=PERM_APPROVE_PARTIAL_CODENAME,
            title="Partial dispatch approval requested",
            body=(
                f"{requester} requested to dispatch Docking {_entry_label(partial_request)} "
                f"with a partial box scan ({partial_request.scanned_boxes} of "
                f"{partial_request.expected_boxes}). Reason: {partial_request.reason}"
            ),
            notification_type=NotificationType.DOCKING_SCAN_SKIP_REQUESTED,
            click_action_url=PARTIAL_APPROVALS_URL,
            company=partial_request.company,
            extra_data={
                "partial_scan_request_id": partial_request.id,
                "sales_dispatch_id": partial_request.sales_dispatch_id,
            },
            created_by=partial_request.requested_by,
        )
    except Exception as exc:  # best-effort: never block the request
        logger.error(
            f"Failed to notify approvers of partial scan request {partial_request.id}: {exc}"
        )


def notify_requester_of_partial_review(partial_request):
    """Tell the operator their partial-dispatch request was approved or rejected."""
    recipient = partial_request.requested_by
    if not recipient:
        return

    approved = partial_request.status == "APPROVED"
    decision = "approved" if approved else "rejected"
    if approved:
        body = (
            f"Your partial dispatch for Docking {_entry_label(partial_request)} was approved. "
            "You can continue to gatepass with the boxes scanned so far."
        )
    else:
        note = partial_request.review_notes or "No reason provided."
        body = (
            f"Your partial dispatch for Docking {_entry_label(partial_request)} was rejected. "
            f"Please scan the remaining boxes to continue. Reason: {note}"
        )

    try:
        NotificationService.send_notification_to_user(
            user=recipient,
            title=f"Partial dispatch {decision}",
            body=body,
            notification_type=NotificationType.DOCKING_SCAN_SKIP_REVIEWED,
            click_action_url=_scan_page_url(partial_request),
            reference_type="docking_partial_scan_request",
            reference_id=partial_request.id,
            company=partial_request.company,
            created_by=partial_request.reviewed_by,
        )
    except Exception as exc:  # best-effort: never block the review
        logger.error(
            f"Failed to notify requester of partial scan review {partial_request.id}: {exc}"
        )
