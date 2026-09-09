"""
The module's fixed vocabularies: statuses, revision types, history events.

Everything here is a ``TextChoices`` rather than a master table because these
are *rules*, not data: adding a ninth employment status changes how the
hierarchy behaves (whether that person may still hold a team), so it belongs in
code that can be reviewed, not in a row somebody edits on a Friday evening.

The two groupings at the bottom -- :data:`MANAGER_ELIGIBLE_STATUSES` and
:data:`IN_SERVICE_STATUSES` -- are the ones the hierarchy actually consults, and
are the reason the statuses are an enum: "can this person be somebody's
manager?" has to have one answer, in one place.
"""

from django.db import models

#: How deep the reporting chain may go. A materialised path is a string, so it
#: has to have a bound; twenty levels is far past any real org (Amazon runs
#: about twelve) and keeps the column comfortably short.
MAX_HIERARCHY_DEPTH = 20

#: Employee code / name column widths, shared by the models and serializers.
MAX_EMPLOYEE_CODE = 40
MAX_NAME = 100

#: Default currency for compensation. Overridable per salary record.
DEFAULT_CURRENCY = "INR"


class EmploymentStatus(models.TextChoices):
    """Where an employee is in their life at the company.

    The split that matters to the hierarchy is not "good / bad" but *in service
    or gone*: someone on leave or on probation still holds their team, someone
    who resigned does not.
    """

    ACTIVE = "ACTIVE", "Active"
    PROBATION = "PROBATION", "Probation"
    ON_LEAVE = "ON_LEAVE", "On Leave"
    SUSPENDED = "SUSPENDED", "Suspended"
    INACTIVE = "INACTIVE", "Inactive"
    RESIGNED = "RESIGNED", "Resigned"
    TERMINATED = "TERMINATED", "Terminated"
    RETIRED = "RETIRED", "Retired"


#: Statuses a person may be somebody's reporting manager in.
#:
#: Probation and leave are in: a new manager still on probation is running the
#: team from day one, and a manager on two weeks' leave has not stopped being
#: the manager. Suspended is deliberately OUT -- a suspension is precisely the
#: moment the team needs to report somewhere else -- and so is every exit
#: status. :func:`employee_hierarchy.services.assign_manager` enforces this.
MANAGER_ELIGIBLE_STATUSES = frozenset(
    {
        EmploymentStatus.ACTIVE,
        EmploymentStatus.PROBATION,
        EmploymentStatus.ON_LEAVE,
    }
)

#: Statuses that count as "still employed" for headcount and the default
#: directory filter. Suspended is in -- they are on the payroll -- even though
#: they may not hold a team.
IN_SERVICE_STATUSES = frozenset(
    {
        EmploymentStatus.ACTIVE,
        EmploymentStatus.PROBATION,
        EmploymentStatus.ON_LEAVE,
        EmploymentStatus.SUSPENDED,
    }
)

#: Statuses that mean the person has left. Reaching one of these detaches their
#: team (see :func:`employee_hierarchy.services.change_status`) so nobody is
#: left reporting to somebody who no longer works here.
EXIT_STATUSES = frozenset(
    {
        EmploymentStatus.INACTIVE,
        EmploymentStatus.RESIGNED,
        EmploymentStatus.TERMINATED,
        EmploymentStatus.RETIRED,
    }
)


class SalaryStatus(models.TextChoices):
    """The life of one salary record.

    A record is written once and then only changes *status* -- never amount.
    ``SCHEDULED`` is what makes a future-dated revision honest: the April
    increment is approved in March and is a real, visible record before it is
    the one being paid.
    """

    DRAFT = "DRAFT", "Draft"
    PENDING = "PENDING", "Pending Approval"
    SCHEDULED = "SCHEDULED", "Approved – starts later"
    ACTIVE = "ACTIVE", "Active"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    REJECTED = "REJECTED", "Rejected"


