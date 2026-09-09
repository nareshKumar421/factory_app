"""
Every write this module performs, and the trail each one leaves.

The rule the whole module hangs on: **nothing outside this file changes an
employee, a salary or the tree.** Views validate input and call in here. The
reason is not tidiness -- it is that each of these operations is really three or
four writes that must happen together. Moving a manager rewrites a subtree,
writes a history row and writes an audit row; approving a revision supersedes
the old record, refreshes the cached figure and writes two rows more. Any of
those half-done leaves a directory that lies about somebody's pay or their
place in the company.

So each public function here is one ``@transaction.atomic`` business act, and
each one records what it did:

* :class:`~employee_hierarchy.models.EmployeeHistory` -- the employee's story,
  in the words a timeline shows them.
* :class:`~employee_hierarchy.models.EmployeeAuditLog` -- the administrative
  record: who did it, what the value was before, what it is now, and why.

Salary is append-only. There is no ``edit_salary``. A revision inserts a new
record and supersedes the old one, which is what makes the history in the brief
("2024 → 2025 → 2026") a set of facts nobody had to reconstruct.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import hierarchy
from .constants import (
    EXIT_STATUSES,
    AuditAction,
    EmploymentStatus,
    HistoryEvent,
    RecordStatus,
    RevisionType,
    SalaryStatus,
)
from .models import (
    Department,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    SalaryRevision,
)


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------


def _actor(user):
    """The user to stamp, or ``None`` for a management command / migration."""
    return user if (user is not None and getattr(user, "is_authenticated", False)) else None


def log_history(employee, event, *, occurred_on=None, from_value="", to_value="", notes="", user=None):
    """Add one row to the employee's career timeline."""
    return EmployeeHistory.objects.create(
        employee=employee,
        event=event,
        occurred_on=occurred_on or timezone.localdate(),
        from_value=str(from_value or ""),
        to_value=str(to_value or ""),
        notes=notes or "",
        created_by=_actor(user),
        updated_by=_actor(user),
    )


def log_audit(employee, action, *, field="", previous="", new="", reason="", notes="", user=None):
    """Record one administrative act. Never updated, never deleted."""
    return EmployeeAuditLog.objects.create(
        employee=employee,
        action=action,
        field=field,
        previous_value="" if previous is None else str(previous),
        new_value="" if new is None else str(new),
        performed_by=_actor(user),
        reason=reason or "",
        notes=notes or "",
    )


def _stamp(instance, user):
    actor = _actor(user)
    if actor is not None:
        instance.updated_by = actor
        if instance.pk is None:
            instance.created_by = actor


def _label(value):
    """How a value reads in a history or audit row.

    Names, not ids: history has to keep saying what was true at the time, and
    an id printed in 2025 means nothing once the row behind it is renamed. An
    employee carries their code too, because two people share a name often
    enough to matter.
    """
    if value is None:
        return "—"
    if isinstance(value, Employee):
        return f"{value.full_name} ({value.employee_code})"
    return getattr(value, "name", None) or str(value)


def _money(amount, currency):
    return f"{currency} {amount:,.2f}"


def _salary_kwargs(salary, *, revision_type, fallback_reason):
    """Normalise a salary block that arrived alongside some other act.

    Hiring somebody and promoting them both carry a reason of their own, and
    the salary block the screen sends alongside carries one too. Passing both
    through to :func:`create_salary_record` is what used to happen, and Python
    refused the call -- every promotion with a raise attached, and every hire
    with a joining salary, died with a ``TypeError`` and answered 500.

    So the collision is resolved here, once, in the order that makes sense:

    * the **reason** on the salary block wins if it has one, because it is the
      more specific of the two ("Band 4 minimum" beats "Promoted"); the
      surrounding act's reason is the fallback;
    * the **revision type** is decided by the act and the payload's is dropped
      -- a promotion's revision is a promotion whatever the client sent, and a
      joining salary is a joining salary. Letting a client override that would
      make the revision-type report meaningless.

    Everything else in the block is passed through untouched.
    """
    kwargs = dict(salary)
    kwargs.pop("revision_type", None)
    reason = (kwargs.pop("reason", None) or "").strip() or fallback_reason
    return {**kwargs, "revision_type": revision_type, "reason": reason}


