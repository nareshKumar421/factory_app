import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from notifications.models import NotificationType
from notifications.services import NotificationService

from .enums import (
    ArrivalSlipStatus,
    FactoryHeadDecision,
    InspectionStatus,
    InspectionWorkflowStatus,
)

logger = logging.getLogger(__name__)

# Raw-material gate users should stay in the loop on everything that happens to
# their material in QC, so every QC notification is also delivered to this group
# (in addition to the primary recipient).
GATE_GROUP_NAME = "raw_material_gatein"


def _arrival_slip_url(slip):
    return f"/qc/arrival-slips/inspections/{slip.id}"


def _gate_arrival_slip_url(entry):
    return f"/gate/raw-materials/edit/{entry.id}/step4"


def _inspection_data(inspection, entry, **extra):
    data = {
        "reference_type": "inspection",
        "reference_id": str(inspection.id),
        "arrival_slip_id": str(inspection.arrival_slip_id or ""),
        "vehicle_entry_id": str(entry.id),
        "entry_no": entry.entry_no,
        "report_no": inspection.report_no,
        "workflow_status": inspection.workflow_status,
        "final_status": inspection.final_status,
    }
    data.update({key: str(value) for key, value in extra.items() if value is not None})
    return data


def _send_group_after_commit(
    *,
    group_name,
    title,
    body,
    notification_type,
    click_action_url,
    company,
    reference_type,
    reference_id,
    extra_data,
    created_by=None,
):
    # Always include the raw-material gate group so gate users are notified of
    # every QC event, not just sent-backs / vendor returns.
    group_names = [group_name]
    if group_name != GATE_GROUP_NAME:
        group_names.append(GATE_GROUP_NAME)

    transaction.on_commit(
        lambda: NotificationService.send_notification_by_auth_groups(
            group_names=group_names,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=click_action_url,
            reference_type=reference_type,
            reference_id=reference_id,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
        )
    )


def _send_permission_after_commit(
    *,
    permission_codename,
    title,
    body,
    notification_type,
    click_action_url,
    company,
    reference_type,
    reference_id,
    extra_data,
    created_by=None,
):
    def _dispatch():
        NotificationService.send_notification_by_permission(
            permission_codename=permission_codename,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=click_action_url,
            reference_type=reference_type,
            reference_id=reference_id,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
        )
        # Keep gate users informed of QC escalations as well.
        NotificationService.send_notification_by_auth_group(
            group_name=GATE_GROUP_NAME,
            title=title,
            body=body,
            notification_type=notification_type,
            click_action_url=click_action_url,
            reference_type=reference_type,
            reference_id=reference_id,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
        )

    transaction.on_commit(_dispatch)


@receiver(pre_save, sender="quality_control.MaterialArrivalSlip")
def capture_arrival_slip_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_notification_state = None
        return

    instance._previous_notification_state = (
        sender.objects.filter(pk=instance.pk)
        .values_list("status", "is_submitted", "sent_back_by_id")
        .first()
    )


@receiver(post_save, sender="quality_control.MaterialArrivalSlip")
def notify_arrival_slip_submitted(sender, instance, **kwargs):
    """When an arrival slip is submitted, notify QC store users once."""
    slip = instance
    previous = getattr(slip, "_previous_notification_state", None)
    was_submitted = bool(previous and previous[1])

    if not (slip.is_submitted and slip.status == ArrivalSlipStatus.SUBMITTED):
        return
    if was_submitted:
        return

    try:
        entry = slip.po_item_receipt.po_receipt.vehicle_entry
        _send_group_after_commit(
            group_name="qc_store",
            title="Arrival Slip Submitted",
            body=(
                f"Arrival slip for {slip.po_item_receipt.item_name} was submitted "
                f"for QC inspection. Entry: {entry.entry_no}."
            ),
            notification_type=NotificationType.ARRIVAL_SLIP_SUBMITTED,
            click_action_url="/qc/arrival-slips",
            company=entry.company,
            reference_type="arrival_slip",
            reference_id=slip.id,
            extra_data={
                "reference_type": "arrival_slip",
                "reference_id": str(slip.id),
                "vehicle_entry_id": str(entry.id),
                "entry_no": entry.entry_no,
                "po_item_receipt_id": str(slip.po_item_receipt_id),
            },
            created_by=slip.submitted_by,
        )
    except Exception as exc:
        logger.error("Failed to queue arrival-slip submitted notification: %s", exc)