#: Statuses of a record that has been approved -- the ones that may ever be the
#: salary someone is actually paid.
APPROVED_SALARY_STATUSES = frozenset(
    {SalaryStatus.SCHEDULED, SalaryStatus.ACTIVE, SalaryStatus.SUPERSEDED}
)


class RevisionType(models.TextChoices):
    """Why the money changed. Reported on, so it is an enum and not free text."""

    ANNUAL_INCREMENT = "ANNUAL_INCREMENT", "Annual Increment"
    PROMOTION = "PROMOTION", "Promotion"
    PERFORMANCE = "PERFORMANCE", "Performance Revision"
    ROLE_CHANGE = "ROLE_CHANGE", "Role Change"
    DEPARTMENT_TRANSFER = "DEPARTMENT_TRANSFER", "Department Transfer"
    MARKET_ADJUSTMENT = "MARKET_ADJUSTMENT", "Market Adjustment"
    BONUS = "BONUS", "Bonus"
    INITIAL = "INITIAL", "Joining Salary"
    OTHER = "OTHER", "Other"


class HistoryEvent(models.TextChoices):
    """What an employee's career timeline is made of.

    One row per thing that happened, in the words the timeline shows. Kept
    separate from :class:`AuditAction`: this is the story of a person, that is
    the record of who touched what.
    """

    JOINED = "JOINED", "Joined"
    MANAGER_CHANGED = "MANAGER_CHANGED", "Reporting manager changed"
    MANAGER_REMOVED = "MANAGER_REMOVED", "Reporting manager removed"
    DEPARTMENT_CHANGED = "DEPARTMENT_CHANGED", "Moved department"
    DESIGNATION_CHANGED = "DESIGNATION_CHANGED", "Designation changed"
    PROMOTED = "PROMOTED", "Promoted"
    LEVEL_CHANGED = "LEVEL_CHANGED", "Hierarchy level changed"
    LOCATION_CHANGED = "LOCATION_CHANGED", "Location changed"
    SALARY_REVISED = "SALARY_REVISED", "Salary revised"
    STATUS_CHANGED = "STATUS_CHANGED", "Employment status changed"
    TEAM_MOVED = "TEAM_MOVED", "Team moved with them"
    EXITED = "EXITED", "Left the company"


class AuditAction(models.TextChoices):
    """What was done, for the audit trail.

    Deliberately verb-shaped and coarse: the trail answers "who changed this
    person's manager, from what, to what, and why", and one action per kind of
    change is enough to ask that question.
    """

    EMPLOYEE_CREATED = "EMPLOYEE_CREATED", "Employee created"
    EMPLOYEE_UPDATED = "EMPLOYEE_UPDATED", "Employee details updated"
    MANAGER_CHANGED = "MANAGER_CHANGED", "Reporting manager changed"
    MANAGER_REMOVED = "MANAGER_REMOVED", "Reporting manager removed"
    SUBTREE_MOVED = "SUBTREE_MOVED", "Team moved under a new manager"
    DEPARTMENT_CHANGED = "DEPARTMENT_CHANGED", "Department changed"
    DESIGNATION_CHANGED = "DESIGNATION_CHANGED", "Designation changed"
    PROMOTED = "PROMOTED", "Promoted"
    STATUS_CHANGED = "STATUS_CHANGED", "Employment status changed"
    SALARY_CREATED = "SALARY_CREATED", "Salary record created"
    SALARY_APPROVED = "SALARY_APPROVED", "Salary revision approved"
    SALARY_REJECTED = "SALARY_REJECTED", "Salary revision rejected"
    SALARY_VIEWED = "SALARY_VIEWED", "Salary information viewed"


class RecordStatus(models.TextChoices):
    """Master-row status for departments and designations.

    Retired rather than deleted: a department that closed still has to name
    itself in last year's history, so nothing is ever removed once employees
    have hung off it.
    """

    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
