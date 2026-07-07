"""
maintenance/jobs.py

Background job that expires work permits whose validity window has passed before
completion, and notifies the Fire Department Head (users with
``can_approve_work_permit``) plus the maintenance submitter.

Called both by the ``expire_work_permits`` management command (manual/cron) and by
the APScheduler loop started via ``run_work_permit_scheduler`` (automatic).
"""

import logging

from django.utils import timezone

from notifications.models import NotificationType
from notifications.services import NotificationService

from .constants import WorkPermitStatus
from .models import WorkPermit

logger = logging.getLogger(__name__)


def expire_lapsed_work_permits() -> int:
    """Mark every lapsed, still-live work permit EXPIRED and notify. Returns count."""
    today = timezone.localdate()
    candidates = WorkPermit.objects.filter(
        status__in=list(WorkPermit.EXPIRABLE_STATUSES),
        is_active=True,
        valid_date__lte=today,
    ).select_related("company", "submitted_by")

    expired_count = 0
    for permit in candidates:
        if not permit.is_expired_now:
            continue

        permit.status = WorkPermitStatus.EXPIRED
        permit.expired_at = timezone.now()
        permit.save(update_fields=["status", "expired_at", "updated_at"])
        expired_count += 1

        title = "Work permit expired"
        body = (
            f"Permit {permit.serial_no} for {permit.job_location} has expired "
            f"before the work was completed."
        )
        try:
            # Notify the Fire Department Head(s).
            NotificationService.send_notification_by_permission(
                permission_codename="can_approve_work_permit",
                title=title,
                body=body,
                notification_type=NotificationType.WORK_PERMIT_EXPIRED,
                reference_type="work_permit",
                reference_id=permit.id,
                company=permit.company,
            )
            # Notify the maintenance submitter so they can renew and resubmit.
            if permit.submitted_by_id:
                NotificationService.send_notification_to_user(
                    user=permit.submitted_by,
                    title=title,
                    body=f"{body} Renew the permit to continue the job.",
                    notification_type=NotificationType.WORK_PERMIT_EXPIRED,
                    reference_type="work_permit",
                    reference_id=permit.id,
                    company=permit.company,
                )
        except Exception as exc:  # notifications must never block expiry
            logger.error(
                "[WorkPermit] Failed to notify for expired permit %s: %s",
                permit.serial_no,
                exc,
                exc_info=True,
            )

    logger.info("[WorkPermit] Expired %s work permit(s).", expired_count)
    return expired_count