# ---------------------------------------------------------------------------
# Masters: departments and designations
# ---------------------------------------------------------------------------


def validate_department_parent(department, parent):
    """Raise unless ``parent`` may hold ``department``.

    The same two rules as the employee tree, for the same reason: a department
    inside itself is a page that never finishes rendering.
    """
    if parent is None:
        return
    if department.pk and parent.pk == department.pk:
        raise ValidationError("A department cannot sit inside itself.")
    if parent.company_id != department.company_id:
        raise ValidationError("A parent department must belong to the same company.")
    seen = set()
    walker = parent
    while walker is not None:
        if walker.pk in seen:
            break
        seen.add(walker.pk)
        if department.pk and walker.pk == department.pk:
            raise ValidationError(
                f"{parent.name} already sits under {department.name}. "
                "That would nest the department inside itself."
            )
        walker = walker.parent


def department_descendants(department):
    """Every department nested under this one, at any depth."""
    children = {}
    for row_id, parent_id in Department.objects.filter(
        company_id=department.company_id
    ).values_list("id", "parent_id"):
        children.setdefault(parent_id, []).append(row_id)

    reached = []
    queue = list(children.get(department.pk, []))
    while queue:
        row_id = queue.pop()
        reached.append(row_id)
        queue.extend(children.get(row_id, []))
    return reached


@transaction.atomic
def retire_department(department, user=None):
    """Mark a department inactive rather than deleting it.

    Deletion is not offered. Last year's history names the department an
    employee moved out of, and a deleted row would make that sentence
    unreadable. A department with employees still in it cannot be retired --
    move them first, which forces somebody to decide where they belong.
    """
    if department.employees.exists():
        raise ValidationError(
            "There are still employees in this department. Move them before retiring it."
        )
    live_children = department.children.filter(status=RecordStatus.ACTIVE)
    if live_children.exists():
        raise ValidationError(
            "This department still has active sub-departments. Retire or move them first."
        )
    department.status = RecordStatus.INACTIVE
    _stamp(department, user)
    department.save(update_fields=["status", "updated_at", "updated_by"])
    return department


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


@transaction.atomic
def create_employee(*, company, data, user=None):
    """Add an employee and place them in the tree.

    ``data`` is validated serializer output. ``reporting_manager`` may be
    ``None``, which creates a top-level employee -- a CEO, or the head of a
    separate unit; the brief wants more than one of those to be possible.

    The employee is saved before being placed, because a materialised path
    contains their own id and so cannot be built until they have one.
    """
    manager = data.pop("reporting_manager", None)
    initial_salary = data.pop("initial_salary", None)

    employee = Employee(company=company, **data)
    if manager is not None:
        # Checked before the insert so an impossible placement never leaves a
        # half-created employee behind.
        hierarchy.validate_manager(employee, manager)
    if employee.designation_id and not employee.is_manager:
        employee.is_manager = bool(employee.designation.is_managerial)
    _stamp(employee, user)
    employee.save()

    hierarchy.place(employee, manager)
    if manager is not None:
        _mark_as_manager(manager, user)

    log_history(
        employee,
        HistoryEvent.JOINED,
        occurred_on=employee.joining_date,
        to_value=_label(employee.designation) if employee.designation_id else employee.job_title,
        notes=f"Joined {_label(employee.department)}" if employee.department_id else "",
        user=user,
    )
    log_audit(
        employee,
        AuditAction.EMPLOYEE_CREATED,
        new=f"{employee.employee_code} – {employee.full_name}",
        user=user,
    )
    if manager is not None:
        log_audit(
            employee,
            AuditAction.MANAGER_CHANGED,
            field="reporting_manager",
            previous="",
            new=_label(manager),
            reason="Initial placement",
            user=user,
        )

    if initial_salary:
        create_salary_record(
            employee,
            user=user,
            **_salary_kwargs(
                initial_salary,
                revision_type=RevisionType.INITIAL,
                fallback_reason="Joining salary",
            ),
        )
    return employee


