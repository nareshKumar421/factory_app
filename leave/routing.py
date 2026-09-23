"""
Who may decide whose leave.

This is the first module in the repo to authorise anything by **position in the
org** rather than by permission alone. ``labour_request``, ``budget_approvals``
and ``invoice_approval`` all work the other way: hold the grant and you can
decide anything. That is right for a budget and wrong for leave, where the
question is not "are you an approver?" but "are you *their* approver?".

So a decision needs **both**:

1. the ``can_decide_leave`` permission -- you are the kind of person who decides; and
2. a position above the applicant in the reporting tree -- you are *their* one.

``can_decide_any_leave`` is the single grant that skips step 2, and it is HR's.

**Three authorities, and the trail records which was used.** ``manager`` is the
applicant's direct manager, ``skip_level`` is anybody further up the same
chain, ``hr`` is the blanket grant. They are stored on the trail rather than
re-derived later because the tree moves: somebody who approved as a manager in
March may not be in that line by July, and the record should still say what it
was at the time.

**Why skip-level is allowed at all.** Managers go on leave themselves, and 13
people carry the reporting lines for 249 employees on this data -- if the one
manager in a chain is away, their team cannot get leave approved at all. The
materialised path makes "is this person above that one?" a single string
comparison, so allowing it costs nothing and refusing it would strand people.

**Nobody approves their own leave**, however high they sit, and that is checked
before anything else. A CEO with ``can_decide_any_leave`` still cannot sign off
their own week off; somebody else with the HR grant does it.
"""

from django.db.models import Q

from employee_hierarchy.access import has_any, viewer_employee
from employee_hierarchy.constants import IN_SERVICE_STATUSES
from employee_hierarchy.models import Department

DECIDE_OWN_TEAM = "leave.can_decide_leave"
DECIDE_ANY = "leave.can_decide_any_leave"
APPLY = "leave.can_apply_leave"
APPLY_FOR_OTHERS = "leave.can_apply_leave_for_others"
VIEW_TEAM = "leave.can_view_team_leave"
CANCEL_APPROVED = "leave.can_cancel_approved_leave"
MANAGE_TYPES = "leave.can_manage_leave_types"

#: Holding any of these reveals the module.
ANY_ACCESS = (APPLY, APPLY_FOR_OTHERS, VIEW_TEAM, DECIDE_OWN_TEAM, DECIDE_ANY)

#: The three ways somebody can be entitled to decide, most specific first.
AUTHORITY_MANAGER = "manager"
AUTHORITY_SKIP_LEVEL = "skip_level"
AUTHORITY_HR = "hr"


def responsible_manager(employee):
    """The employee who ought to decide this person's leave, or ``None``.

    Walks **up** the chain rather than taking ``reporting_manager`` flat,
    because a manager who has left or been suspended cannot decide anything and
    their team should not be stuck behind them. Falls back to the head of the
    applicant's department, which covers the tree roots -- on the live data
    seven people report to nobody, and without a fallback their leave could only
    ever be decided by HR.
    """
    if employee is None:
        return None

    seen = set()
    candidate = employee.reporting_manager
    while candidate is not None and candidate.pk not in seen:
        seen.add(candidate.pk)
        if candidate.employment_status in IN_SERVICE_STATUSES:
            return candidate
        candidate = candidate.reporting_manager

    department = employee.department
    if department is not None:
        head = Department.objects.filter(pk=department.pk).values_list("head", flat=True).first()
        if head and head != employee.pk:
            from employee_hierarchy.models import Employee

            candidate = Employee.objects.filter(pk=head).first()
            if candidate is not None and candidate.employment_status in IN_SERVICE_STATUSES:
                return candidate
    return None


