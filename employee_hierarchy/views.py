"""
API for the employee hierarchy and compensation module.

Endpoints, all under ``/api/v1/employee-hierarchy/`` and all scoped to the
company in the ``Company-Code`` header::

    GET    meta/                              masters, choice lists, my rights
    GET    employees/                          the directory (see search.py for filters)
    POST   employees/                          hire somebody
    GET    employees/<id>/                     one employee
    PATCH  employees/<id>/                     edit their plain details
    GET    employees/<id>/reporting/           manager, chain, reports, peers, subordinates
    GET    employees/<id>/history/             their career here
    GET    employees/<id>/audit/               who changed what about them
    POST   employees/<id>/manager/             move them in the tree (team follows)
    POST   employees/<id>/department/          transfer them
    POST   employees/<id>/designation/         change their rung
    POST   employees/<id>/promote/             promotion: rung + manager + money, one act
    POST   employees/<id>/status/              probation, leave, resignation, exit
    GET    employees/<id>/salary/              current + history, per the salary rules
    POST   employees/<id>/salary/              propose a revision
    POST   salary-records/<id>/approve/        put it in force
    POST   salary-records/<id>/reject/         turn it down
    GET    salary-approvals/                   what is waiting for an approver
    GET    salary-revisions/                   the revision-history report
    GET    tree/                               the org chart
    GET/POST         departments/              the department master
    PATCH/DELETE     departments/<id>/
    GET/POST         designations/             the designation master
    PATCH/DELETE     designations/<id>/
    GET    reports/                            headcount and salary reporting

Three things hold across all of them.

**The company header decides the scope.** Every lookup is filtered by it, so a
URL carrying another plant's employee id is a 404 rather than a leak.

**Writes go to the services.** The views validate and translate; the business
act belongs to :mod:`employee_hierarchy.services`, which is also what writes
the history and audit rows.

**Salary is checked per employee, not per endpoint.** Getting past
:class:`~employee_hierarchy.permissions.CanAccessSalary` only means the viewer
may see *some* pay. Whose, is
:class:`~employee_hierarchy.access.SalaryReach`'s answer, asked again for every
employee in every response.
"""

from dataclasses import replace
from datetime import timedelta

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from grpo.pagination import build_page, get_page_params, paginate_queryset

from . import hierarchy, services
from .access import (
    APPROVE_SALARY,
    CREATE_SALARY,
    UPDATE_SALARY,
    permission_flags,
    salary_reach,
)
from .constants import (
    IN_SERVICE_STATUSES,
    AuditAction,
    EmploymentStatus,
    HistoryEvent,
    RecordStatus,
    RevisionType,
    SalaryStatus,
)
from .models import (
    Department,
    Designation,
    Employee,
    EmployeeSalary,
    SalaryRevision,
)
from .permissions import (
    CanAccessSalary,
    CanApproveSalary,
    CanManageEmployees,
    CanManageStructure,
    CanViewEmployeeAudit,
    CanViewEmployees,
    CanViewWorkforceReports,
)
from .search import SORT_FIELDS, apply_filters, parse_filters
from .serializers import (
    DecisionSerializer,
    DepartmentChangeSerializer,
    DepartmentSerializer,
    DesignationChangeSerializer,
    DesignationSerializer,
    EmployeeAuditSerializer,
    EmployeeBriefSerializer,
    EmployeeDetailSerializer,
    EmployeeHistorySerializer,
    EmployeeListSerializer,
    EmployeeWriteSerializer,
    ManagerChangeSerializer,
    PromotionSerializer,
    SalaryRecordSerializer,
    SalaryRevisionSerializer,
    SalaryWriteSerializer,
    StatusChangeSerializer,
)


class CompanyScopedAPI(APIView):
    """Base for every view here: resolves the company and the shared context."""

    permission_classes = [CanViewEmployees, HasCompanyContext]

    @property
    def company(self):
        return self.request.company.company

    def context(self, **extra):
        return {"request": self.request, "company": self.company, **extra}

    def employees(self):
        """The company's employees, with the rows every serializer reads."""
        return Employee.objects.filter(company=self.company).select_related(
            "department", "designation", "reporting_manager", "user"
        )

    def employee(self, employee_id):
        return get_object_or_404(self.employees(), pk=employee_id)


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


class EmployeeMetaAPI(CompanyScopedAPI):
    """Everything the screens need to render their pickers, in one call.

    The directory's filter bar, the hire form, the transfer dialog and the
    org chart all want the same masters. Fetching them together keeps a page
    load at two requests instead of seven, and the ``permissions`` block lets
    each screen hide what this user cannot do rather than offer a button that
    403s.
    """

    def get(self, request):
        departments = (
            Department.objects.filter(company=self.company)
            .select_related("head", "parent")
            .annotate(
                employee_count=Count(
                    "employees",
                    filter=Q(employees__employment_status__in=IN_SERVICE_STATUSES),
                    distinct=True,
                )
            )
        )
        designations = Designation.objects.filter(company=self.company).annotate(
            employee_count=Count(
                "employees",
                filter=Q(employees__employment_status__in=IN_SERVICE_STATUSES),
                distinct=True,
            )
        )
        managers = (
            self.employees()
            .filter(employment_status__in=IN_SERVICE_STATUSES)
            .filter(Q(is_manager=True) | Q(direct_reports__isnull=False))
            .distinct()
            .order_by("hierarchy_level", "full_name")
        )
        return Response(
            {
                "departments": DepartmentSerializer(
                    departments, many=True, context=self.context()
                ).data,
                "designations": DesignationSerializer(designations, many=True).data,
                "managers": EmployeeBriefSerializer(
                    managers, many=True, context=self.context()
                ).data,
                "employment_statuses": [
                    {"value": value, "label": label} for value, label in EmploymentStatus.choices
                ],
                "revision_types": [
                    {"value": value, "label": label} for value, label in RevisionType.choices
                ],
                "salary_statuses": [
                    {"value": value, "label": label} for value, label in SalaryStatus.choices
                ],
                "history_events": [
                    {"value": value, "label": label} for value, label in HistoryEvent.choices
                ],
                "sort_options": sorted(SORT_FIELDS.keys()),
                "headcount": self.employees()
                .filter(employment_status__in=IN_SERVICE_STATUSES)
                .count(),
                "permissions": permission_flags(request),
            }
        )


