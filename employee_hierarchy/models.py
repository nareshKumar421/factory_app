"""
Employees, the org they hang in, and what they are paid.

Eight tables, in three groups.

**The org**

* :class:`Department`  -- the tree of departments and sub-departments, each with
  a head. Per company: Oil, Mart and Beverages are different plants.
* :class:`Designation` -- the ladder ("CTO", "Team Lead", "Intern"), each rung
  carrying the organisational level it sits at.

**The people**

* :class:`Employee`    -- the person, their place in the reporting tree, and
  their current department / designation / status.
* :class:`EmployeeHistory` -- their career here, one row per thing that
  happened, in the words the timeline shows.
* :class:`EmployeeAuditLog` -- who changed what, from what, to what, and why.

**The money**

* :class:`EmployeeSalary`  -- one row per salary that has ever been in force.
  Written once, never edited; a revision *adds* a row.
* :class:`SalaryRevision` -- the story of one change: previous, new, why, who
  approved it.
* :class:`EmployeePermission` -- the sentinel carrying the module's rights,
  including the salary ones, which are separate from everything else.

Two design decisions are worth knowing before reading further.

**The reporting tree is a materialised path.** Every employee stores
``hierarchy_path`` -- ``"/1/4/9/"`` for employee 9 reporting to 4 reporting to
1. It makes the three questions this module is built to answer single indexed
queries rather than recursion: everyone under a manager is one
``startswith``, the chain to the top is one ``IN``, and "would this create a
cycle?" is one string comparison. Moving a manager rewrites their subtree's
paths in one ``UPDATE``. See :mod:`employee_hierarchy.hierarchy`.

**Nothing here inherits ``gate_core.BaseModel``.** That base carries an
``is_active`` boolean, and every model in this module already has a *domain*
status -- employment status, salary status, record status -- which is the real
answer to "is this live?". A second boolean beside it would be a competing
answer, and the day the two disagree nobody could say which one payroll should
believe. :class:`Stamped` below gives the same created / updated / by columns
without it.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from company.models import Company

from .constants import (
    APPROVED_SALARY_STATUSES,
    DEFAULT_CURRENCY,
    EXIT_STATUSES,
    MANAGER_ELIGIBLE_STATUSES,
    MAX_EMPLOYEE_CODE,
    MAX_NAME,
    AuditAction,
    EmploymentStatus,
    HistoryEvent,
    RecordStatus,
    RevisionType,
    SalaryStatus,
)


class Stamped(models.Model):
    """Created / updated timestamps and the users behind them.

    The repo's ``BaseModel`` minus its ``is_active`` flag -- see the module
    docstring for why this module cannot carry that flag.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_updated",
    )

    class Meta:
        abstract = True


