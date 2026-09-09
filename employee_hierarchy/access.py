"""
Who may see whose salary.

Salary access is **not** hierarchy access. Being able to open the org chart
reveals no money at all, and being *in* somebody's reporting line grants
nothing either: a developer under the CTO must not see the CTO's pay, and two
peers must never see each other's. Every figure this module returns has passed
through one of the two functions here.

There are four grants, and a viewer's reach is the union of the ones they hold:

===================================  ===============================================
``can_view_own_salary``              their own record, and nothing else
``can_view_subordinate_salary``      anyone below them in the tree, at any depth
``can_view_department_salary``       their own department and any they head,
                                     including sub-departments
``can_view_all_salaries``            everybody (HR, Finance, an administrator)
===================================  ===============================================

Two shapes, because there are two kinds of question:

* :func:`can_view_salary` answers "this one person?" with **no database
  query** -- it compares materialised paths and a small set of department ids.
* :func:`salary_visibility_filter` returns the same rules as a ``Q`` object, so
  a salary-band filter or a distribution report over four thousand employees
  stays one indexed query and never leaks a row it then has to hide.

Both take the *user*, not the employee: the app's identity is the login, and
the two grants that mean "relative to me" need the employee that login is
linked to. A user with no linked employee record therefore has no "own" and no
"subordinates" -- deliberately, because guessing which employee an unlinked
login is would be guessing about somebody's pay.
"""

from django.db.models import Q

from .constants import RecordStatus
from .models import Department, Employee

VIEW_EMPLOYEES = "employee_hierarchy.can_view_employees"
MANAGE_EMPLOYEES = "employee_hierarchy.can_manage_employees"
MANAGE_STRUCTURE = "employee_hierarchy.can_manage_org_structure"
VIEW_REPORTS = "employee_hierarchy.can_view_workforce_reports"
VIEW_AUDIT = "employee_hierarchy.can_view_employee_audit"

VIEW_OWN_SALARY = "employee_hierarchy.can_view_own_salary"
VIEW_SUBORDINATE_SALARY = "employee_hierarchy.can_view_subordinate_salary"
VIEW_DEPARTMENT_SALARY = "employee_hierarchy.can_view_department_salary"
VIEW_ALL_SALARIES = "employee_hierarchy.can_view_all_salaries"
VIEW_SALARY_HISTORY = "employee_hierarchy.can_view_salary_history"
CREATE_SALARY = "employee_hierarchy.can_create_salary"
UPDATE_SALARY = "employee_hierarchy.can_update_salary"
APPROVE_SALARY = "employee_hierarchy.can_approve_salary_revision"

#: Holding any of these reveals the module.
ANY_ACCESS = (VIEW_EMPLOYEES, MANAGE_EMPLOYEES, MANAGE_STRUCTURE, VIEW_REPORTS)

#: Holding any of these means the viewer can see *some* money, so the screens
#: that are entirely about salary are worth showing them.
ANY_SALARY_ACCESS = (
    VIEW_OWN_SALARY,
    VIEW_SUBORDINATE_SALARY,
    VIEW_DEPARTMENT_SALARY,
    VIEW_ALL_SALARIES,
)


def has_any(user, permissions):
    return bool(
        user
        and user.is_authenticated
        and any(user.has_perm(permission) for permission in permissions)
    )


def viewer_employee(user, company=None):
    """The employee record this login is, or ``None``.

    One query, and deliberately **not** memoised on the user object. The
    per-request cache belongs on the request (see :func:`salary_reach`, which
    resolves this once and holds it): a cache on the user instance outlives the
    request in anything that keeps a user around — a management command, a
    background job, a test client — and a stale answer here is a stale answer
    to "whose salary is this person allowed to see", which is the one question
    in this module that must never be answered from memory.
    """
    if not user or not user.is_authenticated:
        return None
    profile = (
        Employee.objects.filter(user=user)
        .select_related("department", "designation", "company")
        .first()
    )
    if company is not None and profile is not None and profile.company_id != company.id:
        # A login can be an employee of one plant and a user of another; their
        # "own salary" grant does not follow them across companies.
        return None
    return profile


def _department_scope(viewer, company):
    """Department ids a ``can_view_department_salary`` holder reaches.

    Their own department plus any they head, and everything nested under
    either -- a Technology head sees Engineering, QA and DevOps, because that
    is what "their department" means to the person saying it.

    Departments are few and change rarely, so the tree is walked in Python from
    one query rather than kept as a path like the employee tree.
    """
    if viewer is None:
        return set()

    seeds = set()
    if viewer.department_id:
        seeds.add(viewer.department_id)
    seeds.update(
        Department.objects.filter(head=viewer, status=RecordStatus.ACTIVE).values_list(
            "id", flat=True
        )
    )
    if not seeds:
        return set()

    children = {}
    for department_id, parent_id in Department.objects.filter(company=company).values_list(
        "id", "parent_id"
    ):
        children.setdefault(parent_id, []).append(department_id)

    reached = set()
    queue = list(seeds)
    while queue:
        department_id = queue.pop()
        if department_id in reached:
            continue
        reached.add(department_id)
        queue.extend(children.get(department_id, []))
    return reached


