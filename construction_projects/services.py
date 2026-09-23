"""
Business logic for construction projects.

Views stay thin: they authenticate, deserialise, and call one function in here.
Every rule in ``docs/construction_projects/README.md`` section 3 lives in this
module and nowhere else.

Kept as one module rather than a ``services/`` package on purpose -- six tables
do not need five files, and the plan's guiding instruction was to keep it
simple.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.exceptions import APIException

from .constants import (
    ACTIVE_STATUSES,
    CLOSED_STATUSES,
    DEFAULT_BACKDATE_DAYS,
    EDITABLE_BATCH_STATUSES,
    EDITABLE_STATUSES,
    ExpenseBatchStatus,
    ProjectStatus,
    RevisionStatus,
)
from .models import (
    DailyLog,
    EstimateLine,
    ExpenseBatch,
    DailyLogStopReason,
    Expense,
    Project,
    ProjectAttachment,
    ProjectRevision,
    ProjectSequence,
)

ZERO = Decimal("0.00")


class ConstructionError(APIException):
    """A rule was broken. Renders as the module's one error shape:

        {"detail": "...", "code": "stable_slug", "context": {...}}

    ``code`` is stable and testable; ``detail`` is for humans and may be
    reworded; ``context`` carries the numbers so the UI can show the arithmetic
    rather than just the verdict.
    """

    status_code = 400

    def __init__(self, detail, code, context=None):
        super().__init__({"detail": detail, "code": code, "context": context or {}})


# ---------------------------------------------------------------------------
# Roll-ups
# ---------------------------------------------------------------------------


def recompute_totals(project, *, save=True):
    """Refresh the three denormalised figures on a project from its rows.

    Called from every place that changes money or progress. Nothing decides
    anything from these fields -- they exist so a list of 40 projects is one
    query instead of 120.
    """
    approved_extra = project.revisions.filter(
        status=RevisionStatus.APPROVED, is_active=True
    ).aggregate(total=Sum("additional_amount"))["total"] or ZERO

    # The budget is sanctioned at approval, not at creation. Keyed on
    # sanctioned_at rather than status, so a later hold, completion or
    # cancellation does not un-sanction money that was genuinely granted --
    # and a draft that was cancelled never becomes sanctioned by accident.
    if project.is_sanctioned:
        project.sanctioned_budget = project.estimated_cost + approved_extra
    else:
        project.sanctioned_budget = ZERO

    # Every recorded line counts, approved or not. The cash left the box, and a
    # project sitting on an unreviewed batch must not read as under budget.
    project.spent_amount = project.expenses.filter(is_active=True).aggregate(
        total=Sum("amount")
    )["total"] or ZERO

    latest = (
        project.daily_logs.filter(is_active=True, progress_percent__isnull=False)
        .order_by("-log_date", "-id")
        .values_list("progress_percent", flat=True)
        .first()
    )
    project.progress_percent = latest if latest is not None else ZERO

    if save:
        project.save(
            update_fields=[
                "sanctioned_budget",
                "spent_amount",
                "progress_percent",
                "updated_at",
            ]
        )
    return project


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def visible_projects(user, company):
    """Projects this caller may see.

    Applied in get_queryset() on every view, never in a serializer -- a filter
    that lives in a serializer is one .values() call away from leaking.
    """
    qs = Project.objects.filter(company=company, is_active=True)
    if user.has_perm("construction_projects.can_view_all_projects"):
        return qs
    return qs.filter(Q(manager=user) | Q(site_incharge=user))


@transaction.atomic
def create_project(*, company, user, **data):
    project = Project(company=company, created_by=user, updated_by=user, **data)
    # Validate before taking a number, rather than relying on the rollback to
    # give it back.
    _validate_dates(project)
    project.code = ProjectSequence.next_code(company)
    project.save()
    return project


def update_project(project, *, user, **data):
    if not project.is_editable:
        raise ConstructionError(
            "This project has been approved. Its budget and dates change through "
            "a revision, not an edit.",
            "project_not_editable",
            {"status": project.status},
        )
    breakdown = estimate_total(project)
    if breakdown is not None and "estimated_cost" in data:
        # The breakdown is the estimate; a typed figure beside it would be a
        # second answer to the same question.
        data.pop("estimated_cost")

    for field, value in data.items():
        setattr(project, field, value)
    project.updated_by = user
    _validate_dates(project)
    project.save()
    return project


@transaction.atomic
def save_estimate_lines(project, *, user, lines):
    """Replace the estimate's breakdown, and re-derive the estimate from it.

    Saved wholesale rather than line by line because this is a table: somebody
    pastes twenty rows from a spreadsheet, renumbers two and deletes one, then
    saves once.

    An empty list clears the breakdown and hands ``estimated_cost`` back to
    being typed directly.
    """
    if not project.is_editable:
        raise ConstructionError(
            "This project has been approved. Its estimate changes through a "
            "revision, not an edit.",
            "project_not_editable",
            {"status": project.status},
        )

    project.estimate_lines.all().delete()
    created = [
        EstimateLine(
            project=project,
            line_no=row.get("line_no") or index + 1,
            material=row["material"],
            quantity=row.get("quantity") or Decimal("0"),
            unit=row.get("unit", ""),
            rate=row.get("rate") or ZERO,
            notes=row.get("notes", ""),
            created_by=user,
            updated_by=user,
        )
        for index, row in enumerate(lines)
    ]
    for line in created:
        # bulk_create skips save(), and amount is derived there.
        line.amount = (line.quantity * line.rate).quantize(Decimal("0.01"))
    EstimateLine.objects.bulk_create(created)

    sync_estimated_cost(project, user=user)
    return created


def sync_estimated_cost(project, *, user=None):
    """When a breakdown exists, the lines ARE the estimate.

    Two numbers that are supposed to agree eventually will not, and nobody can
    then say which one was sanctioned. With no lines, ``estimated_cost`` is
    whatever was typed and is left alone.
    """
    total = project.estimate_lines.filter(is_active=True).aggregate(
        total=Sum("amount")
    )["total"]
    if total is None:
        return project
    project.estimated_cost = total
    if user is not None:
        project.updated_by = user
    project.save(update_fields=["estimated_cost", "updated_by", "updated_at"])
    return project


def estimate_total(project):
    """The breakdown's total, or None when there is no breakdown."""
    return project.estimate_lines.filter(is_active=True).aggregate(
        total=Sum("amount")
    )["total"]