def _mark_as_manager(manager, user=None):
    """Turn the manager flag on -- never off.

    Gaining a report proves someone manages people. Losing every report proves
    nothing: a manager whose team was just reassigned still holds the role, and
    flipping the flag back would erase that.
    """
    if not manager.is_manager:
        manager.is_manager = True
        _stamp(manager, user)
        manager.save(update_fields=["is_manager", "updated_at", "updated_by"])


#: Employee fields that :func:`update_employee` may touch, and how a change to
#: each reads in the trail. Anything structural -- manager, department,
#: designation, status -- is deliberately absent: those have their own
#: functions because each one has consequences beyond writing a column.
EDITABLE_FIELDS = {
    "employee_code": "Employee code",
    "first_name": "First name",
    "last_name": "Last name",
    "email": "Email",
    "phone": "Phone",
    "date_of_birth": "Date of birth",
    "joining_date": "Joining date",
    "job_title": "Job title",
    "location": "Location",
    "photo": "Profile photo",
    "user": "Linked login",
    "is_manager": "Manager flag",
}


@transaction.atomic
def update_employee(employee, data, *, user=None, reason=""):
    """Edit the plain details. Returns the list of changes made.

    Only the fields in :data:`EDITABLE_FIELDS`. A caller that wants to move
    somebody in the tree, change their department, promote them or end their
    employment calls the function for that -- each of those does more than
    write a column, and letting them ride along on a general-purpose update is
    how a directory ends up with a manager change nobody recorded.
    """
    changes = []
    touched = []
    for field, label in EDITABLE_FIELDS.items():
        if field not in data:
            continue
        previous = getattr(employee, field)
        new = data[field]
        if previous == new:
            continue
        setattr(employee, field, new)
        touched.append(field)
        changes.append((field, label, previous, new))

    if not touched:
        return []

    if "location" in touched:
        before = dict((field, previous) for field, _, previous, _ in changes)
        log_history(
            employee,
            HistoryEvent.LOCATION_CHANGED,
            from_value=before["location"] or "—",
            to_value=employee.location or "—",
            user=user,
        )

    _stamp(employee, user)
    employee.save()

    for field, label, previous, new in changes:
        log_audit(
            employee,
            AuditAction.EMPLOYEE_UPDATED,
            field=field,
            previous=_label(previous) if previous not in ("", None) else "",
            new=_label(new) if new not in ("", None) else "",
            reason=reason,
            user=user,
        )
    return changes


@transaction.atomic
def change_manager(employee, manager, *, user=None, reason="", carry_team=True):
    """Re-point an employee at a new manager, or at nobody.

    ``carry_team=True`` (the default, and what the brief asks for) moves their
    whole subordinate structure with them: a manager who transfers takes their
    team, and the hierarchy below them is untouched.

    ``carry_team=False`` is the other thing people mean by "move him": the
    person goes alone and their direct reports move up one level, to the
    manager they are leaving. That is the honest way to do it -- the reports
    have to end up somewhere, and silently leaving them pointed at somebody who
    now sits three levels away in another department is not it. An employee
    with reports and no current manager cannot go alone; there is nowhere to
    leave the team.
    """
    previous_manager = employee.reporting_manager
    if manager is not None and previous_manager is not None and manager.pk == previous_manager.pk:
        return {"moved": 0, "team_moved": 0, "changed": False}

    reports = list(hierarchy.direct_reports(employee))
    orphans = 0
    if reports and not carry_team:
        if previous_manager is None:
            raise ValidationError(
                f"{employee.full_name} has {len(reports)} direct report(s) and no manager to "
                "leave them with. Move the team with them, or reassign the team first."
            )
        for report in reports:
            hierarchy.move_to_manager(report, previous_manager)
            log_history(
                report,
                HistoryEvent.MANAGER_CHANGED,
                from_value=_label(employee),
                to_value=_label(previous_manager),
                notes=f"{employee.full_name} moved away",
                user=user,
            )
            log_audit(
                report,
                AuditAction.MANAGER_CHANGED,
                field="reporting_manager",
                previous=_label(employee),
                new=_label(previous_manager),
                reason=reason or f"{employee.full_name} was moved without their team",
                user=user,
            )
        orphans = len(reports)
        employee.refresh_from_db()

    team_moved = hierarchy.move_to_manager(employee, manager)

    if manager is None:
        log_history(
            employee,
            HistoryEvent.MANAGER_REMOVED,
            from_value=_label(previous_manager),
            to_value="Top level",
            notes=reason,
            user=user,
        )
        log_audit(
            employee,
            AuditAction.MANAGER_REMOVED,
            field="reporting_manager",
            previous=_label(previous_manager),
            new="",
            reason=reason,
            user=user,
        )
    else:
        _mark_as_manager(manager, user)
        log_history(
            employee,
            HistoryEvent.MANAGER_CHANGED,
            from_value=_label(previous_manager),
            to_value=_label(manager),
            notes=reason,
            user=user,
        )
        log_audit(
            employee,
            AuditAction.SUBTREE_MOVED if team_moved else AuditAction.MANAGER_CHANGED,
            field="reporting_manager",
            previous=_label(previous_manager),
            new=_label(manager),
            reason=reason,
            notes=f"{team_moved} team member(s) moved with them." if team_moved else "",
            user=user,
        )

    if team_moved:
        log_history(
            employee,
            HistoryEvent.TEAM_MOVED,
            to_value=f"{team_moved} team member(s)",
            notes=f"Moved with them under {_label(manager)}",
            user=user,
        )

    return {"moved": 1, "team_moved": team_moved, "reports_reassigned": orphans, "changed": True}


