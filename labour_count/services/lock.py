"""
Timing/lock helpers for labour count sheets.

A sheet's lock and gate-verify deadlines come from a per-company
``LabourShiftWindow`` row, falling back to these IST code defaults:

    DAY   : lock 18:30, verify by 19:00 (same day)
    NIGHT : lock 06:30, verify by 09:00 (the morning AFTER work_date)
"""
import zoneinfo
from datetime import datetime, time, timedelta

from django.utils import timezone

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

DEFAULT_WINDOWS = {
    "DAY": {"submit_lock": time(18, 30), "verify_deadline": time(19, 0)},
    "NIGHT": {"submit_lock": time(6, 30), "verify_deadline": time(9, 0)},
}


def shift_times(company, shift):
    """Return (submit_lock_time, verify_deadline_time) for a company+shift."""
    from ..models import LabourShiftWindow

    window = LabourShiftWindow.objects.filter(company=company, shift=shift).first()
    if window:
        return window.submit_lock_time, window.verify_deadline_time
    defaults = DEFAULT_WINDOWS[shift]
    return defaults["submit_lock"], defaults["verify_deadline"]


def _target_date(work_date, shift):
    # NIGHT shift starts on work_date at 19:00 and ends the next morning,
    # so its lock/verify happen the day AFTER work_date.
    return work_date if shift == "DAY" else work_date + timedelta(days=1)


def compute_lock_at(company, work_date, shift):
    submit_lock, _ = shift_times(company, shift)
    return datetime.combine(_target_date(work_date, shift), submit_lock, tzinfo=IST)


def compute_verify_deadline(company, work_date, shift):
    _, verify_deadline = shift_times(company, shift)
    return datetime.combine(_target_date(work_date, shift), verify_deadline, tzinfo=IST)


def is_editable(sheet):
    """Department may edit counts only while DRAFT and before the lock time."""
    return sheet.status == "DRAFT" and timezone.now() < sheet.lock_at


def can_pull_back(sheet):
    """Department may pull a submission back to DRAFT only before the lock time."""
    return sheet.status == "SUBMITTED" and timezone.now() < sheet.lock_at