class SalaryReach:
    """One viewer's salary reach, resolved once and then asked many times.

    Built per request by :func:`salary_reach`. It holds the three cheap facts
    the rules need -- am I anybody, which paths am I above, which departments do
    I reach -- so a list of a hundred employees costs the queries of one.
    """

    def __init__(self, user, company):
        self.user = user
        self.company = company
        self.all = user.has_perm(VIEW_ALL_SALARIES)
        self.own = user.has_perm(VIEW_OWN_SALARY)
        self.subordinates = user.has_perm(VIEW_SUBORDINATE_SALARY)
        self.department = user.has_perm(VIEW_DEPARTMENT_SALARY)
        self.history = user.has_perm(VIEW_SALARY_HISTORY)
        self.viewer = viewer_employee(user, company) if (self.own or self.subordinates or self.department) else None
        self._department_ids = (
            _department_scope(self.viewer, company) if self.department else set()
        )

    @property
    def sees_nothing(self):
        return not (self.all or self.own or self.subordinates or self.department)

    def can_view(self, employee):
        """Whether this viewer may see ``employee``'s pay. No queries."""
        if self.all:
            return True
        viewer = self.viewer
        if viewer is None:
            return False
        if self.own and employee.pk == viewer.pk:
            return True
        if self.subordinates and employee.pk != viewer.pk:
            # Strictly below: the prefix test includes the viewer themselves,
            # which is the "own salary" grant's business, not this one's.
            if (employee.hierarchy_path or "").startswith(viewer.path_prefix):
                return True
        if self.department and employee.department_id in self._department_ids:
            return True
        return False

    def can_view_history(self, employee):
        """Past records, not just the one in force.

        Reading history needs the right to see the person's salary at all AND
        ``can_view_salary_history`` -- except for your own, which you may always
        read in full if you may read it at all. Somebody's salary history is a
        sharper thing than their current figure: it shows how they have been
        treated over years, and a manager cleared to see today's number is not
        automatically cleared to see that.
        """
        if not self.can_view(employee):
            return False
        if self.viewer is not None and employee.pk == self.viewer.pk:
            return True
        return self.history or self.all

    def as_filter(self):
        """The same rules as a ``Q``, or ``None`` when the viewer sees nothing.

        ``Q()`` (matches everything) is returned only for
        ``can_view_all_salaries``; the difference between "everything" and
        "nothing" must never be an empty ``Q``, which is why nothing is
        ``None`` and callers check for it explicitly.
        """
        if self.all:
            return Q()
        clauses = []
        viewer = self.viewer
        if viewer is not None:
            if self.own:
                clauses.append(Q(pk=viewer.pk))
            if self.subordinates:
                clauses.append(
                    Q(hierarchy_path__startswith=viewer.path_prefix) & ~Q(pk=viewer.pk)
                )
            if self.department and self._department_ids:
                clauses.append(Q(department_id__in=self._department_ids))
        if not clauses:
            return None
        combined = clauses[0]
        for clause in clauses[1:]:
            combined |= clause
        return combined


def salary_reach(request):
    """This request's :class:`SalaryReach`, built once and reused."""
    cached = getattr(request, "_salary_reach", None)
    if cached is None:
        company = getattr(getattr(request, "company", None), "company", None)
        cached = SalaryReach(request.user, company)
        request._salary_reach = cached
    return cached


def can_view_salary(request, employee):
    """Shorthand: may this request see this employee's pay?"""
    return salary_reach(request).can_view(employee)


def salary_visibility_filter(request):
    """Shorthand: the ``Q`` for this request's salary reach, or ``None``."""
    return salary_reach(request).as_filter()


def permission_flags(request):
    """What this user may do, for the frontend to render against.

    The screens ask once and then hide what they must: a page that offers a
    "Revise salary" button which 403s is worse than one that never shows it.
    """
    user = request.user
    reach = salary_reach(request)
    viewer = reach.viewer if not reach.sees_nothing else viewer_employee(user)
    return {
        "can_view_employees": user.has_perm(VIEW_EMPLOYEES) or user.has_perm(MANAGE_EMPLOYEES),
        "can_manage_employees": user.has_perm(MANAGE_EMPLOYEES),
        "can_manage_structure": user.has_perm(MANAGE_STRUCTURE),
        "can_view_reports": user.has_perm(VIEW_REPORTS),
        "can_view_audit": user.has_perm(VIEW_AUDIT),
        "salary": {
            "any": not reach.sees_nothing,
            "own": reach.own,
            "subordinates": reach.subordinates,
            "department": reach.department,
            "all": reach.all,
            "history": reach.history or reach.all,
            "create": user.has_perm(CREATE_SALARY),
            "update": user.has_perm(UPDATE_SALARY),
            "approve": user.has_perm(APPROVE_SALARY),
        },
        # Which employee this login is, so the directory can offer "my team"
        # and the profile page can say "this is you".
        "self_employee_id": viewer.id if viewer else None,
        "self_employee_code": viewer.employee_code if viewer else None,
    }