@transaction.atomic
def change_department(employee, department, *, user=None, reason="", include_team=False):
    """Move an employee to another department.

    Reporting is left alone on purpose: which department someone belongs to and
    who they report to are different facts, and a transfer that quietly
    re-pointed their manager would be two changes wearing one name.
    ``include_team=True`` carries their whole subtree across, which is what a
    department restructure means.
    """
    previous = employee.department
    if previous == department:
        return {"changed": False, "team_moved": 0}

    employee.department = department
    _stamp(employee, user)
    employee.save(update_fields=["department", "updated_at", "updated_by"])

    log_history(
        employee,
        HistoryEvent.DEPARTMENT_CHANGED,
        from_value=_label(previous),
        to_value=_label(department),
        notes=reason,
        user=user,
    )
    log_audit(
        employee,
        AuditAction.DEPARTMENT_CHANGED,
        field="department",
        previous=_label(previous),
        new=_label(department),
        reason=reason,
        user=user,
    )

    team_moved = 0
    if include_team:
        for member in hierarchy.subtree(employee).select_related("department"):
            member_previous = member.department
            if member_previous == department:
                continue
            member.department = department
            _stamp(member, user)
            member.save(update_fields=["department", "updated_at", "updated_by"])
            log_history(
                member,
                HistoryEvent.DEPARTMENT_CHANGED,
                from_value=_label(member_previous),
                to_value=_label(department),
                notes=f"Moved with {employee.full_name}'s team",
                user=user,
            )
            log_audit(
                member,
                AuditAction.DEPARTMENT_CHANGED,
                field="department",
                previous=_label(member_previous),
                new=_label(department),
                reason=reason or f"Team of {employee.full_name} transferred",
                user=user,
            )
            team_moved += 1

    return {"changed": True, "team_moved": team_moved}


@transaction.atomic
def change_designation(employee, designation, *, user=None, reason="", promotion=False):
    """Change the rung of the ladder someone is on.

    ``promotion=True`` only changes how it is recorded -- the timeline says
    "Promoted" instead of "Designation changed". The money is not part of it:
    :func:`create_salary_record` is a separate act with its own approval, and
    plenty of promotions land a month before the revision does.
    """
    previous = employee.designation
    if previous == designation:
        return {"changed": False}

    employee.designation = designation
    if designation is not None and designation.is_managerial:
        employee.is_manager = True
    _stamp(employee, user)
    employee.save(update_fields=["designation", "is_manager", "updated_at", "updated_by"])

    log_history(
        employee,
        HistoryEvent.PROMOTED if promotion else HistoryEvent.DESIGNATION_CHANGED,
        from_value=_label(previous),
        to_value=_label(designation),
        notes=reason,
        user=user,
    )
    log_audit(
        employee,
        AuditAction.PROMOTED if promotion else AuditAction.DESIGNATION_CHANGED,
        field="designation",
        previous=_label(previous),
        new=_label(designation),
        reason=reason,
        user=user,
    )
    return {"changed": True}