# ---------------------------------------------------------------------------
# The directory
# ---------------------------------------------------------------------------


class EmployeeListAPI(CompanyScopedAPI):
    """The directory: search, filter, sort, page.

    The response carries ``status_counts`` alongside the page so the filter
    chips can show live numbers without a second round trip -- counted over the
    *filtered* set minus the status filter itself, which is what makes "Active
    18 / On leave 2" mean something while a department filter is on.
    """

    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        filters = parse_filters(request)
        reach = salary_reach(request)

        manager_path = None
        if filters.manager:
            manager = self.employees().filter(pk=filters.manager).first()
            if manager is None:
                raise ValidationError("That manager is not an employee of this company.")
            manager_path = manager.path_prefix

        queryset = apply_filters(
            hierarchy.with_report_counts(self.employees()),
            filters,
            company=self.company,
            salary_scope=reach.as_filter(),
            manager_path=manager_path,
        )

        page, page_size = get_page_params(request)
        rows, total, meta = paginate_queryset(queryset, page, page_size)
        payload = build_page(
            EmployeeListSerializer(rows, many=True, context=self.context()).data, meta
        )
        payload["status_counts"] = self._status_counts(filters, reach, manager_path)
        payload["applied_sort"] = filters.sort
        return Response(payload)

    def _status_counts(self, filters, reach, manager_path):
        """Headcount per employment status, over everything else the filter says."""
        without_status = apply_filters(
            self.employees(),
            replace(filters, statuses=[], in_service_only=False, sort="name"),
            company=self.company,
            salary_scope=reach.as_filter(),
            manager_path=manager_path,
        )
        counted = dict(
            without_status.values_list("employment_status")
            .annotate(total=Count("id"))
            .values_list("employment_status", "total")
        )
        return {value: counted.get(value, 0) for value, _ in EmploymentStatus.choices}

    def post(self, request):
        if not request.user.has_perm("employee_hierarchy.can_manage_employees"):
            raise PermissionDenied("You cannot add employees.")
        serializer = EmployeeWriteSerializer(data=request.data, context=self.context())
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        initial_salary = data.pop("initial_salary", None)
        if initial_salary:
            if not request.user.has_perm(CREATE_SALARY):
                raise PermissionDenied(
                    "You can add the employee, but not their salary. Leave the salary blank "
                    "and ask someone with salary access to enter it."
                )
            initial_salary = dict(initial_salary)
            # A joining salary entered by somebody who may also approve is in
            # force immediately; otherwise it waits, like any other revision.
            initial_salary["approve"] = request.user.has_perm(APPROVE_SALARY)
            initial_salary.pop("revision_type", None)
            data["initial_salary"] = initial_salary

        employee = services.create_employee(company=self.company, data=data, user=request.user)
        employee = self.employee(employee.pk)
        return Response(
            EmployeeDetailSerializer(employee, context=self.context()).data,
            status=http_status.HTTP_201_CREATED,
        )