def _validate_dates(project):
    # A draft may hold one date, or neither. Nothing can be said about the
    # order of dates that are not both there yet, and saying it at submit time
    # is the point of a draft.
    if project.start_date is None or project.expected_end_date is None:
        return
    if project.expected_end_date < project.start_date:
        raise ConstructionError(
            "The expected end date cannot be before the start date.",
            "end_before_start",
            {
                "start_date": project.start_date,
                "expected_end_date": project.expected_end_date,
            },
        )


#: What a project needs before anybody can be asked to approve it. These are
#: nullable columns so that a half-filled draft can be parked, which moves the
#: requirement here -- to the moment the project stops being one person's
#: notes and becomes a request someone else has to answer.
REQUIRED_TO_SUBMIT = (
    ("name", "what is being built"),
    ("start_date", "when it starts"),
    ("expected_end_date", "when it is expected to finish"),
    ("estimated_cost", "the budget being asked for"),
    ("manager", "who runs it"),
)


def missing_before_submit(project):
    """The human names of the fields still to be filled in, in form order."""
    return [
        label
        for field, label in REQUIRED_TO_SUBMIT
        if getattr(project, f"{field}_id" if field == "manager" else field) in (None, "")
    ]


def submit_project(project, *, user):
    if project.status not in EDITABLE_STATUSES:
        raise ConstructionError(
            "Only a draft or a rejected project can be submitted.",
            "not_submittable",
            {"status": project.status},
        )
    missing = missing_before_submit(project)
    if missing:
        raise ConstructionError(
            "This project is not finished yet. Still needed: "
            + ", ".join(missing)
            + ".",
            "incomplete",
            {"missing": missing},
        )
    _validate_dates(project)
    project.status = ProjectStatus.PENDING_APPROVAL
    project.submitted_at = timezone.now()
    project.submitted_by = user
    project.updated_by = user
    project.save(
        update_fields=["status", "submitted_at", "submitted_by", "updated_by", "updated_at"]
    )
    return project