@transaction.atomic
def promote(employee, *, designation=None, manager=None, department=None, salary=None, user=None, reason=""):
    """A promotion, as it actually happens: several changes, one decision.

    New rung, sometimes a new manager, sometimes a new department, usually a
    revision -- and one reason recorded against all of them, so the trail reads
    as one event a year later instead of four unrelated edits made the same
    afternoon.
    """
    result = {"designation": False, "manager": False, "department": False, "salary": None}
    if designation is not None:
        result["designation"] = change_designation(
            employee, designation, user=user, reason=reason, promotion=True
        )["changed"]
    if department is not None:
        result["department"] = change_department(
            employee, department, user=user, reason=reason
        )["changed"]
    if manager is not None:
        result["manager"] = change_manager(employee, manager, user=user, reason=reason)["changed"]
    if salary:
        result["salary"] = create_salary_record(
            employee,
            user=user,
            **_salary_kwargs(
                salary,
                revision_type=RevisionType.PROMOTION,
                fallback_reason=reason or "Promotion",
            ),
        )
    return result


@transaction.atomic
def change_status(employee, status, *, user=None, reason="", exit_date=None, reassign_reports_to=None):
    """Change employment status, and keep the tree honest about it.

    The hierarchy consequences are the point of this function:

    * Reaching an **exit** status (resigned, terminated, retired, inactive)
      means the person cannot hold a team. Their direct reports move to
      ``reassign_reports_to`` if given, and otherwise to the leaver's own
      manager -- one level up, which is where a team goes by default when its
      lead leaves and nobody has decided yet. A leaver with reports and no
      manager needs an explicit target: the system will not guess who runs an
      orphaned department.
    * **Suspension** does the same, because a suspension is exactly the moment
      a team needs to report somewhere else.
    * The leaver keeps their own place in the chain, so their history still
      reads correctly and the audit trail still shows who they worked for.
    """
    previous = employee.employment_status
    if previous == status:
        return {"changed": False, "reports_reassigned": 0}

    labels = dict(EmploymentStatus.choices)
    reassigned = 0
    loses_team = status in EXIT_STATUSES or status == EmploymentStatus.SUSPENDED
    reports = list(hierarchy.direct_reports(employee)) if loses_team else []
    if reports:
        target = reassign_reports_to or employee.reporting_manager
        if target is None:
            raise ValidationError(
                f"{employee.full_name} has {len(reports)} direct report(s) and no manager above "
                "them. Say who the team should report to before changing the status."
            )
        for report in reports:
            hierarchy.move_to_manager(report, target)
            log_history(
                report,
                HistoryEvent.MANAGER_CHANGED,
                from_value=_label(employee),
                to_value=_label(target),
                notes=f"{employee.full_name} is now {labels[status].lower()}",
                user=user,
            )
            log_audit(
                report,
                AuditAction.MANAGER_CHANGED,
                field="reporting_manager",
                previous=_label(employee),
                new=_label(target),
                reason=reason or f"{employee.full_name} is no longer able to hold a team",
                user=user,
            )
            reassigned += 1

    employee.employment_status = status
    if status in EXIT_STATUSES:
        employee.exit_date = exit_date or timezone.localdate()
    elif previous in EXIT_STATUSES:
        # Coming back: a rehire or a correction. The exit date must go, or
        # turnover reporting keeps counting them as gone.
        employee.exit_date = None
    _stamp(employee, user)
    employee.save(update_fields=["employment_status", "exit_date", "updated_at", "updated_by"])

    log_history(
        employee,
        HistoryEvent.EXITED if status in EXIT_STATUSES else HistoryEvent.STATUS_CHANGED,
        occurred_on=employee.exit_date if status in EXIT_STATUSES else None,
        from_value=labels.get(previous, previous),
        to_value=labels.get(status, status),
        notes=reason,
        user=user,
    )
    log_audit(
        employee,
        AuditAction.STATUS_CHANGED,
        field="employment_status",
        previous=labels.get(previous, previous),
        new=labels.get(status, status),
        reason=reason,
        notes=f"{reassigned} report(s) reassigned." if reassigned else "",
        user=user,
    )
    return {"changed": True, "reports_reassigned": reassigned}


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------