def authority_of(user, request):
    """How ``user`` is entitled to decide ``request``, or ``None``.

    Returns one of :data:`AUTHORITY_MANAGER`, :data:`AUTHORITY_SKIP_LEVEL`,
    :data:`AUTHORITY_HR`. The order is deliberate: the most specific
    entitlement wins, so an HR person who *is* the applicant's manager is
    recorded as their manager, which is the more informative of the two.
    """
    if user is None or not user.is_authenticated:
        return None

    applicant = request.employee
    viewer = viewer_employee(user)

    # Nobody signs off their own leave, whatever they hold.
    if viewer is not None and viewer.pk == applicant.pk:
        return None

    if viewer is not None and has_any(user, (DECIDE_OWN_TEAM,)):
        if applicant.reporting_manager_id == viewer.pk:
            return AUTHORITY_MANAGER
        # Anywhere above them in the same chain. One string comparison --
        # the applicant's path starts with the viewer's prefix.
        if applicant.hierarchy_path and applicant.hierarchy_path.startswith(
            viewer.path_prefix
        ):
            return AUTHORITY_SKIP_LEVEL

    if has_any(user, (DECIDE_ANY,)):
        return AUTHORITY_HR

    return None


def can_decide(user, request):
    return authority_of(user, request) is not None


def can_cancel(user, request):
    """Cancelling an approved leave is a separate grant, plus a position.

    HR may cancel anybody's. A manager may cancel one they could have decided
    -- taking back your own approval is part of making it.
    """
    if not has_any(user, (CANCEL_APPROVED,)):
        return False
    return can_decide(user, request) or has_any(user, (DECIDE_ANY,))


def visible_filter(user):
    """A ``Q`` limiting requests to the ones ``user`` may see.

    Four reaches, unioned:

    * their own, always;
    * anything they raised on somebody's behalf, so the time office can follow
      up what it submitted;
    * their whole subtree, with ``can_view_team_leave`` or ``can_decide_leave``;
    * everything, with ``can_decide_any_leave``.

    Returned as a ``Q`` rather than a list of ids so the manager's queue over a
    four-thousand-employee tree stays one indexed query -- the same shape, and
    for the same reason, as ``employee_hierarchy.access.salary_visibility_filter``.
    """
    if user is None or not user.is_authenticated:
        return Q(pk__in=[])

    if has_any(user, (DECIDE_ANY,)):
        return Q()

    reach = Q(applied_by=user)

    viewer = viewer_employee(user)
    if viewer is not None:
        reach |= Q(employee=viewer)
        if has_any(user, (VIEW_TEAM, DECIDE_OWN_TEAM)):
            # Everyone below them, at any depth. Excludes themselves, which
            # the `employee=viewer` clause above has already covered.
            reach |= Q(employee__hierarchy_path__startswith=viewer.path_prefix) & ~Q(
                employee=viewer
            )

    return reach


def decidable_filter(user):
    """A ``Q`` limiting requests to the ones ``user`` may act on.

    Narrower than :func:`visible_filter`: seeing your own leave does not mean
    approving it, and the time office seeing what it raised does not mean
    deciding it.
    """
    if user is None or not user.is_authenticated:
        return Q(pk__in=[])

    viewer = viewer_employee(user)

    if has_any(user, (DECIDE_ANY,)):
        # Everything except their own.
        return ~Q(employee=viewer) if viewer is not None else Q()

    if viewer is not None and has_any(user, (DECIDE_OWN_TEAM,)):
        return Q(employee__hierarchy_path__startswith=viewer.path_prefix) & ~Q(
            employee=viewer
        )

    return Q(pk__in=[])


def can_apply_for(user, employee):
    """Whether ``user`` may raise an application naming ``employee``.

    Yourself with :data:`APPLY`; anybody with :data:`APPLY_FOR_OTHERS`, which is
    the time office's grant and exists because roughly half the workforce has no
    login and could otherwise never be recorded as on leave.
    """
    if user is None or not user.is_authenticated:
        return False
    if has_any(user, (APPLY_FOR_OTHERS,)):
        return True
    viewer = viewer_employee(user)
    return viewer is not None and viewer.pk == employee.pk and has_any(user, (APPLY,))
