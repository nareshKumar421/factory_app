"""
Putting approved leave onto the attendance sheet, and taking it off again.

**The rule this module exists to respect:** ``machine_status`` is never
written. It is what the punching machine recorded, it is immutable by design,
and payroll disputes turn on being able to ask "what did the machine actually
say?" months later. An approved leave is an assertion by a human that
contradicts the machine -- exactly what ``effective_status`` is for.

So the projection does not touch attendance's tables directly. It calls
:func:`attendance.services.override_status`, the same door a manual correction
goes through, and inherits three things for free: the machine columns are left
alone, the mandatory reason is enforced, and an ``AttendanceOverrideLog`` row
is written. There is no second way to write that column, which is the point.

**The timing problem, which is the real work here.** Leave is approved in
advance; ``DailyAttendance`` rows do not exist until
``sync_biometric_attendance`` has run for that date. A projection at approval
time would therefore write nothing for the dates that matter most. So:

* a day whose attendance row exists is projected immediately;
* a day whose row does not yet exist is left with ``is_projected=False`` and
  picked up by :func:`project_range`, which is meant to run **after** each
  sync.

``is_projected`` lives on the day, not the request, precisely because a request
spanning a week is projected one date at a time as the sync reaches them.

**Why cancelling does not simply revert.** Between the approval and the
cancellation, HR may have corrected the same day for an unrelated reason. A
blind ``revert_to_machine`` would silently undo their work. So the reversal
only acts on a day still carrying *our* reason code, and reports the rest
rather than touching them.
"""

from django.db import transaction
from django.utils import timezone

from attendance.models import AttendanceStatus, DailyAttendance, OverrideReason
from attendance.services import OverrideRefused, override_status, revert_to_machine

from .constants import DayPortion, LeaveAction, LeaveDayStatus, LeaveRequestStatus
from .models import LeaveApproval, LeaveRequestDay


def status_for(portion):
    """What the sheet should read for one leave day.

    A full day is ``ON_LEAVE``. A half day stays ``HALF_DAY``, because the
    person genuinely was at the gate for half of it and the machine will have
    seen them -- calling that ``ON_LEAVE`` would contradict a punch that exists.
    """
    if portion in (DayPortion.FIRST_HALF, DayPortion.SECOND_HALF):
        return AttendanceStatus.HALF_DAY
    return AttendanceStatus.ON_LEAVE


def _reason_text(day):
    request = day.request
    return (
        f"{request.leave_type.name} approved "
        f"({request.from_date} to {request.to_date}, request #{request.pk})"
    )


@transaction.atomic
def project_day(day, *, user=None):
    """Write one approved leave day onto the sheet, if its row exists yet.

    Returns ``True`` when the sheet was changed, ``False`` when there is
    nothing to do -- no attendance row yet, or the day already says what we
    would have written. Never raises for the ordinary cases; a genuinely
    surprising refusal from attendance is allowed to propagate.
    """
    if day.status != LeaveDayStatus.APPROVED or day.is_projected:
        return False

    row = DailyAttendance.objects.filter(employee=day.employee, date=day.date).first()
    if row is None:
        # The sync has not reached this date. Left for project_range.
        return False

    wanted = status_for(day.portion)

    try:
        override_status(
            row,
            status=wanted,
            reason_code=OverrideReason.APPROVED_LEAVE,
            reason=_reason_text(day),
            user=user,
        )
    except OverrideRefused as exc:
        # The only expected refusal is "already recorded as X" -- somebody, or
        # an earlier run, already put the sheet where we wanted it. That is a
        # success for our purposes; mark it done rather than retrying forever.
        if "already recorded as" not in str(exc):
            raise

    day.is_projected = True
    day.projected_at = timezone.now()
    day.save(update_fields=["is_projected", "projected_at", "updated_at"])
    return True


def project_request(request, *, user=None):
    """Project every approved day of one request whose row exists.

    Called right after an approval so today's and any past dates land on the
    sheet immediately. Future dates are picked up later by :func:`project_range`.
    """
    if request.status != LeaveRequestStatus.APPROVED:
        return 0

    written = 0
    for day in request.days.filter(
        status=LeaveDayStatus.APPROVED, is_projected=False
    ).select_related("request__leave_type", "employee"):
        if project_day(day, user=user):
            written += 1

    if written:
        LeaveApproval.objects.create(
            request=request,
            action=LeaveAction.PROJECTED,
            comment=f"{written} day(s) written to the attendance sheet.",
            performed_by=user if user is not None and user.is_authenticated else None,
        )
    return written


def project_range(date_from, date_to, *, user=None, company=None):
    """Sweep every approved-but-unprojected day in a window onto the sheet.

    This is the half that makes future-dated leave work, and it is meant to run
    **after** ``sync_biometric_attendance``, which is what creates the rows it
    writes to. Idempotent: a day already projected is skipped, so re-running
    over any range is safe, exactly like the sync it follows.
    """
    days = (
        LeaveRequestDay.objects.filter(
            status=LeaveDayStatus.APPROVED,
            is_projected=False,
            date__gte=date_from,
            date__lte=date_to,
            request__status=LeaveRequestStatus.APPROVED,
        )
        .select_related("request__leave_type", "employee")
        .order_by("date", "employee_id")
    )
    if company is not None:
        days = days.filter(request__company=company)

    written = 0
    touched_requests = {}
    for day in days:
        if project_day(day, user=user):
            written += 1
            touched_requests[day.request_id] = day.request

    for request in touched_requests.values():
        LeaveApproval.objects.create(
            request=request,
            action=LeaveAction.PROJECTED,
            comment=f"Projected by the {date_from}..{date_to} sweep.",
            performed_by=user if user is not None and user.is_authenticated else None,
        )
    return written


@transaction.atomic
def unproject_request(request, *, user=None):
    """Take a cancelled request's days back off the sheet.

    Only reverses days still carrying our own reason code. A day somebody has
    since corrected for an unrelated reason is left exactly as they left it and
    counted as skipped -- silently undoing another person's correction is worse
    than leaving a stale one they can see and fix.

    Returns ``(reverted, skipped)``.
    """
    reverted = 0
    skipped = 0

    for day in request.days.filter(is_projected=True).select_related("employee"):
        row = DailyAttendance.objects.filter(
            employee=day.employee, date=day.date
        ).first()
        if row is None:
            skipped += 1
            continue

        if row.is_overridden and row.override_reason_code == OverrideReason.APPROVED_LEAVE:
            try:
                revert_to_machine(
                    row,
                    reason=f"Leave request #{request.pk} cancelled.",
                    user=user,
                )
                reverted += 1
            except OverrideRefused:
                skipped += 1
        else:
            # Somebody corrected this day for their own reason after we wrote
            # it. Not ours to undo.
            skipped += 1

        day.is_projected = False
        day.projected_at = None
        day.save(update_fields=["is_projected", "projected_at", "updated_at"])

    if reverted or skipped:
        LeaveApproval.objects.create(
            request=request,
            action=LeaveAction.UNPROJECTED,
            comment=f"{reverted} day(s) reverted, {skipped} left alone.",
            performed_by=user if user is not None and user.is_authenticated else None,
        )
    return reverted, skipped