def current_salary(employee):
    """The record being paid today: approved, and the latest one already started."""
    today = timezone.localdate()
    return (
        employee.salary_records.filter(
            status__in=(SalaryStatus.ACTIVE, SalaryStatus.SUPERSEDED),
            effective_from__lte=today,
        )
        .order_by("-effective_from", "-id")
        .first()
    )


@transaction.atomic
def recalculate_current_salary(employee, *, user=None):
    """Re-decide which approved record is in force, and refresh the cache.

    Called after anything that can change the answer -- a new record, an
    approval, a future-dated revision coming due. Exactly one approved record
    ends up ``ACTIVE``: the latest whose effective date has arrived. Later ones
    are ``SCHEDULED``, earlier ones ``SUPERSEDED``.

    ``Employee.current_salary_amount`` is then written from it. That column is a
    cache -- it exists so a salary-band filter or a distribution chart over
    thousands of rows is one indexed scan -- and this is the only function that
    may write it.
    """
    today = timezone.localdate()
    approved = list(
        employee.salary_records.filter(
            status__in=(SalaryStatus.ACTIVE, SalaryStatus.SCHEDULED, SalaryStatus.SUPERSEDED)
        ).order_by("effective_from", "id")
    )
    in_force = None
    for record in approved:
        if record.effective_from <= today:
            in_force = record

    for record in approved:
        if record is in_force:
            wanted = SalaryStatus.ACTIVE
        elif record.effective_from > today:
            wanted = SalaryStatus.SCHEDULED
        else:
            wanted = SalaryStatus.SUPERSEDED
        if record.status != wanted:
            record.status = wanted
            record.save(update_fields=["status", "updated_at"])

    amount = in_force.total_compensation if in_force else None
    currency = in_force.currency if in_force else employee.current_salary_currency
    if employee.current_salary_amount != amount or employee.current_salary_currency != currency:
        employee.current_salary_amount = amount
        employee.current_salary_currency = currency or ""
        employee.save(update_fields=["current_salary_amount", "current_salary_currency"])
    return in_force


@transaction.atomic
def create_salary_record(
    employee,
    *,
    effective_from,
    basic_salary,
    allowances=0,
    bonuses=0,
    deductions=0,
    currency=None,
    revision_type=RevisionType.ANNUAL_INCREMENT,
    reason="",
    notes="",
    revision_date=None,
    approve=False,
    user=None,
):
    """Add a salary record, and the revision that explains it.

    This is the only way money enters the system, and it **adds** -- the
    previous record is left exactly as it was. The revision row beside it
    carries the pair of figures, the type, the reason and who asked, so the
    revision-history report never has to diff two records to work out what
    happened.

    A record starts ``PENDING``: proposing a raise and approving one are
    different rights (see :class:`~employee_hierarchy.models.EmployeePermission`).
    ``approve=True`` is only honoured for a user who holds the approval right,
    and puts it straight in force -- which is the normal path for a joining
    salary entered by HR.
    """
    currency = currency or employee.current_salary_currency or "INR"
    previous = current_salary(employee)

    if employee.salary_records.filter(effective_from=effective_from).exists():
        raise ValidationError(
            f"There is already a salary record for {employee.full_name} effective "
            f"{effective_from:%d %b %Y}. Pick another date, or reject the existing one first."
        )
    if previous is not None and effective_from < previous.effective_from:
        raise ValidationError(
            "A revision cannot start before the salary already in force "
            f"({previous.effective_from:%d %b %Y}). Salary history is append-only."
        )

    record = EmployeeSalary(
        employee=employee,
        basic_salary=basic_salary,
        allowances=allowances or 0,
        bonuses=bonuses or 0,
        deductions=deductions or 0,
        currency=currency,
        effective_from=effective_from,
        revision_date=revision_date or timezone.localdate(),
        status=SalaryStatus.PENDING,
        notes=notes or "",
    )
    _stamp(record, user)
    record.save()

    revision = SalaryRevision(
        employee=employee,
        salary_record=record,
        previous_amount=previous.total_compensation if previous else None,
        new_amount=record.total_compensation,
        currency=currency,
        effective_date=effective_from,
        revision_date=record.revision_date,
        revision_type=revision_type,
        reason=reason or "",
        notes=notes or "",
    )
    _stamp(revision, user)
    revision.save()

    log_audit(
        employee,
        AuditAction.SALARY_CREATED,
        field="salary",
        previous=_money(previous.total_compensation, previous.currency) if previous else "",
        new=_money(record.total_compensation, currency),
        reason=reason,
        notes=f"Effective {effective_from:%d %b %Y}, awaiting approval.",
        user=user,
    )

    if approve:
        approve_salary_record(record, user=user, reason=reason)
    return record