@transaction.atomic
def approve_project(project, *, user, note=""):
    if project.status != ProjectStatus.PENDING_APPROVAL:
        raise ConstructionError(
            "Only a project waiting for approval can be approved.",
            "not_pending_approval",
            {"status": project.status},
        )
    now = timezone.now()
    project.status = ProjectStatus.APPROVED
    project.decided_at = now
    project.decided_by = user
    project.sanctioned_at = now
    project.decision_note = note
    project.updated_by = user
    project.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "sanctioned_at",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return recompute_totals(project)


def reject_project(project, *, user, note=""):
    if project.status != ProjectStatus.PENDING_APPROVAL:
        raise ConstructionError(
            "Only a project waiting for approval can be rejected.",
            "not_pending_approval",
            {"status": project.status},
        )
    _assert_reason_given(note)
    project.status = ProjectStatus.REJECTED
    project.decided_at = timezone.now()
    project.decided_by = user
    project.decision_note = note
    project.updated_by = user
    # sanctioned_at is deliberately left alone: nothing was sanctioned.
    project.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return project


def hold_project(project, *, user, note=""):
    if project.status not in {ProjectStatus.APPROVED, ProjectStatus.IN_PROGRESS}:
        raise ConstructionError(
            "Only a live project can be put on hold.",
            "not_holdable",
            {"status": project.status},
        )
    project.status = ProjectStatus.ON_HOLD
    project.decision_note = note
    project.updated_by = user
    project.save(update_fields=["status", "decision_note", "updated_by", "updated_at"])
    return project


def resume_project(project, *, user):
    if project.status != ProjectStatus.ON_HOLD:
        raise ConstructionError(
            "Only a project on hold can be resumed.",
            "not_on_hold",
            {"status": project.status},
        )
    project.status = (
        ProjectStatus.IN_PROGRESS
        if project.daily_logs.filter(is_active=True).exists()
        else ProjectStatus.APPROVED
    )
    project.updated_by = user
    project.save(update_fields=["status", "updated_by", "updated_at"])
    return project


