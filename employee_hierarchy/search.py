"""
The employee directory's filters, parsed and applied.

Kept out of the view for the reason the ``issues`` app keeps its own search
apart: what the query params *mean* is worth testing without a request, and the
place where filters are turned into SQL is the place a privacy mistake would
hide. Everything the brief asks to filter by is here -- department (with its
sub-departments), designation, manager (their whole organisation, not just
their direct reports), hierarchy level, employment status, joining window, and
salary band.

The salary band is the one filter with teeth. Filtering by pay is a way of
*reading* pay -- "show me everyone over 20 lakh" reveals who is over 20 lakh --
so it is refused outright to a viewer with no salary right, and for everybody
else it is intersected with exactly the rows they are allowed to see. A manager
cleared for their own team can filter their own team by band and gets nothing
from the rest of the company, not even a count.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from .constants import EmploymentStatus, IN_SERVICE_STATUSES
from .models import Department

#: ``sort`` values the directory accepts, mapped to real ordering.
#:
#: Salary is here but is checked before use -- see :func:`apply_filters` -- for
#: the same reason the band filter is: ordering by pay is reading pay.
SORT_FIELDS = {
    "name": ("full_name",),
    "name-desc": ("-full_name",),
    "code": ("employee_code",),
    "code-desc": ("-employee_code",),
    "joined": ("-joining_date", "full_name"),
    "joined-asc": ("joining_date", "full_name"),
    "level": ("hierarchy_level", "full_name"),
    "level-desc": ("-hierarchy_level", "full_name"),
    "department": ("department__name", "full_name"),
    "designation": ("designation__level", "full_name"),
    "salary": ("-current_salary_amount", "full_name"),
    "salary-asc": ("current_salary_amount", "full_name"),
}

DEFAULT_SORT = "name"

#: Sorts that expose pay and therefore need a salary right.
SALARY_SORTS = frozenset({"salary", "salary-asc"})


def _decimal(raw):
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _flag(raw):
    if raw in (None, ""):
        return None
    return str(raw).lower() in {"1", "true", "yes", "on"}


@dataclass
class EmployeeFilters:
    """One directory query, parsed. Pure data -- no queryset, no request."""

    text: str = ""
    departments: list = field(default_factory=list)
    include_sub_departments: bool = True
    designations: list = field(default_factory=list)
    manager: int | None = None
    #: ``True`` -- everyone under that manager at any depth; ``False`` -- only
    #: their direct reports. The org chart wants the first, a team list the
    #: second, and they are different questions.
    manager_deep: bool = True
    statuses: list = field(default_factory=list)
    levels: list = field(default_factory=list)
    location: str = ""
    managers_only: bool | None = None
    top_level_only: bool = False
    joined_from: date | None = None
    joined_to: date | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    sort: str = DEFAULT_SORT
    in_service_only: bool = True

    @property
    def wants_salary(self):
        return self.salary_min is not None or self.salary_max is not None or self.sort in SALARY_SORTS


def parse_filters(request):
    """Read the query string into an :class:`EmployeeFilters`.

    Unparseable values are dropped rather than rejected: a half-typed date in a
    filter bar should not turn the page into an error, and every dropped value
    is one the client can see was not applied.
    """
    params = request.GET
    statuses = [
        value
        for value in params.getlist("status")
        if value in EmploymentStatus.values
    ]
    sort = params.get("sort") or DEFAULT_SORT
    if sort not in SORT_FIELDS:
        sort = DEFAULT_SORT

    return EmployeeFilters(
        text=(params.get("q") or "").strip(),
        departments=[value for value in map(_int, params.getlist("department")) if value],
        include_sub_departments=_flag(params.get("include_sub_departments")) is not False,
        designations=[value for value in map(_int, params.getlist("designation")) if value],
        manager=_int(params.get("manager")),
        manager_deep=_flag(params.get("manager_deep")) is not False,
        statuses=statuses,
        levels=[value for value in map(_int, params.getlist("level")) if value],
        location=(params.get("location") or "").strip(),
        managers_only=_flag(params.get("managers_only")),
        top_level_only=bool(_flag(params.get("top_level_only"))),
        joined_from=_date(params.get("joined_from")),
        joined_to=_date(params.get("joined_to")),
        salary_min=_decimal(params.get("salary_min")),
        salary_max=_decimal(params.get("salary_max")),
        sort=sort,
        # Default to people who are still here. A directory that opens showing
        # everyone who ever worked here is not a directory.
        in_service_only=_flag(params.get("include_past")) is not True and not statuses,
    )


def _text_clause(text):
    """Free text against the four things people actually type.

    Code, name, email, phone -- and the code match is a prefix rather than a
    contains, because "EMP1" should find EMP1, EMP10, EMP100 and not everybody
    whose code happens to contain the digits.
    """
    return (
        Q(employee_code__istartswith=text)
        | Q(full_name__icontains=text)
        | Q(email__icontains=text)
        | Q(phone__icontains=text)
        | Q(job_title__icontains=text)
    )


def _department_ids(company, department_ids):
    """The chosen departments plus everything nested under them."""
    children = {}
    for row_id, parent_id in Department.objects.filter(company=company).values_list(
        "id", "parent_id"
    ):
        children.setdefault(parent_id, []).append(row_id)
    reached = set()
    queue = list(department_ids)
    while queue:
        row_id = queue.pop()
        if row_id in reached:
            continue
        reached.add(row_id)
        queue.extend(children.get(row_id, []))
    return reached


def apply_filters(queryset, filters, *, company, salary_scope=None, manager_path=None):
    """Narrow ``queryset`` by ``filters`` and return it, ordered.

    ``salary_scope`` is the ``Q`` from
    :func:`employee_hierarchy.access.salary_visibility_filter` -- ``None`` when
    the viewer may see no pay at all. It is consulted only if the query touches
    money, and when it does, the band is intersected with it so no row outside
    the viewer's reach can be inferred from the result, not even by counting.

    ``manager_path`` is the chosen manager's materialised path, looked up by the
    caller (which already has the row) so this function stays free of queries
    it cannot see.
    """
    if filters.text:
        queryset = queryset.filter(_text_clause(filters.text))

    if filters.departments:
        ids = (
            _department_ids(company, filters.departments)
            if filters.include_sub_departments
            else set(filters.departments)
        )
        queryset = queryset.filter(department_id__in=ids)

    if filters.designations:
        queryset = queryset.filter(designation_id__in=filters.designations)

    if filters.manager:
        if filters.manager_deep and manager_path:
            queryset = queryset.filter(hierarchy_path__startswith=manager_path).exclude(
                pk=filters.manager
            )
        else:
            queryset = queryset.filter(reporting_manager_id=filters.manager)

    if filters.statuses:
        queryset = queryset.filter(employment_status__in=filters.statuses)
    elif filters.in_service_only:
        queryset = queryset.filter(employment_status__in=IN_SERVICE_STATUSES)

    if filters.levels:
        queryset = queryset.filter(hierarchy_level__in=filters.levels)

    if filters.location:
        queryset = queryset.filter(location__icontains=filters.location)

    if filters.managers_only is True:
        queryset = queryset.filter(is_manager=True)
    elif filters.managers_only is False:
        queryset = queryset.filter(is_manager=False)

    if filters.top_level_only:
        queryset = queryset.filter(reporting_manager__isnull=True)

    if filters.joined_from:
        queryset = queryset.filter(joining_date__gte=filters.joined_from)
    if filters.joined_to:
        queryset = queryset.filter(joining_date__lte=filters.joined_to)

    if filters.wants_salary:
        if salary_scope is None:
            raise PermissionDenied(
                "Filtering or sorting employees by salary needs salary access."
            )
        queryset = queryset.filter(salary_scope)
        if filters.salary_min is not None:
            queryset = queryset.filter(current_salary_amount__gte=filters.salary_min)
        if filters.salary_max is not None:
            queryset = queryset.filter(current_salary_amount__lte=filters.salary_max)

    return queryset.order_by(*SORT_FIELDS[filters.sort])