@transaction.atomic
def approve_salary_record(record, *, user=None, reason=""):
    """Put a proposed revision in force.

    Approval is the moment the figure becomes real, so it is also the moment
    the employee's timeline gets its "Salary revised" row -- a proposal that is
    never approved must not appear in somebody's career history as a raise they
    did not get.
    """
    if record.status not in (SalaryStatus.DRAFT, SalaryStatus.PENDING):
        raise ValidationError(
            f"This record is already {record.get_status_display().lower()}; "
            "there is nothing to approve."
        )
    record.status = SalaryStatus.SCHEDULED
    record.approved_by = _actor(user)
    record.approved_at = timezone.now()
    _stamp(record, user)
    record.save(update_fields=["status", "approved_by", "approved_at", "updated_at", "updated_by"])

    employee = record.employee
    revision = getattr(record, "revision", None)
    recalculate_current_salary(employee, user=user)
    record.refresh_from_db()

    log_history(
        employee,
        HistoryEvent.SALARY_REVISED,
        occurred_on=record.effective_from,
        from_value=_money(revision.previous_amount, record.currency)
        if revision and revision.previous_amount is not None
        else "—",
        to_value=_money(record.total_compensation, record.currency),
        notes=revision.reason if revision else reason,
        user=user,
    )
    log_audit(
        employee,
        AuditAction.SALARY_APPROVED,
        field="salary",
        previous=_money(revision.previous_amount, record.currency)
        if revision and revision.previous_amount is not None
        else "",
        new=_money(record.total_compensation, record.currency),
        reason=reason or (revision.reason if revision else ""),
        notes=f"Effective {record.effective_from:%d %b %Y}.",
        user=user,
    )
    return record


@transaction.atomic
def reject_salary_record(record, *, user=None, reason=""):
    """Turn down a proposed revision.

    The row stays, marked rejected. A raise that was asked for and refused is
    part of the record; deleting it would leave the next reviewer without the
    one fact they most need.
    """
    if record.status not in (SalaryStatus.DRAFT, SalaryStatus.PENDING):
        raise ValidationError(
            f"This record is {record.get_status_display().lower()} and cannot be rejected."
        )
    record.status = SalaryStatus.REJECTED
    _stamp(record, user)
    record.save(update_fields=["status", "updated_at", "updated_by"])
    log_audit(
        record.employee,
        AuditAction.SALARY_REJECTED,
        field="salary",
        new=_money(record.total_compensation, record.currency),
        reason=reason,
        notes=f"Proposed effective {record.effective_from:%d %b %Y}.",
        user=user,
    )
    return record


def apply_due_revisions(company=None):
    """Bring scheduled revisions into force once their date arrives.

    Without this, an increment approved in March for the 1st of April is still
    "scheduled" on the 2nd, and the cached figure the reports read is last
    year's. Run daily (there is a management command); it is idempotent, and
    only touches employees who actually have something due.
    """
    today = timezone.localdate()
    due = EmployeeSalary.objects.filter(
        status=SalaryStatus.SCHEDULED, effective_from__lte=today
    )
    if company is not None:
        due = due.filter(employee__company=company)
    employee_ids = set(due.values_list("employee_id", flat=True))
    applied = 0
    for employee in Employee.objects.filter(id__in=employee_ids):
        recalculate_current_salary(employee)
        applied += 1
    return applied