def complete_project(project, *, user, actual_end_date=None, note=""):
    if project.status not in ACTIVE_STATUSES:
        raise ConstructionError(
            "Only a live project can be completed.",
            "not_completable",
            {"status": project.status},
        )
    project.status = ProjectStatus.COMPLETED
    project.actual_end_date = actual_end_date or timezone.localdate()
    project.decision_note = note
    project.updated_by = user
    project.save(
        update_fields=[
            "status",
            "actual_end_date",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return project


def cancel_project(project, *, user, note=""):
    if project.is_closed:
        raise ConstructionError(
            "This project is already closed.", "already_closed", {"status": project.status}
        )
    # Money was spent; the project happened. It completes or it stays on hold,
    # but it did not un-happen.
    if project.expenses.filter(is_active=True).exists():
        raise ConstructionError(
            "Money has already been spent on this project, so it cannot be "
            "cancelled. Complete it instead.",
            "cancel_with_spend",
            {"spent_amount": project.spent_amount},
        )
    project.status = ProjectStatus.CANCELLED
    project.decision_note = note
    project.updated_by = user
    project.save(update_fields=["status", "decision_note", "updated_by", "updated_at"])
    return project


def _assert_writable(project):
    """A project must be live before anything is logged or spent against it."""
    if project.is_closed:
        raise ConstructionError(
            "This project is closed.", "project_closed", {"status": project.status}
        )
    if not project.is_live:
        raise ConstructionError(
            "This project has not been approved yet.",
            "project_not_approved",
            {"status": project.status},
        )


# ---------------------------------------------------------------------------
# The daily loop
# ---------------------------------------------------------------------------


def _assert_log_date_allowed(project, log_date, user):
    today = timezone.localdate()
    if log_date > today:
        raise ConstructionError(
            "A day cannot be logged before it has happened.",
            "log_date_in_future",
            {"log_date": log_date, "today": today},
        )
    if log_date < project.start_date:
        raise ConstructionError(
            "That date is before the project started.",
            "log_date_before_start",
            {"log_date": log_date, "start_date": project.start_date},
        )
    limit = today - timedelta(days=DEFAULT_BACKDATE_DAYS)
    if log_date < limit and not user.has_perm("construction_projects.can_edit_project"):
        raise ConstructionError(
            f"A log can only be back-dated {DEFAULT_BACKDATE_DAYS} days.",
            "log_too_old",
            {"log_date": log_date, "earliest": limit},
        )


def _assert_progress_forward(project, log_date, progress, user, *, exclude_id=None):
    """Work does not un-happen.

    A typo that drops a project from 60% to 6% otherwise sits there until
    somebody notices the chart. Compared against earlier logs only, so a
    back-dated entry is judged against what was true before it.
    """
    if progress is None:
        return
    if user.has_perm("construction_projects.can_edit_project"):
        return
    qs = project.daily_logs.filter(
        is_active=True, progress_percent__isnull=False, log_date__lt=log_date
    )
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    highest = qs.aggregate(top=Max("progress_percent"))["top"]
    if highest is not None and progress < highest:
        raise ConstructionError(
            f"Progress was already {highest}% on an earlier day. It cannot go "
            "backwards.",
            "progress_went_backwards",
            {"recorded": progress, "previous_highest": highest},
        )


def _set_stop_reasons(log, reasons):
    """Replace a day's stop reasons with the set given.

    ``None`` means the caller did not mention them and the existing ones stand;
    an empty list means "no longer stopped" and clears them. A day that is not
    stopped never keeps reasons, whatever was sent.
    """
    if reasons is None:
        if not log.work_stopped:
            log.stop_reasons.all().delete()
        return
    wanted = set(reasons) if log.work_stopped else set()
    log.stop_reasons.exclude(reason__in=wanted).delete()
    have = set(log.stop_reasons.values_list("reason", flat=True))
    DailyLogStopReason.objects.bulk_create(
        [DailyLogStopReason(daily_log=log, reason=reason) for reason in wanted - have],
        ignore_conflicts=True,
    )


@transaction.atomic
def save_daily_log(project, *, user, log_data):
    """Create or update the day's log, with its expenses, in one transaction.

    The day's WORK only. Spend used to be part of this form and was moved out:
    payments are approved in batches of their own, on their own screen, and
    burying them in the diary made a page that half the site could not submit.

    Re-posting a date that already has a log updates it rather than erroring:
    the site in-charge who remembers something at 9pm should not have to hunt
    for yesterday's row.
    """
    _assert_writable(project)
    log_date = log_data["log_date"]
    _assert_log_date_allowed(project, log_date, user)

    existing = project.daily_logs.filter(log_date=log_date).first()
    _assert_progress_forward(
        project,
        log_date,
        log_data.get("progress_percent"),
        user,
        exclude_id=existing.id if existing else None,
    )

    # The reasons live in a child table, so they are not a field on the log.
    reasons = log_data.pop("stopped_reasons", None)

    if existing:
        for field, value in log_data.items():
            setattr(existing, field, value)
        existing.is_active = True
        existing.updated_by = user
        existing.save()
        log = existing
    else:
        log = DailyLog.objects.create(
            project=project, created_by=user, updated_by=user, **log_data
        )

    _set_stop_reasons(log, reasons)

    # The first log is what starting actually is.
    if project.status == ProjectStatus.APPROVED:
        project.status = ProjectStatus.IN_PROGRESS
        project.save(update_fields=["status", "updated_at"])

    recompute_totals(project)
    return log, budget_warning(project)


def record_expense(project, *, user, **data):
    """Record one spend. Returns (expense, warning-or-None).

    An expense that takes the project past its budget is SAVED, not refused --
    the money is already gone, and refusing to record it only makes the books
    wrong while the shed still gets built. The overrun comes back as a warning
    the UI turns into a "Request more budget" button.
    """
    _assert_writable(project)
    spend_date = data["spend_date"]
    today = timezone.localdate()
    if spend_date > today:
        raise ConstructionError(
            "Spend cannot be dated in the future.",
            "spend_date_in_future",
            {"spend_date": spend_date, "today": today},
        )
    if spend_date < project.start_date:
        raise ConstructionError(
            "That date is before the project started.",
            "spend_date_before_start",
            {"spend_date": spend_date, "start_date": project.start_date},
        )

    expense = Expense.objects.create(
        project=project,
        batch=current_batch(project, user=user),
        created_by=user,
        updated_by=user,
        **data,
    )
    recompute_totals(project)
    return expense, budget_warning(project)


def current_batch(project, *, user=None, create=True):
    """The batch a new line joins: the project's OPEN or RETURNED one.

    Created on demand, so nobody has to open a claim before recording what they
    spent. A partial unique constraint keeps there being only ever one.
    """
    batch = project.expense_batches.filter(
        is_active=True, status__in=EDITABLE_BATCH_STATUSES
    ).first()
    if batch or not create:
        return batch
    last_no = (
        project.expense_batches.order_by("-batch_no")
        .values_list("batch_no", flat=True)
        .first()
        or 0
    )
    return ExpenseBatch.objects.create(
        project=project,
        batch_no=last_no + 1,
        created_by=user,
        updated_by=user,
    )


def _assert_batch_editable(batch):
    if not batch.is_editable:
        raise ConstructionError(
            "This batch is with the approver. Its lines cannot be changed."
            if batch.status == ExpenseBatchStatus.SUBMITTED
            else "This batch has been approved and is settled.",
            "batch_not_editable",
            {"status": batch.status, "batch_no": batch.batch_no},
        )


@transaction.atomic
def submit_expense_batch(project, *, user, expense_ids=None):
    """Send payments for approval.

    ``expense_ids`` picks which of the open batch's payments go: a site often
    has one bill it is still chasing and does not want to hold the rest back
    for. ``None`` sends everything, which is the common case.

    A partial send keeps the batch being sent as it is -- same number, same
    identity -- and moves what is left into a NEW open batch. The alternative,
    putting the sent ones in a new batch, would leave the open batch with a
    lower number than the claim made after it, so the numbers would no longer
    read in the order the claims were made.
    """
    batch = current_batch(project, create=False)
    if batch is None or batch.line_count == 0:
        raise ConstructionError(
            "There is nothing to send for approval.", "batch_is_empty", {}
        )

    held_back = []
    if expense_ids is not None:
        chosen = set(
            batch.expenses.filter(id__in=expense_ids, is_active=True).values_list(
                "id", flat=True
            )
        )
        if not chosen:
            raise ConstructionError(
                "Pick at least one payment to send.", "no_payments_chosen", {}
            )
        held_back = list(batch.expenses.filter(is_active=True).exclude(id__in=chosen))

    batch.status = ExpenseBatchStatus.SUBMITTED
    batch.submitted_at = timezone.now()
    batch.submitted_by = user
    batch.updated_by = user
    batch.save(
        update_fields=[
            "status",
            "submitted_at",
            "submitted_by",
            "updated_by",
            "updated_at",
        ]
    )

    if held_back:
        # Only now: a second OPEN batch alongside the first would break the
        # one-open-batch constraint, so the first has to stop being open.
        next_batch = current_batch(project, user=user)
        Expense.objects.filter(id__in=[row.id for row in held_back]).update(
            batch=next_batch
        )

    return batch


def decide_expense_batch(batch, *, user, decision, note=""):
    """Approve the whole batch, or send it back.

    Approving approves every line in it -- that is the point of the batch. A
    return is not a disallowance: nothing is un-spent, the batch simply goes
    back to the site to be corrected and sent again.
    """
    if batch.status != ExpenseBatchStatus.SUBMITTED:
        raise ConstructionError(
            "Only a batch that has been sent for approval can be decided.",
            "batch_not_submitted",
            {"status": batch.status},
        )
    if decision == ExpenseBatchStatus.RETURNED:
        _assert_reason_given(note)

    batch.status = decision
    batch.decided_by = user
    batch.decided_at = timezone.now()
    batch.decision_note = note
    batch.updated_by = user
    batch.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    recompute_totals(batch.project)
    return batch


def unapproved_expense_total(project):
    """What has been spent but not yet signed off."""
    return project.expenses.filter(is_active=True).exclude(
        batch__status=ExpenseBatchStatus.APPROVED
    ).aggregate(total=Sum("amount"))["total"] or ZERO


def budget_warning(project):
    """The overrun block, or None. Shape is part of the API contract."""
    if not project.is_over_budget:
        return None
    over_by = project.spent_amount - project.sanctioned_budget
    return {
        "code": "budget_exceeded",
        "sanctioned": str(project.sanctioned_budget),
        "spent": str(project.spent_amount),
        "over_by": str(over_by),
        "message": (
            f"This project is now {over_by} over its sanctioned budget. "
            "Raise a revision."
        ),
    }


def update_expense(expense, *, user, **data):
    project = expense.project
    _assert_writable(project)
    _assert_batch_editable(expense.batch)
    for field, value in data.items():
        setattr(expense, field, value)
    expense.updated_by = user
    expense.save()
    recompute_totals(project)
    return expense, budget_warning(project)


def delete_expense(expense, *, user):
    project = expense.project
    _assert_writable(project)
    _assert_batch_editable(expense.batch)
    expense.is_active = False
    expense.updated_by = user
    expense.save(update_fields=["is_active", "updated_by", "updated_at"])
    recompute_totals(project)


# ---------------------------------------------------------------------------
# Revisions -- more money, more time
# ---------------------------------------------------------------------------


@transaction.atomic
def request_revision(project, *, user, additional_amount=ZERO, new_end_date=None, reason):
    if project.is_closed:
        raise ConstructionError(
            "This project is closed.", "project_closed", {"status": project.status}
        )
    if not project.is_sanctioned:
        raise ConstructionError(
            "A project that has not been approved does not need a revision -- "
            "edit it instead.",
            "project_not_approved",
            {"status": project.status},
        )

    additional_amount = additional_amount or ZERO
    if additional_amount <= ZERO and new_end_date is None:
        raise ConstructionError(
            "A revision must ask for more budget, more time, or both.",
            "revision_asks_nothing",
            {},
        )
    if new_end_date is not None and new_end_date <= project.expected_end_date:
        raise ConstructionError(
            "The new end date must be later than the current one. This form "
            "extends a timeline; pulling a date in is a different conversation.",
            "new_end_date_not_later",
            {
                "new_end_date": new_end_date,
                "current_end_date": project.expected_end_date,
            },
        )
    if project.revisions.filter(status=RevisionStatus.PENDING).exists():
        raise ConstructionError(
            "This project already has a revision waiting for a decision.",
            "revision_already_pending",
            {},
        )

    # No more money until the last lot is accounted for. Sanctioning a fresh
    # budget on top of spend nobody has checked is how a project quietly
    # doubles: the figure being revised is not yet known to be right.
    outstanding = unapproved_expense_total(project)
    if outstanding > ZERO:
        raise ConstructionError(
            "Every payment already recorded has to be approved before more "
            "budget can be asked for.",
            "expenses_awaiting_approval",
            {"unapproved_total": outstanding},
        )

    last_no = (
        project.revisions.order_by("-revision_no")
        .values_list("revision_no", flat=True)
        .first()
        or 0
    )
    return ProjectRevision.objects.create(
        project=project,
        revision_no=last_no + 1,
        additional_amount=additional_amount,
        new_end_date=new_end_date,
        reason=reason,
        budget_before=project.sanctioned_budget,
        end_date_before=project.expected_end_date,
        requested_by=user,
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def approve_revision(revision, *, user, note=""):
    _assert_revision_pending(revision)
    revision.status = RevisionStatus.APPROVED
    revision.decided_by = user
    revision.decided_at = timezone.now()
    revision.decision_note = note
    revision.updated_by = user
    revision.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )

    project = revision.project
    if revision.new_end_date:
        project.expected_end_date = revision.new_end_date
        project.save(update_fields=["expected_end_date", "updated_at"])
    recompute_totals(project)
    return revision


def reject_revision(revision, *, user, note=""):
    _assert_revision_pending(revision)
    _assert_reason_given(note)
    revision.status = RevisionStatus.REJECTED
    revision.decided_by = user
    revision.decided_at = timezone.now()
    revision.decision_note = note
    revision.updated_by = user
    revision.save(
        update_fields=[
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return revision


def withdraw_revision(revision, *, user):
    _assert_revision_pending(revision)
    revision.status = RevisionStatus.WITHDRAWN
    revision.updated_by = user
    revision.save(update_fields=["status", "updated_by", "updated_at"])
    return revision


def _assert_reason_given(note):
    """A rejection without a reason leaves the asker nothing to act on."""
    if not (note or "").strip():
        raise ConstructionError(
            "Say why you are rejecting this.", "rejection_needs_a_reason", {}
        )


def _assert_revision_pending(revision):
    if revision.status != RevisionStatus.PENDING:
        raise ConstructionError(
            "This revision has already been decided.",
            "revision_not_pending",
            {"status": revision.status},
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _money(value):
    """Money and percentages leave this module as strings, never floats.

    DRF's JSON encoder turns a raw ``Decimal`` in a plain dict into a float,
    which silently loses paise on the way out. Model serializers are safe --
    ``DecimalField`` already stringifies -- but the composed reads below build
    plain dicts, so they quantise and stringify here.
    """
    return str((value if value is not None else ZERO).quantize(Decimal("0.01")))




def project_summary(project):
    """The header every screen shows."""
    last_log = (
        project.daily_logs.filter(is_active=True)
        .order_by("-log_date")
        .values_list("log_date", flat=True)
        .first()
    )
    today = timezone.localdate()
    spent_today = project.expenses.filter(
        is_active=True, spend_date=today
    ).aggregate(total=Sum("amount"))["total"] or ZERO

    unapproved = project.expenses.filter(is_active=True).exclude(
        batch__status=ExpenseBatchStatus.APPROVED
    ).aggregate(total=Sum("amount"), count=Count("id"))
    approved_total = project.expenses.filter(
        is_active=True, batch__status=ExpenseBatchStatus.APPROVED
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    open_batch = current_batch(project, create=False)

    return {
        "id": project.id,
        "code": project.code,
        "name": project.name,
        "status": project.status,
        "status_display": project.get_status_display(),
        "location": project.location,
        "sanctioned_budget": _money(project.sanctioned_budget),
        "spent_amount": _money(project.spent_amount),
        "remaining": _money(project.remaining_budget),
        "percent_used": _money(project.percent_used),
        "is_over_budget": project.is_over_budget,
        "start_date": project.start_date,
        "expected_end_date": project.expected_end_date,
        "actual_end_date": project.actual_end_date,
        "days_elapsed": project.days_elapsed,
        "days_left": project.days_left,
        "is_overdue": project.is_overdue,
        "progress_percent": _money(project.progress_percent),
        "last_log_date": last_log,
        # A project nobody has written up for five days is the first sign
        # something has stalled, and it is visible without opening anything.
        "days_since_last_log": (today - last_log).days if last_log else None,
        "revisions": {
            "count": project.revisions.filter(is_active=True).count(),
            "pending": project.revisions.filter(status=RevisionStatus.PENDING).count(),
        },
        "spent_today": _money(spent_today),
        # ``spent_amount`` above is everything recorded. These break it out so a
        # reader can see how much of it nobody has signed off.
        "approved_amount": _money(approved_total),
        "pending_amount": _money(unapproved["total"]),
        "pending_count": unapproved["count"],
        # A revision is refused while this is above zero -- see request_revision.
        "can_request_revision": (unapproved["total"] or ZERO) <= ZERO,
        "open_batch": (
            {
                "id": open_batch.id,
                "batch_no": open_batch.batch_no,
                "status": open_batch.status,
                "line_count": open_batch.line_count,
                "total": _money(open_batch.total),
            }
            if open_batch
            else None
        ),
    }


def day_view(project, day):
    """The log, the expenses and the running totals for one date."""
    log = project.daily_logs.filter(log_date=day, is_active=True).first()
    expenses = project.expenses.filter(spend_date=day, is_active=True)
    spent_today = expenses.aggregate(total=Sum("amount"))["total"] or ZERO
    spent_to_date = project.expenses.filter(
        is_active=True, spend_date__lte=day
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    return {
        "date": day,
        "log": log,
        "expenses": expenses,
        "spent_today": _money(spent_today),
        "spent_to_date": _money(spent_to_date),
        "budget_remaining": _money(project.sanctioned_budget - spent_to_date),
    }


def spend_summary(project):
    """Spend by category and by month, plus days lost by reason.

    Two charts and the timeline-extension justification, from one call.
    """
    expenses = project.expenses.filter(is_active=True)

    by_category = [
        {
            "category": row["category"],
            "amount": _money(row["amount"]),
            "count": row["count"],
        }
        for row in expenses.values("category")
        .annotate(amount=Sum("amount"), count=Count("id"))
        .order_by("-amount")
    ]

    by_month = [
        {"month": row["month"], "amount": _money(row["amount"])}
        for row in expenses.annotate(month=TruncMonth("spend_date"))
        .values("month")
        .annotate(amount=Sum("amount"))
        .order_by("month")
    ]

    # One row per reason. These can sum to MORE than ``days_lost_total``: a day
    # that both rained and ran out of material is one lost day counted under
    # two reasons, which is the honest answer to "why did we lose time".
    days_lost = [
        {"reason": row["reason"], "days": row["days"]}
        for row in DailyLogStopReason.objects.filter(
            daily_log__project=project,
            daily_log__is_active=True,
            daily_log__work_stopped=True,
        )
        .values("reason")
        .annotate(days=Count("daily_log", distinct=True))
        .order_by("-days")
    ]

    logs = project.daily_logs.filter(is_active=True)
    return {
        "total_spent": _money(expenses.aggregate(total=Sum("amount"))["total"]),
        "sanctioned_budget": _money(project.sanctioned_budget),
        "by_category": by_category,
        "by_month": by_month,
        "days_logged": logs.count(),
        "days_lost": days_lost,
        "days_lost_total": logs.filter(work_stopped=True).count(),
    }