class EmployeePermission(models.Model):
    """Sentinel model carrying the module's permissions (no table of its own).

    Two families, and keeping them apart is the point of this module's access
    control.

    The **directory** rights (``can_view_employees``, ``can_manage_employees``,
    ``can_manage_org_structure``) are about people and structure: names, teams,
    who reports to whom. They are meant to be handed out widely -- an org chart
    nobody can open is a picture on a wall again.

    The **salary** rights are separate, narrower, and additive. Holding
    ``can_view_employees`` reveals no money at all. Seeing a figure needs one of
    ``can_view_own_salary`` (yours), ``can_view_subordinate_salary`` (anyone
    below you in the tree), ``can_view_department_salary`` (your department, or
    one you head) or ``can_view_all_salaries`` (HR / Finance). Being *in*
    somebody's reporting line grants nothing on its own: a developer who happens
    to sit under the CTO must not see the CTO's pay, and a peer must never see a
    peer's.

    Writing money is split again, because proposing a raise and approving one
    are different jobs: ``can_create_salary`` / ``can_update_salary`` draft a
    revision, and ``can_approve_salary_revision`` is what puts it in force.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Employee Hierarchy & Salary"
        verbose_name_plural = "Employee Hierarchy & Salary"
        permissions = [
            # --- the directory and the tree -------------------------------
            ("can_view_employees", "Can view employees and the org chart"),
            ("can_manage_employees", "Can add and edit employees, and move them in the tree"),
            ("can_manage_org_structure", "Can maintain departments and designations"),
            ("can_view_workforce_reports", "Can view headcount and org reports"),
            ("can_view_employee_audit", "Can view the employee audit trail"),
            # --- money ----------------------------------------------------
            ("can_view_own_salary", "Can view their own salary"),
            ("can_view_subordinate_salary", "Can view the salary of anyone below them"),
            ("can_view_department_salary", "Can view salaries within their department"),
            ("can_view_all_salaries", "Can view every employee's salary"),
            ("can_view_salary_history", "Can view past salary records, not just the current one"),
            ("can_create_salary", "Can create a salary record"),
            ("can_update_salary", "Can draft a salary revision"),
            ("can_approve_salary_revision", "Can approve a salary revision"),
        ]


class Department(Stamped):
    """One department, possibly inside another.

    A tree, because that is how the company is drawn: Technology contains
    Engineering, QA and DevOps. ``parent`` is the only structural field --
    depth is read from it rather than stored, since departments are few and
    restructured rarely (unlike employees, which is why *they* keep a path).

    ``head`` is an :class:`Employee`, not free text. This is the master payroll
    and reporting run off, so the head has to be a real person in the
    directory; the wall chart that names "Shunty Veerji" as a section owner is
    a different, deliberately looser thing (see the ``org_chart`` app).
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="hr_departments"
    )
    code = models.CharField(
        max_length=30, help_text="Short handle used in search and filters, e.g. 'ENG'."
    )
    name = models.CharField(max_length=MAX_NAME)
    description = models.TextField(blank=True, default="")
    head = models.ForeignKey(
        "Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="heads_departments",
        help_text="Who runs the department. Blank until somebody is named.",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        help_text="The department this one sits inside. Blank for a top-level department.",
    )
    status = models.CharField(
        max_length=20, choices=RecordStatus.choices, default=RecordStatus.ACTIVE
    )
    sort_order = models.PositiveIntegerField(
        default=0, help_text="Position among its siblings on the department tree."
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Department"
        verbose_name_plural = "Departments"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_hr_department_code_per_company"
            ),
            models.UniqueConstraint(
                fields=["company", "name"], name="uniq_hr_department_name_per_company"
            ),
        ]
        indexes = [models.Index(fields=["company", "status"])]

    def __str__(self):
        return self.name

    @property
    def is_live(self):
        return self.status == RecordStatus.ACTIVE