class EmployeeDetailAPI(CompanyScopedAPI):
    """One employee: read them, or edit their plain details."""

    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request, employee_id):
        employee = hierarchy.with_report_counts(self.employees()).filter(pk=employee_id).first()
        if employee is None:
            raise ValidationError("No such employee in this company.")
        reach = salary_reach(request)
        current = services.current_salary(employee) if reach.can_view(employee) else None
        return Response(
            EmployeeDetailSerializer(
                employee, context=self.context(current_salary=current)
            ).data
        )

    def patch(self, request, employee_id):
        if not request.user.has_perm("employee_hierarchy.can_manage_employees"):
            raise PermissionDenied("You cannot edit employees.")
        employee = self.employee(employee_id)
        serializer = EmployeeWriteSerializer(
            employee, data=request.data, partial=True, context=self.context()
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("initial_salary", None)

        # Structural changes have their own endpoints, because each one does
        # more than write a column and each one takes a reason.
        for structural, endpoint in (
            ("reporting_manager", "manager/"),
            ("department", "department/"),
            ("designation", "designation/"),
            ("employment_status", "status/"),
        ):
            if structural in data:
                raise ValidationError(
                    {structural: f"Change this through the {endpoint} endpoint, "
                                 "so the move is recorded with its reason."}
                )

        services.update_employee(
            employee, data, user=request.user, reason=request.data.get("reason", "")
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        return Response(EmployeeDetailSerializer(employee, context=self.context()).data)


class EmployeeReportingAPI(CompanyScopedAPI):
    """Everything about one employee's place in the organisation.

    One call answers all seven questions the brief asks -- direct manager,
    direct reports, the manager's manager, the full management chain, peers,
    every subordinate, and the organisational path -- because they are one
    screen and, thanks to the materialised path, four queries rather than one
    per level.
    """

    def get(self, request, employee_id):
        employee = self.employee(employee_id)
        chain = hierarchy.reporting_chain(employee)
        reports = hierarchy.with_report_counts(hierarchy.direct_reports(employee)).order_by(
            "full_name"
        )
        subordinates = hierarchy.subtree(employee).select_related("department", "designation")

        context = self.context()
        manager = employee.reporting_manager
        grand_manager = manager.reporting_manager if manager is not None else None
        return Response(
            {
                "employee": EmployeeBriefSerializer(employee, context=context).data,
                "manager": EmployeeBriefSerializer(manager, context=context).data
                if manager
                else None,
                "managers_manager": EmployeeBriefSerializer(grand_manager, context=context).data
                if grand_manager
                else None,
                # Top-most first, so the client can print CEO → … → this person.
                "management_chain": EmployeeBriefSerializer(
                    chain, many=True, context=context
                ).data,
                "direct_reports": EmployeeListSerializer(
                    reports, many=True, context=context
                ).data,
                "peers": EmployeeBriefSerializer(
                    hierarchy.peers(employee).order_by("full_name"), many=True, context=context
                ).data,
                "subordinate_count": subordinates.count(),
                "direct_report_count": reports.count(),
                "organisational_path": [
                    *(person.full_name for person in chain),
                    employee.full_name,
                ],
                "department_path": self._department_path(employee),
            }
        )

    def _department_path(self, employee):
        """Company → Technology → Engineering, for the breadcrumb on the page."""
        names = []
        department = employee.department
        seen = set()
        while department is not None and department.pk not in seen:
            seen.add(department.pk)
            names.append(department.name)
            department = department.parent
        return list(reversed(names))


class EmployeeHistoryAPI(CompanyScopedAPI):
    """The employee's career here, newest first.

    Salary rows are in the timeline but their *amounts* are not: the event says
    "Salary revised" and carries the figures only for a viewer who may see this
    person's pay. The fact that somebody got a raise is part of their story;
    what it was is not, unless you are cleared for it.
    """

    def get(self, request, employee_id):
        employee = self.employee(employee_id)
        entries = employee.history_entries.select_related("created_by")
        may_see_money = salary_reach(request).can_view(employee)

        page, page_size = get_page_params(request)
        rows, total, meta = paginate_queryset(entries, page, page_size)
        data = EmployeeHistorySerializer(rows, many=True, context=self.context()).data
        if not may_see_money:
            for entry in data:
                if entry["event"] == HistoryEvent.SALARY_REVISED:
                    entry["from_value"] = ""
                    entry["to_value"] = ""
                    entry["notes"] = ""
        return Response(build_page(data, meta))


class EmployeeAuditAPI(CompanyScopedAPI):
    """Who changed what about this employee, and why."""

    permission_classes = [CanViewEmployeeAudit, HasCompanyContext]

    def get(self, request, employee_id):
        employee = self.employee(employee_id)
        entries = employee.audit_logs.select_related("performed_by")
        action = request.GET.get("action")
        if action:
            entries = entries.filter(action=action)

        may_see_money = salary_reach(request).can_view(employee)
        if not may_see_money:
            # The audit trail records the amounts. Someone cleared to audit
            # structure is not automatically cleared to read pay, so the salary
            # rows are listed without their figures.
            entries = entries.exclude(
                action__in=(
                    AuditAction.SALARY_CREATED,
                    AuditAction.SALARY_APPROVED,
                    AuditAction.SALARY_REJECTED,
                )
            )

        page, page_size = get_page_params(request)
        rows, total, meta = paginate_queryset(entries, page, page_size)
        return Response(
            build_page(EmployeeAuditSerializer(rows, many=True).data, meta)
        )


# ---------------------------------------------------------------------------
# Hierarchy operations
# ---------------------------------------------------------------------------


class EmployeeManagerAPI(CompanyScopedAPI):
    """Assign, change or remove an employee's reporting manager."""

    permission_classes = [CanManageEmployees, HasCompanyContext]

    def post(self, request, employee_id):
        employee = self.employee(employee_id)
        serializer = ManagerChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        manager = serializer.validated_data["manager"]
        if manager is not None and manager.company_id != self.company.id:
            raise ValidationError("That manager is not an employee of this company.")

        result = services.change_manager(
            employee,
            manager,
            user=request.user,
            reason=serializer.validated_data.get("reason", ""),
            carry_team=serializer.validated_data.get("carry_team", True),
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        return Response(
            {
                **result,
                "employee": EmployeeDetailSerializer(employee, context=self.context()).data,
            }
        )


class EmployeeDepartmentAPI(CompanyScopedAPI):
    """Move an employee -- and optionally their whole team -- to a department."""

    permission_classes = [CanManageEmployees, HasCompanyContext]

    def post(self, request, employee_id):
        employee = self.employee(employee_id)
        serializer = DepartmentChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        department = serializer.validated_data["department"]
        if department is not None and department.company_id != self.company.id:
            raise ValidationError("That department belongs to another company.")

        result = services.change_department(
            employee,
            department,
            user=request.user,
            reason=serializer.validated_data.get("reason", ""),
            include_team=serializer.validated_data.get("include_team", False),
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        return Response(
            {**result, "employee": EmployeeDetailSerializer(employee, context=self.context()).data}
        )


class EmployeeDesignationAPI(CompanyScopedAPI):
    """Change the rung of the ladder somebody is on."""

    permission_classes = [CanManageEmployees, HasCompanyContext]

    def post(self, request, employee_id):
        employee = self.employee(employee_id)
        serializer = DesignationChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        designation = serializer.validated_data["designation"]
        if designation is not None and designation.company_id != self.company.id:
            raise ValidationError("That designation belongs to another company.")

        result = services.change_designation(
            employee,
            designation,
            user=request.user,
            reason=serializer.validated_data.get("reason", ""),
            promotion=serializer.validated_data.get("promotion", False),
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        return Response(
            {**result, "employee": EmployeeDetailSerializer(employee, context=self.context()).data}
        )


class EmployeePromotionAPI(CompanyScopedAPI):
    """A promotion: new rung, sometimes a new manager, usually new money.

    One endpoint rather than three calls, so the trail records one decision
    with one reason instead of three edits made the same afternoon.
    """

    permission_classes = [CanManageEmployees, HasCompanyContext]

    def post(self, request, employee_id):
        employee = self.employee(employee_id)
        serializer = PromotionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        salary = data.get("salary")
        if salary:
            if not request.user.has_perm(CREATE_SALARY) and not request.user.has_perm(UPDATE_SALARY):
                raise PermissionDenied(
                    "You can promote this employee, but not set their salary. "
                    "Promote them without the revision and let payroll enter it."
                )
            salary = dict(salary)
            salary["approve"] = request.user.has_perm(APPROVE_SALARY)
            salary.pop("revision_type", None)

        for field in ("designation", "manager", "department"):
            value = data.get(field)
            if value is not None and value.company_id != self.company.id:
                raise ValidationError({field: "That belongs to another company."})

        result = services.promote(
            employee,
            designation=data.get("designation"),
            manager=data.get("manager"),
            department=data.get("department"),
            salary=salary,
            user=request.user,
            reason=data.get("reason", ""),
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        record = result.pop("salary", None)
        return Response(
            {
                **result,
                "salary_record_id": record.id if record is not None else None,
                "employee": EmployeeDetailSerializer(employee, context=self.context()).data,
            }
        )


class EmployeeStatusAPI(CompanyScopedAPI):
    """Probation, leave, suspension, resignation, retirement, termination."""

    permission_classes = [CanManageEmployees, HasCompanyContext]

    def post(self, request, employee_id):
        employee = self.employee(employee_id)
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        target = data.get("reassign_reports_to")
        if target is not None and target.company_id != self.company.id:
            raise ValidationError("That employee is not in this company.")

        result = services.change_status(
            employee,
            data["status"],
            user=request.user,
            reason=data.get("reason", ""),
            exit_date=data.get("exit_date"),
            reassign_reports_to=target,
        )
        employee = hierarchy.with_report_counts(self.employees()).get(pk=employee_id)
        return Response(
            {**result, "employee": EmployeeDetailSerializer(employee, context=self.context()).data}
        )


# ---------------------------------------------------------------------------
# The chart
# ---------------------------------------------------------------------------


class OrgTreeAPI(CompanyScopedAPI):
    """The organisation as a tree.

    ``?root=<id>`` returns just that person's organisation (the manager view);
    without it, every top-level employee comes back as a root -- the brief wants
    more than one to be possible.

    ``?include_past=1`` keeps people who have left, which is off by default:
    a chart of everyone who ever worked here is not a chart.

    The whole tree is fetched in ONE query and nested in Python. That is the
    materialised path earning its keep -- the alternative is a query per node,
    and this endpoint is the one a four-thousand-person company opens first.
    """

    def get(self, request):
        queryset = self.employees().annotate(
            direct_report_count=Count("direct_reports", distinct=True)
        )
        if request.GET.get("include_past") not in ("1", "true", "yes"):
            queryset = queryset.filter(employment_status__in=IN_SERVICE_STATUSES)

        root_id = request.GET.get("root")
        root = None
        if root_id:
            root = self.employee(root_id)
            queryset = queryset.filter(hierarchy_path__startswith=root.path_prefix)

        department_id = request.GET.get("department")
        if department_id:
            queryset = queryset.filter(department_id=department_id)

        employees = list(queryset.order_by("hierarchy_level", "full_name"))
        context = self.context()

        def node(employee):
            data = EmployeeBriefSerializer(employee, context=context).data
            data["direct_report_count"] = getattr(employee, "direct_report_count", 0)
            data["manager_id"] = employee.reporting_manager_id
            return data

        forest = hierarchy.build_forest(employees, node)
        return Response(
            {
                "roots": forest,
                "total": len(employees),
                "root_employee": EmployeeBriefSerializer(root, context=context).data
                if root
                else None,
                "chain_to_root": EmployeeBriefSerializer(
                    hierarchy.reporting_chain(root), many=True, context=context
                ).data
                if root
                else [],
            }
        )


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------


class EmployeeSalaryAPI(CompanyScopedAPI):
    """One employee's pay: what it is now, and what it has been.

    The permission class only asks "may this user see any salary at all". Whose
    they may see is decided here, per employee, and a refusal says so plainly
    rather than pretending the employee does not exist -- the person is in the
    directory either way, and a vague 404 would only send someone to ask a
    colleague.

    Reading somebody else's pay is itself recorded in the audit trail. That is
    deliberate: the cheapest way to keep salary access honest is for everyone
    to know that looking leaves a trace.
    """

    permission_classes = [CanAccessSalary, HasCompanyContext]

    def get(self, request, employee_id):
        employee = self.employee(employee_id)
        reach = salary_reach(request)
        if not reach.can_view(employee):
            raise PermissionDenied(
                f"You do not have access to {employee.full_name}'s salary information."
            )

        current = services.current_salary(employee)
        history_allowed = reach.can_view_history(employee)
        records = employee.salary_records.select_related(
            "approved_by", "created_by", "revision"
        )
        if not history_allowed:
            # Without the history right, the answer is the record in force and
            # anything already approved for the future -- what they are paid,
            # not how they got here.
            records = records.filter(
                status__in=(SalaryStatus.ACTIVE, SalaryStatus.SCHEDULED, SalaryStatus.PENDING)
            )

        if reach.viewer is None or reach.viewer.pk != employee.pk:
            services.log_audit(
                employee,
                AuditAction.SALARY_VIEWED,
                field="salary",
                reason="Salary information opened",
                user=request.user,
            )

        return Response(
            {
                "employee": EmployeeBriefSerializer(employee, context=self.context()).data,
                "current": SalaryRecordSerializer(current, context=self.context()).data
                if current
                else None,
                "records": SalaryRecordSerializer(
                    records, many=True, context=self.context()
                ).data,
                "can_view_history": history_allowed,
                "can_create": request.user.has_perm(CREATE_SALARY),
                "can_approve": request.user.has_perm(APPROVE_SALARY),
                "pending_count": employee.salary_records.filter(
                    status__in=(SalaryStatus.DRAFT, SalaryStatus.PENDING)
                ).count(),
            }
        )

    def post(self, request, employee_id):
        """Propose a revision. It waits for approval unless the caller may approve."""
        if not (request.user.has_perm(CREATE_SALARY) or request.user.has_perm(UPDATE_SALARY)):
            raise PermissionDenied("You cannot create or revise salary records.")
        employee = self.employee(employee_id)
        reach = salary_reach(request)
        if not reach.can_view(employee):
            raise PermissionDenied(
                f"You do not have access to {employee.full_name}'s salary information."
            )

        serializer = SalaryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        wants_approval = data.pop("approve", False)

        record = services.create_salary_record(
            employee,
            user=request.user,
            approve=bool(wants_approval and request.user.has_perm(APPROVE_SALARY)),
            **data,
        )
        return Response(
            SalaryRecordSerializer(record, context=self.context()).data,
            status=http_status.HTTP_201_CREATED,
        )


class SalaryDecisionAPI(CompanyScopedAPI):
    """Approve or reject a proposed revision.

    Both live on one view because they are the same decision with two answers,
    and both need the same thing said about them: a rejected record is kept.
    """

    permission_classes = [CanApproveSalary, HasCompanyContext]

    def _record(self, record_id):
        return get_object_or_404(
            EmployeeSalary.objects.select_related("employee", "revision").filter(
                employee__company=self.company
            ),
            pk=record_id,
        )

    def post(self, request, record_id, decision):
        record = self._record(record_id)
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data.get("reason", "")

        if decision == "approve":
            record = services.approve_salary_record(record, user=request.user, reason=reason)
        else:
            record = services.reject_salary_record(record, user=request.user, reason=reason)
        return Response(SalaryRecordSerializer(record, context=self.context()).data)


class SalaryApprovalQueueAPI(CompanyScopedAPI):
    """What is waiting for an approver.

    Restricted to the approver's salary reach, like every other list of money:
    an approver cleared for one department must not learn the rest of the
    company's numbers from their own inbox.
    """

    permission_classes = [CanAccessSalary, HasCompanyContext]

    def get(self, request):
        reach = salary_reach(request)
        scope = reach.as_filter()
        if scope is None:
            raise PermissionDenied("You do not have access to salary information.")

        visible_employees = self.employees().filter(scope)
        pending = (
            EmployeeSalary.objects.filter(
                employee__in=visible_employees,
                status__in=(SalaryStatus.DRAFT, SalaryStatus.PENDING),
            )
            .select_related("employee", "employee__department", "employee__designation", "revision")
            .order_by("effective_from", "id")
        )
        page, page_size = get_page_params(request)
        rows, total, meta = paginate_queryset(pending, page, page_size)
        data = []
        for record in rows:
            entry = SalaryRecordSerializer(record, context=self.context()).data
            entry["employee"] = EmployeeBriefSerializer(
                record.employee, context=self.context()
            ).data
            data.append(entry)
        return Response(
            build_page(data, meta) | {"can_approve": request.user.has_perm(APPROVE_SALARY)}
        )


class SalaryRevisionListAPI(CompanyScopedAPI):
    """The revision-history report: every change, filterable by type and date."""

    permission_classes = [CanAccessSalary, HasCompanyContext]

    def get(self, request):
        reach = salary_reach(request)
        scope = reach.as_filter()
        if scope is None:
            raise PermissionDenied("You do not have access to salary information.")

        revisions = SalaryRevision.objects.filter(
            employee__in=self.employees().filter(scope)
        ).select_related(
            "employee",
            "employee__department",
            "employee__designation",
            "salary_record",
            "salary_record__approved_by",
            "created_by",
        )

        revision_type = request.GET.get("revision_type")
        if revision_type:
            revisions = revisions.filter(revision_type=revision_type)
        employee_id = request.GET.get("employee")
        if employee_id:
            revisions = revisions.filter(employee_id=employee_id)
        year = request.GET.get("year")
        if year and year.isdigit():
            revisions = revisions.filter(effective_date__year=int(year))

        page, page_size = get_page_params(request)
        rows, total, meta = paginate_queryset(revisions, page, page_size)
        return Response(
            build_page(
                SalaryRevisionSerializer(rows, many=True, context=self.context()).data, meta
            )
        )


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------


class DepartmentListAPI(CompanyScopedAPI):
    """The department master, with headcount, as a flat list and as a tree."""

    permission_classes = [CanManageStructure, HasCompanyContext]

    def _queryset(self):
        return (
            Department.objects.filter(company=self.company)
            .select_related("head", "parent")
            .annotate(
                employee_count=Count(
                    "employees",
                    filter=Q(employees__employment_status__in=IN_SERVICE_STATUSES),
                    distinct=True,
                )
            )
        )

    def get(self, request):
        departments = list(self._queryset())
        serialized = DepartmentSerializer(
            departments, many=True, context=self.context()
        ).data

        # Total headcount includes sub-departments: "Technology: 34" is what
        # somebody looking at the tree means, even though only two of those
        # people sit in Technology itself.
        by_id = {row["id"]: row for row in serialized}
        children = {}
        for row in serialized:
            children.setdefault(row["parent"], []).append(row["id"])

        def total(department_id):
            row = by_id[department_id]
            running = row["employee_count"]
            for child_id in children.get(department_id, []):
                running += total(child_id)
            row["total_employee_count"] = running
            return running

        for root_id in children.get(None, []):
            total(root_id)
        # A department whose parent was retired out from under it still needs a
        # total, so anything the roots did not reach is measured on its own.
        for row in serialized:
            if "total_employee_count" not in row or row["total_employee_count"] is None:
                total(row["id"])

        return Response({"results": serialized, "count": len(serialized)})

    def post(self, request):
        serializer = DepartmentSerializer(data=request.data, context=self.context())
        serializer.is_valid(raise_exception=True)
        department = Department(company=self.company, **serializer.validated_data)
        services.validate_department_parent(department, department.parent)
        department.created_by = request.user
        department.updated_by = request.user
        department.save()
        return Response(
            DepartmentSerializer(department, context=self.context()).data,
            status=http_status.HTTP_201_CREATED,
        )


class DepartmentDetailAPI(CompanyScopedAPI):
    permission_classes = [CanManageStructure, HasCompanyContext]

    def _department(self, department_id):
        return get_object_or_404(
            Department.objects.filter(company=self.company), pk=department_id
        )

    def patch(self, request, department_id):
        department = self._department(department_id)
        serializer = DepartmentSerializer(
            department, data=request.data, partial=True, context=self.context()
        )
        serializer.is_valid(raise_exception=True)
        updated = serializer.save(updated_by=request.user)
        services.validate_department_parent(updated, updated.parent)
        return Response(DepartmentSerializer(updated, context=self.context()).data)

    def delete(self, request, department_id):
        """Retire it. Nothing here is ever deleted -- see ``retire_department``."""
        department = self._department(department_id)
        services.retire_department(department, user=request.user)
        return Response(DepartmentSerializer(department, context=self.context()).data)


class DesignationListAPI(CompanyScopedAPI):
    permission_classes = [CanManageStructure, HasCompanyContext]

    def get(self, request):
        designations = Designation.objects.filter(company=self.company).annotate(
            employee_count=Count(
                "employees",
                filter=Q(employees__employment_status__in=IN_SERVICE_STATUSES),
                distinct=True,
            )
        )
        data = DesignationSerializer(designations, many=True).data
        return Response({"results": data, "count": len(data)})

    def post(self, request):
        serializer = DesignationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        designation = Designation(company=self.company, **serializer.validated_data)
        designation.created_by = request.user
        designation.updated_by = request.user
        designation.save()
        return Response(
            DesignationSerializer(designation).data, status=http_status.HTTP_201_CREATED
        )


class DesignationDetailAPI(CompanyScopedAPI):
    permission_classes = [CanManageStructure, HasCompanyContext]

    def _designation(self, designation_id):
        return get_object_or_404(
            Designation.objects.filter(company=self.company), pk=designation_id
        )

    def patch(self, request, designation_id):
        designation = self._designation(designation_id)
        serializer = DesignationSerializer(designation, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, designation_id):
        """Retire a rung. Employees keep pointing at it; history stays readable."""
        designation = self._designation(designation_id)
        if designation.employees.filter(employment_status__in=IN_SERVICE_STATUSES).exists():
            raise ValidationError(
                "Employees still hold this designation. Move them to another rung first."
            )
        designation.status = RecordStatus.INACTIVE
        designation.updated_by = request.user
        designation.save(update_fields=["status", "updated_at", "updated_by"])
        return Response(DesignationSerializer(designation).data)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

#: Annual-compensation bands for the distribution chart, in rupees.
#:
#: Fixed rather than computed from the data, because a histogram whose buckets
#: move every time somebody is hired cannot be compared with last month's. They
#: are Indian salary bands (3 lakh, 6 lakh, …) since that is the currency this
#: runs in; the labels travel with the response so the client never invents its
#: own.
SALARY_BANDS = [
    (None, 300000, "Under ₹3L"),
    (300000, 600000, "₹3L – ₹6L"),
    (600000, 1000000, "₹6L – ₹10L"),
    (1000000, 1500000, "₹10L – ₹15L"),
    (1500000, 2500000, "₹15L – ₹25L"),
    (2500000, 5000000, "₹25L – ₹50L"),
    (5000000, None, "Over ₹50L"),
]

#: How far back the trend charts look.
TREND_MONTHS = 12


class WorkforceReportsAPI(CompanyScopedAPI):
    """Headcount, structure and -- for whoever may see it -- money.

    One endpoint for the whole reports page, because every tile on it is an
    aggregate over the same set of employees and eleven separate calls would
    read the same table eleven times.

    The salary half is built only for a viewer with salary access, and only
    over the employees that viewer may see: a department head with department
    access gets their department's payroll and a distribution of their own
    people, not the company's. The ``salary_scope`` key says which it was, so
    the page can label the number honestly instead of implying it is the
    company total.
    """

    permission_classes = [CanViewWorkforceReports, HasCompanyContext]

    def get(self, request):
        include_past = request.GET.get("include_past") in ("1", "true", "yes")
        employees = self.employees()
        if not include_past:
            employees = employees.filter(employment_status__in=IN_SERVICE_STATUSES)

        payload = {
            "generated_at": timezone.now(),
            "include_past": include_past,
            "totals": self._totals(employees),
            "by_department": self._by_department(employees),
            "by_designation": self._by_designation(employees),
            "by_level": self._by_level(employees),
            "by_status": self._by_status(),
            "by_manager": self._by_manager(employees),
            "joining_trend": self._joining_trend(),
            "turnover": self._turnover(),
        }
        payload["salary"] = self._salary(request)
        return Response(payload)

    # -- headcount --------------------------------------------------------

    def _totals(self, employees):
        aggregate = employees.aggregate(
            headcount=Count("id"),
            managers=Count("id", filter=Q(is_manager=True)),
            top_level=Count("id", filter=Q(reporting_manager__isnull=True)),
            deepest=Max("hierarchy_level"),
            earliest_joining=Min("joining_date"),
        )
        managed = employees.filter(direct_reports__isnull=False).distinct().count()
        with_reports = (
            employees.annotate(team=Count("direct_reports"))
            .filter(team__gt=0)
            .aggregate(average=Avg("team"))["average"]
        )
        today = timezone.localdate()
        month_start = today.replace(day=1)
        return {
            "headcount": aggregate["headcount"] or 0,
            "managers": aggregate["managers"] or 0,
            "managers_with_reports": managed,
            "top_level": aggregate["top_level"] or 0,
            "levels_deep": aggregate["deepest"] or 0,
            "average_team_size": round(float(with_reports), 1) if with_reports else 0,
            "joined_this_month": employees.filter(joining_date__gte=month_start).count(),
            "joined_this_year": employees.filter(joining_date__year=today.year).count(),
            "earliest_joining": aggregate["earliest_joining"],
            "departments": Department.objects.filter(
                company=self.company, status=RecordStatus.ACTIVE
            ).count(),
            "designations": Designation.objects.filter(
                company=self.company, status=RecordStatus.ACTIVE
            ).count(),
        }

    def _by_department(self, employees):
        rows = (
            employees.values("department_id", "department__name", "department__code")
            .annotate(count=Count("id"), managers=Count("id", filter=Q(is_manager=True)))
            .order_by("-count")
        )
        return [
            {
                "id": row["department_id"],
                "name": row["department__name"] or "Unassigned",
                "code": row["department__code"] or "",
                "count": row["count"],
                "managers": row["managers"],
            }
            for row in rows
        ]

    def _by_designation(self, employees):
        rows = (
            employees.values("designation_id", "designation__name", "designation__level")
            .annotate(count=Count("id"))
            .order_by("designation__level", "-count")
        )
        return [
            {
                "id": row["designation_id"],
                "name": row["designation__name"] or "Unassigned",
                "level": row["designation__level"],
                "count": row["count"],
            }
            for row in rows
        ]

    def _by_level(self, employees):
        rows = employees.values("hierarchy_level").annotate(count=Count("id")).order_by(
            "hierarchy_level"
        )
        return [{"level": row["hierarchy_level"], "count": row["count"]} for row in rows]

    def _by_status(self):
        """Always over everybody -- the point of this tile is who is not active."""
        counted = dict(
            self.employees()
            .values_list("employment_status")
            .annotate(total=Count("id"))
            .values_list("employment_status", "total")
        )
        return [
            {"status": value, "label": label, "count": counted.get(value, 0)}
            for value, label in EmploymentStatus.choices
        ]

    def _by_manager(self, employees):
        """Team size per manager -- direct reports, and the whole organisation.

        The deep count is one query for all of them: every manager's subtree
        size is the number of employees whose path contains their id, which is
        counted in Python from a single column fetch rather than a query per
        manager.
        """
        managers = list(
            employees.annotate(direct=Count("direct_reports", distinct=True))
            .filter(direct__gt=0)
            .order_by("-direct", "full_name")[:25]
            .values("id", "full_name", "employee_code", "direct", "department__name")
        )
        if not managers:
            return []
        wanted = {row["id"] for row in managers}
        deep = {manager_id: 0 for manager_id in wanted}
        for path in employees.values_list("hierarchy_path", flat=True):
            segments = [int(segment) for segment in path.split("/") if segment]
            # The last segment is the employee themselves; the rest are the
            # managers above them.
            for manager_id in segments[:-1]:
                if manager_id in deep:
                    deep[manager_id] += 1
        return [
            {
                "id": row["id"],
                "name": row["full_name"],
                "employee_code": row["employee_code"],
                "department": row["department__name"] or "—",
                "direct_reports": row["direct"],
                "total_reports": deep.get(row["id"], 0),
            }
            for row in managers
        ]

    # -- trends -----------------------------------------------------------

    def _month_series(self, months=TREND_MONTHS):
        """The last ``months`` months as ``YYYY-MM`` keys, oldest first."""
        today = timezone.localdate().replace(day=1)
        series = []
        cursor = today
        for _ in range(months):
            series.append(cursor)
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        return list(reversed(series))

    def _joining_trend(self):
        months = self._month_series()
        start = months[0]
        rows = (
            self.employees()
            .filter(joining_date__gte=start)
            .annotate(month=TruncMonth("joining_date"))
            .values("month")
            .annotate(count=Count("id"))
        )
        counted = {row["month"].strftime("%Y-%m"): row["count"] for row in rows if row["month"]}
        return [
            {"month": month.strftime("%Y-%m"), "label": month.strftime("%b %y"), "joined": counted.get(month.strftime("%Y-%m"), 0)}
            for month in months
        ]

    def _turnover(self):
        """Exits by month, and the rate against average headcount.

        The rate is exits over the year divided by the headcount still here
        plus those exits -- the simple version of the number, computed the same
        way every month so the trend is comparable even where the definition is
        arguable.
        """
        months = self._month_series()
        start = months[0]
        rows = (
            self.employees()
            .filter(exit_date__gte=start)
            .annotate(month=TruncMonth("exit_date"))
            .values("month")
            .annotate(count=Count("id"))
        )
        counted = {row["month"].strftime("%Y-%m"): row["count"] for row in rows if row["month"]}
        series = [
            {"month": month.strftime("%Y-%m"), "label": month.strftime("%b %y"), "exits": counted.get(month.strftime("%Y-%m"), 0)}
            for month in months
        ]
        exits = sum(entry["exits"] for entry in series)
        in_service = (
            self.employees().filter(employment_status__in=IN_SERVICE_STATUSES).count()
        )
        base = in_service + exits
        return {
            "series": series,
            "exits_12m": exits,
            "in_service": in_service,
            "rate_percent": round(exits / base * 100, 1) if base else 0.0,
        }

    # -- money ------------------------------------------------------------

    def _salary(self, request):
        """The salary half of the report, or a refusal that says why.

        Every figure here is over the viewer's own reach. ``scope`` names it, so
        a department head's page can say "your department" instead of leaving
        them to assume they are looking at the company.
        """
        reach = salary_reach(request)
        scope = reach.as_filter()
        if scope is None:
            return {"visible": False, "scope": "none"}

        visible = self.employees().filter(scope).filter(
            employment_status__in=IN_SERVICE_STATUSES, current_salary_amount__isnull=False
        )
        aggregate = visible.aggregate(
            total=Sum("current_salary_amount"),
            average=Avg("current_salary_amount"),
            lowest=Min("current_salary_amount"),
            highest=Max("current_salary_amount"),
            people=Count("id"),
        )

        amounts = sorted(
            float(amount)
            for amount in visible.values_list("current_salary_amount", flat=True)
            if amount is not None
        )
        median = None
        if amounts:
            middle = len(amounts) // 2
            median = (
                amounts[middle]
                if len(amounts) % 2
                else (amounts[middle - 1] + amounts[middle]) / 2
            )

        distribution = []
        for low, high, label in SALARY_BANDS:
            count = sum(
                1
                for amount in amounts
                if (low is None or amount >= low) and (high is None or amount < high)
            )
            distribution.append({"label": label, "from": low, "to": high, "count": count})

        by_department = [
            {
                "id": row["department_id"],
                "name": row["department__name"] or "Unassigned",
                "people": row["people"],
                "total": row["total"],
                "average": row["average"],
            }
            for row in visible.values("department_id", "department__name")
            .annotate(
                people=Count("id"),
                total=Sum("current_salary_amount"),
                average=Avg("current_salary_amount"),
            )
            .order_by("-total")
        ]

        revisions = (
            SalaryRevision.objects.filter(employee__in=self.employees().filter(scope))
            .values("revision_type")
            .annotate(count=Count("id"), average_change=Avg("new_amount"))
            .order_by("-count")
        )
        revision_labels = dict(RevisionType.choices)

        return {
            "visible": True,
            "scope": "all" if reach.all else "partial",
            "people_with_salary": aggregate["people"] or 0,
            "total_payroll": aggregate["total"],
            "average": aggregate["average"],
            "median": median,
            "lowest": aggregate["lowest"],
            "highest": aggregate["highest"],
            "currency": visible.values_list("current_salary_currency", flat=True).first() or "INR",
            "distribution": distribution,
            "by_department": by_department,
            "revisions_by_type": [
                {
                    "type": row["revision_type"],
                    "label": revision_labels.get(row["revision_type"], row["revision_type"]),
                    "count": row["count"],
                }
                for row in revisions
            ],
            "pending_approvals": EmployeeSalary.objects.filter(
                employee__in=self.employees().filter(scope),
                status__in=(SalaryStatus.DRAFT, SalaryStatus.PENDING),
            ).count(),
        }