@receiver(post_save, sender="quality_control.MaterialArrivalSlip")
def notify_arrival_slip_sent_back(sender, instance, **kwargs):
    """When QC sends an arrival slip back, notify raw material gate users."""
    slip = instance
    previous = getattr(slip, "_previous_notification_state", None)
    previous_status = previous[0] if previous else None
    previous_sent_back_by_id = previous[2] if previous else None

    if not (slip.status == ArrivalSlipStatus.DRAFT and slip.sent_back_by_id):
        return
    if previous_status == ArrivalSlipStatus.DRAFT and previous_sent_back_by_id == slip.sent_back_by_id:
        return

    try:
        entry = slip.po_item_receipt.po_receipt.vehicle_entry
        _send_group_after_commit(
            group_name="raw_material_gatein",
            title="Arrival Slip Sent Back for Correction",
            body=(
                f"Arrival slip for {slip.po_item_receipt.item_name} was sent back "
                f"for correction. Entry: {entry.entry_no}. Remarks: {slip.remarks or 'N/A'}"
            ),
            notification_type=NotificationType.ARRIVAL_SLIP_SENT_BACK,
            click_action_url=_gate_arrival_slip_url(entry),
            company=entry.company,
            reference_type="arrival_slip",
            reference_id=slip.id,
            extra_data={
                "reference_type": "arrival_slip",
                "reference_id": str(slip.id),
                "vehicle_entry_id": str(entry.id),
                "entry_no": entry.entry_no,
                "remarks": slip.remarks or "",
            },
            created_by=slip.sent_back_by,
        )
    except Exception as exc:
        logger.error("Failed to queue arrival-slip sent-back notification: %s", exc)


@receiver(pre_save, sender="quality_control.RawMaterialInspection")
def capture_inspection_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_notification_state = None
        return

    instance._previous_notification_state = (
        sender.objects.filter(pk=instance.pk)
        .values_list("workflow_status", "final_status", "factory_head_decision")
        .first()
    )


def _notify_factory_head_decision_required(inspection, entry):
    _send_permission_after_commit(
        permission_codename="can_factory_head_decision",
        title="Factory Head Decision Required",
        body=(
            f"QC rejected {inspection.description_of_material} "
            f"({inspection.report_no}). Factory Head decision is required."
        ),
        notification_type=NotificationType.FACTORY_HEAD_DECISION_REQUIRED,
        click_action_url=_arrival_slip_url(inspection.arrival_slip),
        company=entry.company,
        reference_type="inspection",
        reference_id=inspection.id,
        extra_data=_inspection_data(inspection, entry),
        created_by=inspection.updated_by,
    )


def _notify_factory_head_decision_recorded(inspection, entry):
    route = _arrival_slip_url(inspection.arrival_slip)
    group_name = "qc_store"
    if inspection.factory_head_decision == FactoryHeadDecision.RETURN_TO_VENDOR:
        route = "/gate/rejected-qc-return"
        group_name = "raw_material_gatein"

    _send_group_after_commit(
        group_name=group_name,
        title="Factory Head Decision Recorded",
        body=(
            f"Factory Head decision for {inspection.description_of_material} "
            f"({inspection.report_no}): {inspection.get_factory_head_decision_display()}."
        ),
        notification_type=NotificationType.FACTORY_HEAD_DECISION_RECORDED,
        click_action_url=route,
        company=entry.company,
        reference_type="inspection",
        reference_id=inspection.id,
        extra_data=_inspection_data(
            inspection,
            entry,
            factory_head_decision=inspection.factory_head_decision,
        ),
        created_by=inspection.factory_head or inspection.updated_by,
    )


def _notify_rejected(inspection, entry):
    _send_group_after_commit(
        group_name="qc_store",
        title="QC Inspection Rejected",
        body=(
            f"Inspection for {inspection.description_of_material} "
            f"({inspection.report_no}) was rejected. Remarks: {inspection.remarks or 'N/A'}"
        ),
        notification_type=NotificationType.QC_REJECTED,
        click_action_url=_arrival_slip_url(inspection.arrival_slip),
        company=entry.company,
        reference_type="inspection",
        reference_id=inspection.id,
        extra_data=_inspection_data(inspection, entry),
        created_by=inspection.updated_by,
    )
    _notify_factory_head_decision_required(inspection, entry)