class Designation(Stamped):
    """One rung of the ladder.

    ``level`` is the rung's height, 1 at the top (CEO) and rising as you go
    down. It is what makes "promote" mean something the system can check, and it
    seeds a new employee's hierarchy level -- but it is NOT the same number as
    :attr:`Employee.hierarchy_level`, which counts actual reporting hops. A
    Senior Developer reporting to a Team Lead who reports to the CEO sits at
    hierarchy level 3 whatever the ladder says; the ladder is the *grade*, the
    tree is the *chain*. Reports that group "by level" mean the ladder; reports
    that walk the org mean the chain.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="hr_designations"
    )
    name = models.CharField(max_length=MAX_NAME)
    code = models.CharField(max_length=30, help_text="Short handle, e.g. 'SR_DEV'.")
    description = models.TextField(blank=True, default="")
    level = models.PositiveSmallIntegerField(
        default=5,
        help_text="Organisational level: 1 is the top of the ladder (CEO), higher is lower down.",
    )
    is_managerial = models.BooleanField(
        default=False,
        help_text="Whether this rung is expected to manage people. Seeds the employee's manager flag.",
    )
    status = models.CharField(
        max_length=20, choices=RecordStatus.choices, default=RecordStatus.ACTIVE
    )

    class Meta:
        ordering = ["level", "name"]
        verbose_name = "Designation"
        verbose_name_plural = "Designations"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_designation_code_per_company"
            ),
            models.UniqueConstraint(
                fields=["company", "name"], name="uniq_designation_name_per_company"
            ),
        ]
        indexes = [models.Index(fields=["company", "level"])]

    def __str__(self):
        return self.name

    @property
    def is_live(self):
        return self.status == RecordStatus.ACTIVE


class Employee(Stamped):
    """A person, and where they stand.

    ``reporting_manager`` is the single structural fact: one manager, any number
    of reports. Everything else about the tree -- ``hierarchy_path``,
    ``hierarchy_level`` -- is *derived* from it and maintained by
    :mod:`employee_hierarchy.hierarchy`, never set by a caller. They are stored
    rather than computed because the questions asked of them ("everyone under
    the CTO", 4 000 rows deep) have to be one query.

    Two fields look like duplicates and are not:

    ``full_name`` is kept alongside first / last because it is what every
    screen, search box and report prints, and one indexed column beats
    concatenating on every query. It is rebuilt in :meth:`save`, so it can never
    drift.

    ``is_manager`` is a *declaration* -- this role manages people -- while the
    direct-report count is an *observation*, annotated when needed. The service
    turns the flag on the moment somebody gains their first report, because that
    proves it; it never turns it off, because a manager whose team was just
    reassigned has not stopped being a manager.

    ``current_salary_amount`` is a cache of the active salary record, kept by
    :func:`employee_hierarchy.services.recalculate_current_salary`. It exists so
    that filtering a directory by salary band, or plotting a distribution across
    thousands of employees, is an indexed scan instead of a per-row lookup. It
    is never the source of truth: :class:`EmployeeSalary` is, and the API reads
    the figure from there. Nothing about it is visible without a salary right.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="employees"
    )
    employee_code = models.CharField(
        max_length=MAX_EMPLOYEE_CODE, help_text="The code people quote, e.g. 'EMP001'."
    )

    first_name = models.CharField(max_length=MAX_NAME)
    last_name = models.CharField(max_length=MAX_NAME, blank=True, default="")
    full_name = models.CharField(
        max_length=2 * MAX_NAME + 1,
        blank=True,
        help_text="Rebuilt from first / last on save. Do not set it directly.",
    )

    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    photo = models.ImageField(
        upload_to="employees/photos/",
        null=True,
        blank=True,
        help_text="Profile photo. The org chart falls back to initials without one.",
    )

    date_of_birth = models.DateField(null=True, blank=True)
    joining_date = models.DateField(default=timezone.localdate)
    exit_date = models.DateField(
        null=True,
        blank=True,
        help_text="Last working day. Set when the status becomes an exit; drives turnover reporting.",
    )

    employment_status = models.CharField(
        max_length=20,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
        db_index=True,
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="employees",
    )
    designation = models.ForeignKey(
        Designation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="employees",
    )
    job_title = models.CharField(
        max_length=MAX_NAME,
        blank=True,
        default="",
        help_text="What the business card says. The designation is the grade; this is the job.",
    )
    location = models.CharField(max_length=MAX_NAME, blank=True, default="")

    reporting_manager = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="direct_reports",
        help_text="The one manager. Blank for a top-level employee such as the CEO.",
    )
    hierarchy_level = models.PositiveSmallIntegerField(
        default=1, help_text="Reporting hops from the top, 1 for a top-level employee. Derived."
    )
    hierarchy_path = models.CharField(
        max_length=512,
        blank=True,
        db_index=True,
        help_text="Materialised path of ids ending in their own, e.g. '/1/4/9/'. Derived.",
    )
    is_manager = models.BooleanField(
        default=False, help_text="Whether this person is expected to manage people."
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_profile",
        help_text="The app login this employee is. Required for them to see their own salary.",
    )

    current_salary_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cache of the active salary record's total. Never the source of truth.",
    )
    current_salary_currency = models.CharField(
        max_length=3, default=DEFAULT_CURRENCY, blank=True
    )

    class Meta:
        ordering = ["hierarchy_level", "full_name"]
        verbose_name = "Employee"
        verbose_name_plural = "Employees"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "employee_code"], name="uniq_employee_code_per_company"
            ),
            # Emails are unique where given; plenty of shop-floor employees have
            # none, and blank must not collide with blank.
            models.UniqueConstraint(
                fields=["company", "email"],
                condition=~Q(email=""),
                name="uniq_employee_email_per_company",
            ),
            # The first hierarchy rule, enforced by the database and not only by
            # the service: nobody reports to themselves.
            models.CheckConstraint(
                condition=~Q(reporting_manager=F("id")), name="employee_not_own_manager"
            ),
            models.CheckConstraint(
                condition=Q(hierarchy_level__gte=1), name="employee_level_at_least_one"
            ),
        ]
        indexes = [
            models.Index(fields=["company", "employment_status"]),
            models.Index(fields=["company", "department"]),
            models.Index(fields=["company", "designation"]),
            models.Index(fields=["company", "reporting_manager"]),
            models.Index(fields=["company", "hierarchy_level"]),
            models.Index(fields=["company", "joining_date"]),
            # The salary-band filter and the distribution report.
            models.Index(fields=["company", "current_salary_amount"]),
            models.Index(fields=["full_name"]),
            models.Index(fields=["employee_code"]),
        ]

    def __str__(self):
        return f"{self.employee_code} – {self.full_name}"

    def save(self, *args, **kwargs):
        self.full_name = " ".join(part for part in (self.first_name, self.last_name) if part)
        super().save(*args, **kwargs)

    # -- the tree ------------------------------------------------------------

    @property
    def is_top_level(self):
        """Nobody above them -- a CEO, or the head of a separate unit."""
        return self.reporting_manager_id is None

    @property
    def path_prefix(self):
        """The prefix every descendant's path starts with, ``'/1/4/9/'``."""
        return self.hierarchy_path or f"/{self.pk}/"

    @property
    def ancestor_ids(self):
        """Ids of everyone above them, top-most first. Read straight off the path."""
        parts = [segment for segment in (self.hierarchy_path or "").split("/") if segment]
        return [int(segment) for segment in parts[:-1]]

    @property
    def can_manage_others(self):
        """Whether they may be handed a team right now.

        Status, not seniority: a suspended director cannot hold one, an intern
        on probation can. The one place this is decided.
        """
        return self.employment_status in MANAGER_ELIGIBLE_STATUSES

    @property
    def has_left(self):
        return self.employment_status in EXIT_STATUSES

    @property
    def initials(self):
        """Fallback for the org chart when there is no photo."""
        letters = [part[0] for part in self.full_name.split() if part][:2]
        return "".join(letters).upper() or "?"


