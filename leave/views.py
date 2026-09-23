"""
The HTTP surface.

Every view here is thin on purpose. The rules live in three modules and the
views only join them up:

* :mod:`leave.services`   -- what a transition does, and what it refuses
* :mod:`leave.routing`    -- who may do it
* :mod:`leave.projection` -- what the attendance sheet is told afterwards

A view that made a decision of its own would be a fourth place to look when the
answer surprises somebody, so none of them do. ``LeaveRefused`` becomes a 400
with its message intact -- the refusal text was written to be read by the
operator, not translated on the way out.

**The projection is deliberately outside the decision's transaction.** An
approval that succeeded must not be rolled back because the attendance sheet
could not be written; the day simply stays unprojected and the next run of
``project_approved_leave`` picks it up. That is why the sweep exists.
"""

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from employee_hierarchy.access import has_any, viewer_employee
from employee_hierarchy.constants import IN_SERVICE_STATUSES
from employee_hierarchy.models import Employee

from .balance import balances_for
from .constants import LeaveRequestStatus
from .models import Holiday, LeaveRequest, LeaveType
from .notifications import notify_applicant, notify_approver
from .permissions import (
    CanAccessLeave,
    CanApplyLeave,
    CanCancelApprovedLeave,
    CanDecideLeave,
    CanManageLeaveTypes,
    CanViewLeave,
    CanViewTeamLeave,
)
from .projection import project_request, unproject_request
from .routing import (
    APPLY_FOR_OTHERS,
    DECIDE_ANY,
    authority_of,
    can_apply_for,
    can_cancel,
    decidable_filter,
    visible_filter,
)
from .serializers import (
    ApplyLeaveSerializer,
    DecisionSerializer,
    HolidaySerializer,
    LeaveApprovalSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    ReasonSerializer,
)
from .services import LeaveRefused, apply_for_leave, approve, cancel, reject, withdraw

BASE_PERMISSIONS = [IsAuthenticated, HasCompanyContext]


def _company(request):
    return request.company.company


def _requests_for(request):
    return (
        LeaveRequest.objects.filter(company=_company(request))
        .filter(visible_filter(request.user))
        .select_related(
            "employee", "employee__department", "leave_type", "applied_by", "decided_by"
        )
        .prefetch_related("days")
        .distinct()
    )


class LeaveTypeListAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanManageLeaveTypes]

    def get(self, request):
        types = LeaveType.objects.filter(company=_company(request)).order_by(
            "sort_order", "name"
        )
        if request.query_params.get("active_only", "true").lower() != "false":
            types = types.filter(status="ACTIVE")
        return Response(LeaveTypeSerializer(types, many=True).data)

    def post(self, request):
        serializer = LeaveTypeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        leave_type = serializer.save(company=_company(request), created_by=request.user)
        return Response(
            LeaveTypeSerializer(leave_type).data, status=status.HTTP_201_CREATED
        )


class LeaveTypeDetailAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanManageLeaveTypes]

    def patch(self, request, type_id):
        leave_type = get_object_or_404(
            LeaveType, pk=type_id, company=_company(request)
        )
        serializer = LeaveTypeSerializer(leave_type, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)


class HolidayListAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanManageLeaveTypes]

    def get(self, request):
        holidays = Holiday.objects.filter(company=_company(request))
        year = request.query_params.get("year")
        if year:
            holidays = holidays.filter(date__year=year)
        return Response(HolidaySerializer(holidays.order_by("date"), many=True).data)

    def post(self, request):
        serializer = HolidaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        holiday = serializer.save(company=_company(request), created_by=request.user)
        return Response(HolidaySerializer(holiday).data, status=status.HTTP_201_CREATED)


class HolidayDetailAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanManageLeaveTypes]

    def delete(self, request, holiday_id):
        holiday = get_object_or_404(Holiday, pk=holiday_id, company=_company(request))
        holiday.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeaveRequestListAPI(APIView):
    """List what the caller may see, and raise a new application."""

    permission_classes = [*BASE_PERMISSIONS, CanViewLeave]
    # A leave type may require a certificate, so this endpoint has to accept a
    # file. JSON stays available for the ordinary case.
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    #: Nothing in this project paginates, and introducing a different response
    #: shape here just for leave would be its own trap. Instead the list is
    #: bounded: HR reaching across 249 employees for a whole year would
    #: otherwise pull thousands of rows into one page. Narrow with ?from=&to=.
    MAX_ROWS = 500

    def get(self, request):
        queryset = _requests_for(request)

        state = request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state.upper())

        employee_id = request.query_params.get("employee")
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)

        date_from = parse_date(request.query_params.get("from") or "")
        date_to = parse_date(request.query_params.get("to") or "")
        if date_from:
            queryset = queryset.filter(to_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(from_date__lte=date_to)

        if request.query_params.get("mine", "").lower() == "true":
            viewer = viewer_employee(request.user)
            queryset = queryset.filter(employee=viewer) if viewer else queryset.none()

        try:
            limit = min(int(request.query_params.get("limit") or self.MAX_ROWS), self.MAX_ROWS)
        except ValueError:
            limit = self.MAX_ROWS

        serializer = LeaveRequestSerializer(
            queryset[:limit], many=True, context={"request": request}
        )
        return Response(serializer.data)

    def post(self, request):
        if not CanApplyLeave().has_permission(request, self):
            return Response(
                {"detail": "You do not have permission to apply for leave."},
                status=status.HTTP_403_FORBIDDEN,
            )

        form = ApplyLeaveSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        company = _company(request)

        if data.get("employee"):
            employee = get_object_or_404(
                Employee, pk=data["employee"], company=company
            )
        else:
            employee = viewer_employee(request.user, company=company)
            if employee is None:
                return Response(
                    {
                        "detail": (
                            "This login is not linked to an employee record, so it "
                            "cannot apply for its own leave. Ask HR to link it, or "
                            "name an employee explicitly."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not can_apply_for(request.user, employee):
            return Response(
                {"detail": "You may only apply for your own leave."},
                status=status.HTTP_403_FORBIDDEN,
            )

        leave_type = get_object_or_404(
            LeaveType, pk=data["leave_type"], company=company
        )

        try:
            leave_request = apply_for_leave(
                employee=employee,
                leave_type=leave_type,
                from_date=data["from_date"],
                to_date=data["to_date"],
                portion=data["portion"],
                reason=data["reason"],
                contact_number=data.get("contact_number", ""),
                document=data.get("document"),
                applied_by=request.user,
                # Granting leave past an entitlement is a real HR decision.
                # Refusing it outright would push the record onto paper.
                allow_overdraw=has_any(request.user, (DECIDE_ANY,)),
            )
        except LeaveRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Best effort, and outside nothing -- notify_approver swallows its own
        # failures so a dead push service cannot lose an application.
        notify_approver(leave_request)

        return Response(
            LeaveRequestSerializer(leave_request, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class LeaveRequestDetailAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanViewLeave]

    def get(self, request, request_id):
        leave_request = get_object_or_404(_requests_for(request), pk=request_id)
        return Response(
            LeaveRequestSerializer(leave_request, context={"request": request}).data
        )


class LeaveRequestHistoryAPI(APIView):
    permission_classes = [*BASE_PERMISSIONS, CanViewLeave]

    def get(self, request, request_id):
        leave_request = get_object_or_404(_requests_for(request), pk=request_id)
        return Response(
            LeaveApprovalSerializer(leave_request.trail.all(), many=True).data
        )


class PendingLeaveAPI(APIView):
    """The manager's queue: everything they may actually act on."""

    permission_classes = [*BASE_PERMISSIONS, CanViewTeamLeave]

    def get(self, request):
        queryset = (
            LeaveRequest.objects.filter(
                company=_company(request), status=LeaveRequestStatus.PENDING
            )
            .filter(decidable_filter(request.user))
            .select_related("employee", "employee__department", "leave_type")
            .prefetch_related("days")
            .distinct()
        )
        return Response(
            LeaveRequestSerializer(
                queryset, many=True, context={"request": request}
            ).data
        )


class PendingLeaveCountAPI(APIView):
    """Just the number, for the sidebar badge.

    A separate endpoint rather than ``len()`` of the list above: the badge is
    polled from every page by every approver, and pulling the whole queue to
    count it is the mistake the OMS integration already made once and had to
    undo. This is a ``COUNT(*)``.
    """

    permission_classes = [*BASE_PERMISSIONS, CanViewTeamLeave]

    def get(self, request):
        count = (
            LeaveRequest.objects.filter(
                company=_company(request), status=LeaveRequestStatus.PENDING
            )
            .filter(decidable_filter(request.user))
            .distinct()
            .count()
        )
        return Response({"count": count})


class LeaveDecisionAPI(APIView):
    """Approve or reject, chosen by the URL rather than the payload."""

    permission_classes = [*BASE_PERMISSIONS, CanDecideLeave]

    def post(self, request, request_id, decision):
        leave_request = get_object_or_404(
            LeaveRequest.objects.select_related("employee", "leave_type"),
            pk=request_id,
            company=_company(request),
        )

        authority = authority_of(request.user, leave_request)
        if authority is None:
            return Response(
                {
                    "detail": (
                        "You are not this employee's approver. Leave is decided by "
                        "their reporting line, or by HR."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        form = DecisionSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        try:
            if decision == "approve":
                with transaction.atomic():
                    approve(
                        leave_request,
                        user=request.user,
                        comment=data.get("comment", ""),
                        authority=authority,
                        only_dates=data.get("only_dates") or None,
                    )
                # Outside the transaction on purpose -- see the module docstring.
                project_request(leave_request, user=request.user)
            else:
                reject(
                    leave_request,
                    user=request.user,
                    comment=data.get("comment", ""),
                    authority=authority,
                )
        except LeaveRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        leave_request.refresh_from_db()
        notify_applicant(leave_request, decided_by=request.user)
        return Response(
            LeaveRequestSerializer(leave_request, context={"request": request}).data
        )


class LeaveEmployeePickerAPI(APIView):
    """The people the time office may raise an application for.

    Deliberately its own endpoint rather than borrowing the directory's. Raising
    leave on somebody's behalf needs to *find* them, but it does not need their
    salary, their reporting chain or their history -- and pointing this picker
    at `employee-hierarchy/employees/` would have meant granting the time office
    `can_view_employees`, which opens the whole directory module in the sidebar.

    Four fields, filtered to people in service, gated on the one grant that
    makes applying for somebody else legitimate in the first place.
    """

    permission_classes = [*BASE_PERMISSIONS, CanApplyLeave]

    def get(self, request):
        if not has_any(request.user, (APPLY_FOR_OTHERS,)):
            return Response(
                {"detail": "You may only apply for your own leave."},
                status=status.HTTP_403_FORBIDDEN,
            )

        employees = Employee.objects.filter(
            company=_company(request), employment_status__in=IN_SERVICE_STATUSES
        ).select_related("department")

        search = (request.query_params.get("search") or "").strip()
        if search:
            employees = employees.filter(
                Q(full_name__icontains=search) | Q(employee_code__icontains=search)
            )

        return Response(
            [
                {
                    "id": employee.pk,
                    "employee_code": employee.employee_code,
                    "full_name": employee.full_name,
                    "department_name": (
                        employee.department.name if employee.department_id else ""
                    ),
                    "has_login": employee.user_id is not None,
                }
                for employee in employees.order_by("full_name")[:200]
            ]
        )


class LeaveBalanceAPI(APIView):
    """What somebody has left, per leave type, for a year.

    Computed from the approved day rows every time -- see :mod:`leave.balance`
    for why there is no stored counter.
    """

    permission_classes = [*BASE_PERMISSIONS, CanAccessLeave]

    def get(self, request):
        company = _company(request)

        employee_id = request.query_params.get("employee")
        if employee_id:
            employee = get_object_or_404(Employee, pk=employee_id, company=company)
            # Somebody else's balance is their manager's or HR's business, and
            # the reach is the one already defined for their requests.
            visible = LeaveRequest.objects.filter(
                company=company, employee=employee
            ).filter(visible_filter(request.user)).exists()
            viewer = viewer_employee(request.user, company=company)
            is_self = viewer is not None and viewer.pk == employee.pk
            if not (is_self or visible or CanViewTeamLeave().has_permission(request, self)):
                return Response(
                    {"detail": "You may not view that employee's balance."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            employee = viewer_employee(request.user, company=company)
            if employee is None:
                return Response(
                    {
                        "detail": (
                            "This login is not linked to an employee record, so it "
                            "has no leave balance of its own."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            year = int(request.query_params.get("year") or timezone.localdate().year)
        except ValueError:
            return Response(
                {"detail": "year must be a number."}, status=status.HTTP_400_BAD_REQUEST
            )

        return Response(
            {
                "employee": employee.pk,
                "employee_code": employee.employee_code,
                "employee_name": employee.full_name,
                "year": year,
                "balances": balances_for(employee, year),
            }
        )


class LeaveWithdrawAPI(APIView):
    """The applicant taking it back before a decision."""

    permission_classes = [*BASE_PERMISSIONS, CanAccessLeave]

    def post(self, request, request_id):
        leave_request = get_object_or_404(
            LeaveRequest, pk=request_id, company=_company(request)
        )

        is_applicant = leave_request.applied_by_id == request.user.pk or (
            leave_request.employee.user_id == request.user.pk
        )
        if not is_applicant:
            return Response(
                {"detail": "Only the applicant may withdraw a request."},
                status=status.HTTP_403_FORBIDDEN,
            )

        form = DecisionSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            withdraw(
                leave_request,
                user=request.user,
                comment=form.validated_data.get("comment", ""),
            )
        except LeaveRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            LeaveRequestSerializer(leave_request, context={"request": request}).data
        )


class LeaveCancelAPI(APIView):
    """Take back an approval, and unpick whatever reached the sheet."""

    permission_classes = [*BASE_PERMISSIONS, CanCancelApprovedLeave]

    def post(self, request, request_id):
        leave_request = get_object_or_404(
            LeaveRequest.objects.select_related("employee", "leave_type"),
            pk=request_id,
            company=_company(request),
        )

        if not can_cancel(request.user, leave_request):
            return Response(
                {"detail": "You may not cancel this leave."},
                status=status.HTTP_403_FORBIDDEN,
            )

        form = ReasonSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                cancel(
                    leave_request,
                    user=request.user,
                    comment=form.validated_data["comment"],
                )
            reverted, skipped = unproject_request(leave_request, user=request.user)
        except LeaveRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        payload = LeaveRequestSerializer(
            leave_request, context={"request": request}
        ).data
        payload["attendance_reverted"] = reverted
        payload["attendance_left_alone"] = skipped
        return Response(payload)


class LeaveCalendarAPI(APIView):
    """Who is away over a window -- the team view."""

    permission_classes = [*BASE_PERMISSIONS, CanViewTeamLeave]

    def get(self, request):
        date_from = parse_date(request.query_params.get("from") or "")
        date_to = parse_date(request.query_params.get("to") or "")
        if not (date_from and date_to):
            return Response(
                {"detail": "Pass ?from=YYYY-MM-DD&to=YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = (
            _requests_for(request)
            .filter(
                status__in=[LeaveRequestStatus.APPROVED, LeaveRequestStatus.PENDING],
                from_date__lte=date_to,
                to_date__gte=date_from,
            )
            .order_by("from_date")
        )
        return Response(
            LeaveRequestSerializer(
                queryset, many=True, context={"request": request}
            ).data
        )