@receiver(post_save, sender="quality_control.RawMaterialInspection")
def notify_inspection_workflow(sender, instance, **kwargs):
    inspection = instance
    previous = getattr(inspection, "_previous_notification_state", None)
    previous_workflow = previous[0] if previous else None
    previous_final_status = previous[1] if previous else None
    previous_factory_decision = previous[2] if previous else ""

    workflow_changed = previous_workflow != inspection.workflow_status
    final_status_changed = previous_final_status != inspection.final_status
    factory_decision_changed = (
        previous_factory_decision != inspection.factory_head_decision
        and bool(inspection.factory_head_decision)
    )

    if not workflow_changed and not final_status_changed and not factory_decision_changed:
        return

    try:
        entry = inspection.vehicle_entry

        if factory_decision_changed:
            _notify_factory_head_decision_recorded(inspection, entry)

        if not workflow_changed and not final_status_changed:
            return

        workflow = inspection.workflow_status
        if workflow == InspectionWorkflowStatus.SUBMITTED:
            _send_group_after_commit(
                group_name="qc_chemist",
                title="QC Inspection Awaiting Approval",
                body=(
                    f"Inspection for {inspection.description_of_material} "
                    f"({inspection.report_no}) is awaiting chemist approval."
                ),
                notification_type=NotificationType.QC_INSPECTION_SUBMITTED,
                click_action_url="/qc/arrival-slips/approvals",
                company=entry.company,
                reference_type="inspection",
                reference_id=inspection.id,
                extra_data=_inspection_data(inspection, entry),
                created_by=inspection.updated_by,
            )
            return

        if workflow == InspectionWorkflowStatus.QA_CHEMIST_APPROVED:
            _send_group_after_commit(
                group_name="qc_manager",
                title="QC Chemist Approved - Awaiting QAM",
                body=(
                    f"Inspection for {inspection.description_of_material} "
                    f"({inspection.report_no}) was approved by QA Chemist."
                ),
                notification_type=NotificationType.QC_CHEMIST_APPROVED,
                click_action_url="/qc/arrival-slips/approvals",
                company=entry.company,
                reference_type="inspection",
                reference_id=inspection.id,
                extra_data=_inspection_data(inspection, entry),
                created_by=inspection.updated_by,
            )
            return

        if workflow == InspectionWorkflowStatus.REJECTED:
            _notify_rejected(inspection, entry)
            return

        if workflow != InspectionWorkflowStatus.QAM_APPROVED:
            return

        if inspection.final_status == InspectionStatus.ACCEPTED:
            _send_group_after_commit(
                group_name="grpo",
                title="QC Approved - Ready for GRPO",
                body=(
                    f"Inspection for {inspection.description_of_material} "
                    f"({inspection.report_no}) was approved by QAM."
                ),
                notification_type=NotificationType.QC_QAM_APPROVED,
                click_action_url=f"/grpo/material/preview/{entry.id}",
                company=entry.company,
                reference_type="inspection",
                reference_id=inspection.id,
                extra_data=_inspection_data(inspection, entry),
                created_by=inspection.updated_by,
            )
        elif inspection.final_status == InspectionStatus.HOLD:
            _send_group_after_commit(
                group_name="qc_store",
                title="QC Inspection On Hold",
                body=(
                    f"Inspection for {inspection.description_of_material} "
                    f"({inspection.report_no}) was put on hold by QAM."
                ),
                notification_type=NotificationType.QC_HOLD,
                click_action_url=_arrival_slip_url(inspection.arrival_slip),
                company=entry.company,
                reference_type="inspection",
                reference_id=inspection.id,
                extra_data=_inspection_data(inspection, entry),
                created_by=inspection.updated_by,
            )
        elif inspection.final_status == InspectionStatus.REJECTED:
            _notify_rejected(inspection, entry)
    except Exception as exc:
        logger.error("Failed to queue inspection notification: %s", exc)