class EmployeeSalary(Stamped):
    """One salary, in force from ``effective_from`` until the next one starts.

    **Append-only.** A revision never edits the previous row -- it inserts a new
    one and marks the old one superseded, which is what makes the history in the
    brief ("2024: 5,00,000 / 2025: 6,00,000 / 2026: 7,20,000") a set of facts
    rather than an audit reconstruction. The amounts on an approved row are
    immutable; the service refuses to change them.

    ``total_compensation`` is stored, not computed on read, for the same reason
    the tree stores a path: salary distribution across thousands of employees
    has to be one indexed aggregate. :meth:`save` recomputes it, so it cannot
    drift from its components.

    Amounts are annual, in ``currency``. Deductions are subtracted -- the total
    is what the company hands over, which is the figure a revision is about.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="salary_records"
    )

    basic_salary = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    allowances = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bonuses = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_compensation = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text="basic + allowances + bonuses − deductions. Recomputed on save.",
    )
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)

    effective_from = models.DateField(help_text="The day this salary starts being the one paid.")
    revision_date = models.DateField(
        default=timezone.localdate,
        help_text="The day the decision was taken, which is often months earlier.",
    )
    status = models.CharField(
        max_length=20, choices=SalaryStatus.choices, default=SalaryStatus.DRAFT, db_index=True
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_salaries",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-effective_from", "-id"]
        verbose_name = "Salary record"
        verbose_name_plural = "Salary records"
        constraints = [
            # One salary per start date per person: two rows starting the same
            # day cannot both be "the" salary, and the pair is almost always a
            # double-submit.
            models.UniqueConstraint(
                fields=["employee", "effective_from"], name="uniq_salary_per_effective_date"
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["status", "effective_from"]),
        ]

    def __str__(self):
        return f"{self.employee.employee_code} – {self.currency} {self.total_compensation}"

    def save(self, *args, **kwargs):
        self.total_compensation = (
            (self.basic_salary or 0)
            + (self.allowances or 0)
            + (self.bonuses or 0)
            - (self.deductions or 0)
        )
        super().save(*args, **kwargs)

    @property
    def is_approved(self):
        return self.status in APPROVED_SALARY_STATUSES

    @property
    def is_editable(self):
        """Only an unapproved draft may still be changed."""
        return self.status in {SalaryStatus.DRAFT, SalaryStatus.PENDING}


class SalaryRevision(Stamped):
    """Why one salary became another.

    Sits beside the record it created: the record is the *amount*, the revision
    is the *decision* -- previous, new, why, what kind, who signed it. Kept
    separate so the revision-history report can be read without touching the
    figures, and so a rejected proposal still leaves a trace of having been
    asked for.

    ``approved_by`` deliberately reads through to the salary record rather than
    storing a second copy: approving the record *is* approving the revision, and
    two columns could disagree.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="salary_revisions"
    )
    salary_record = models.OneToOneField(
        EmployeeSalary,
        on_delete=models.CASCADE,
        related_name="revision",
        help_text="The record this revision put in place.",
    )

    previous_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total compensation before. Blank for a joining salary.",
    )
    new_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)

    effective_date = models.DateField()
    revision_date = models.DateField(default=timezone.localdate)

    revision_type = models.CharField(
        max_length=30, choices=RevisionType.choices, default=RevisionType.ANNUAL_INCREMENT
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-effective_date", "-id"]
        verbose_name = "Salary revision"
        verbose_name_plural = "Salary revisions"
        indexes = [
            models.Index(fields=["employee", "effective_date"]),
            models.Index(fields=["revision_type"]),
        ]

    def __str__(self):
        return f"{self.employee.employee_code} – {self.get_revision_type_display()}"

    @property
    def approved_by(self):
        """Who approved it -- read from the record, which is the thing approved."""
        return self.salary_record.approved_by

    @property
    def approved_at(self):
        return self.salary_record.approved_at

    @property
    def change_amount(self):
        if self.previous_amount is None:
            return None
        return self.new_amount - self.previous_amount

    @property
    def change_percent(self):
        if not self.previous_amount:
            return None
        return (self.new_amount - self.previous_amount) / self.previous_amount * 100


class EmployeeHistory(Stamped):
    """One thing that happened to one employee, as the timeline says it.

    Written by the services, never by a client. ``from_value`` / ``to_value``
    are the human strings the timeline prints ("Engineering" → "DevOps") rather
    than ids, so a department renamed in 2027 does not rewrite what happened in
    2025 -- history has to keep saying what was true at the time.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="history_entries"
    )
    event = models.CharField(max_length=30, choices=HistoryEvent.choices, db_index=True)
    occurred_on = models.DateField(
        default=timezone.localdate,
        help_text="The day it took effect, which may differ from when it was recorded.",
    )
    from_value = models.CharField(max_length=255, blank=True, default="")
    to_value = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-occurred_on", "-id"]
        verbose_name = "Employee history entry"
        verbose_name_plural = "Employee history"
        indexes = [models.Index(fields=["employee", "occurred_on"])]

    def __str__(self):
        return f"{self.employee.employee_code} – {self.get_event_display()}"


class EmployeeAuditLog(models.Model):
    """Who changed what, from what, to what, and why.

    Not :class:`Stamped`: an audit row is created once and never updated, so a
    single ``performed_at`` / ``performed_by`` pair is the honest shape --
    ``updated_by`` on an audit row would be a contradiction. Rows are never
    edited or deleted, which is the whole point of having them.

    It overlaps with :class:`EmployeeHistory` on purpose. History is the
    employee's story, shown to anyone who may see the employee; the audit trail
    is the record of *administrative acts*, gated behind
    ``can_view_employee_audit``, and it also carries the acts that are not part
    of anyone's career -- including who looked at a salary.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="audit_logs"
    )
    action = models.CharField(max_length=30, choices=AuditAction.choices, db_index=True)
    field = models.CharField(
        max_length=60,
        blank=True,
        default="",
        help_text="Which attribute moved, e.g. 'reporting_manager'.",
    )
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_audit_entries",
    )
    performed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-performed_at", "-id"]
        verbose_name = "Employee audit entry"
        verbose_name_plural = "Employee audit trail"
        indexes = [
            models.Index(fields=["employee", "-performed_at"]),
            models.Index(fields=["action", "-performed_at"]),
        ]

    def __str__(self):
        return f"{self.employee_id} – {self.action} @ {self.performed_at:%Y-%m-%d %H:%M}"
