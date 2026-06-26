import logging

from django.utils import timezone

from .models import LabourCountSheet, SheetStatus

logger = logging.getLogger(__name__)


def auto_submit_labour_sheets() -> int:
    """
    Lock & auto-submit any DRAFT sheet whose lock time has passed.

    Empty drafts (no counts, not a holiday) are left alone - there is nothing
    to submit and the time-based ``is_editable`` guard already blocks edits.
    Sheets with data (or marked holiday) flip to SUBMITTED so the gate sees them.
    """
    now = timezone.now()
    drafts = LabourCountSheet.objects.filter(
        status=SheetStatus.DRAFT, lock_at__lte=now
    ).prefetch_related("items")

    submitted = 0
    for sheet in drafts:
        if sheet.is_holiday or sheet.items.exists():
            sheet.status = SheetStatus.SUBMITTED
            sheet.submitted_at = now
            sheet.auto_submitted = True
            sheet.save(
                update_fields=["status", "submitted_at", "auto_submitted", "updated_at"]
            )
            submitted += 1

    logger.info("Labour auto-submit: %s sheet(s) locked & submitted at %s", submitted, now)
    return submitted
