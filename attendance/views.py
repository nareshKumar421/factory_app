"""
The attendance API.

Three viewsets: the daily sheet (the main one), the read-only employee list the
screens pick from, and the manual photographed marks kept for when the machine
itself is down.

The daily sheet's list endpoint answers one screen: *one date, everybody,
filterable*. It always returns both statuses on every row -- see
:mod:`attendance.serializers` for why the "machine only" default is a column
choice in the browser and not a narrower response.
"""

import calendar
from datetime import timedelta

from django.db.models import ProtectedError, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from employee_hierarchy.constants import IN_SERVICE_STATUSES
from employee_hierarchy.models import Employee

from . import punch_store, services
from .models import (
    AttendanceRecord,
    AttendanceStatus,
    DailyAttendance,
    OverrideReason,
)
from .permissions import (
    CanManageAttendance,
    CanOverrideAttendance,
    CanSyncAttendance,
    CanViewAttendance,
)
from .serializers import (
    AttendanceEmployeeSerializer,
    AttendanceRecordSerializer,
    DailyAttendanceSerializer,
    OverrideLogSerializer,
    OverrideRequestSerializer,
    RevertRequestSerializer,
)


def _parse_date(value):
    if not value:
        return None
    try:
        return timezone.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_month(value):
    """``"2026-09"`` -> (first, last) of that month, or ``None``.

    Built on :func:`_parse_date` rather than a second strptime, so the two can
    never drift on what counts as a valid date.
    """
    if not value:
        return None
    first = _parse_date(f"{value}-01")
    if first is None:
        return None
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


class AttendanceEmployeeViewSet(viewsets.ReadOnlyModelViewSet):
    """The directory, as the attendance screens need it. Read-only on purpose.

    Employees are created and edited in ``employee_hierarchy``. This module once
    had its own master and the two could disagree about who worked here, which
    meant a real employee whose punches matched nothing.
    """

    serializer_class = AttendanceEmployeeSerializer
    permission_classes = [IsAuthenticated, CanViewAttendance]
    pagination_class = None

    def get_queryset(self):
        queryset = (
            Employee.objects.select_related("department", "designation")
            .filter(employment_status__in=IN_SERVICE_STATUSES)
            .order_by("full_name")
        )
        department = self.request.query_params.get("department")
        if department:
            queryset = queryset.filter(department_id=department)

        segment = self.request.query_params.get("sap_segment")
        if segment:
            queryset = queryset.filter(sap_segment__iexact=segment)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(full_name__icontains=search) | Q(employee_code__icontains=search)
            )
        return queryset


class DailyAttendanceViewSet(viewsets.ReadOnlyModelViewSet):
    """One employee-day per row, with both statuses on it.

    Read-only as a viewset: a status changes only through :meth:`override` or
    :meth:`revert`, which demand a reason and write the append-only trail. A
    plain PATCH would skip both, which is the whole thing this module is for.
    """

    serializer_class = DailyAttendanceSerializer
    permission_classes = [IsAuthenticated, CanOverrideAttendance]
    pagination_class = None

    def get_queryset(self):
        queryset = DailyAttendance.objects.select_related(
            "employee", "employee__department", "overridden_by"
        )

        date = _parse_date(self.request.query_params.get("date"))
        date_from = _parse_date(self.request.query_params.get("date_from"))
        date_to = _parse_date(self.request.query_params.get("date_to"))
        if date:
            queryset = queryset.filter(date=date)
        else:
            if date_from:
                queryset = queryset.filter(date__gte=date_from)
            if date_to:
                queryset = queryset.filter(date__lte=date_to)
            if not date_from and not date_to and self.action == "list":
                # Never serve the whole history by accident: 300 people times
                # two years is 200,000 rows and the screen only ever wants one
                # day. An unfiltered *list* means today.
                #
                # Only the list: a detail route looks a row up by primary key,
                # and narrowing that to today would 404 every correction made
                # to a day that has already closed -- which is most of them.
                queryset = queryset.filter(date=timezone.localdate())

        employee = self.request.query_params.get("employee")
        if employee:
            queryset = queryset.filter(employee_id=employee)

        department = self.request.query_params.get("department")
        if department:
            queryset = queryset.filter(employee__department_id=department)

        segment = self.request.query_params.get("sap_segment")
        if segment:
            queryset = queryset.filter(employee__sap_segment__iexact=segment)

        # Filtering by status means the effective one -- what stands. The
        # machine's own reading is filterable separately, for "who did the
        # machine miss today?".
        effective = self.request.query_params.get("status")
        if effective:
            queryset = queryset.filter(effective_status=effective)

        machine = self.request.query_params.get("machine_status")
        if machine:
            queryset = queryset.filter(machine_status=machine)

        overridden = self.request.query_params.get("is_overridden")
        if overridden in ("true", "false"):
            queryset = queryset.filter(is_overridden=(overridden == "true"))

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q

            queryset = queryset.filter(
                Q(employee__full_name__icontains=search)
                | Q(employee__employee_code__icontains=search)
            )
        return queryset.order_by("employee__full_name")

    # -- corrections -----------------------------------------------------

    @action(detail=True, methods=["post"])
    def override(self, request, pk=None):
        """Change what stands, with a reason. The machine's reading is untouched."""
        row = self.get_object()
        payload = OverrideRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            services.override_status(
                row,
                status=payload.validated_data["status"],
                reason_code=payload.validated_data["reason_code"],
                reason=payload.validated_data["reason"],
                user=request.user,
            )
        except services.OverrideRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"])
    def revert(self, request, pk=None):
        """Drop the correction and go back to the machine's reading."""
        row = self.get_object()
        payload = RevertRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            services.revert_to_machine(
                row, reason=payload.validated_data.get("reason", ""), user=request.user
            )
        except services.OverrideRefused as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Every change ever made to this day, newest first."""
        row = self.get_object()
        entries = row.override_log.select_related("performed_by")
        return Response(OverrideLogSerializer(entries, many=True).data)

    # -- the rest of the screen ------------------------------------------

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the filtered set, by machine reading and by what stands."""
        return Response(services.summarise(self.filter_queryset(self.get_queryset())))

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated, CanViewAttendance])
    def muster(self, request):
        """One month as a register: a row per employee, a cell per day.

        The daily sheet answers "who turned up today". This answers "what did
        September look like", which is the shape payroll is actually run from
        and the only way a pattern -- a man who is `MISSING_PUNCH` every Tuesday
        -- is visible at all.

        **Three states per cell, and conflating any two of them is the bug this
        endpoint exists to avoid.**

        * a synced day is ``{"id": .., "m": ..}``, with ``"e"`` added *only*
          when the day was corrected
        * a day inside the person's service that nobody has synced is ``null``
        * a day outside it -- before they joined, after they left -- has no key

        ``null`` is not ``ABSENT``. A month where the sync has not run since
        Tuesday would otherwise show three hundred people absent for a fortnight,
        which is exactly the confusion ``source_status`` exists to prevent on the
        daily sheet.

        ``"e"`` keyed off ``is_overridden`` and not off ``e != m``: HR may
        re-affirm the machine's own reading as a correction (the machine was
        right, and somebody has now said so on the record), and that day is
        still overridden even though the two values match.
        """
        window = _parse_month(request.query_params.get("month"))
        if window is None:
            today = timezone.localdate()
            window = _parse_month(f"{today.year}-{today.month:02d}")
        month_start, month_end = window
        days_in_month = month_end.day

        # Who belongs on this month's roll -- deliberately NOT
        # ``IN_SERVICE_STATUSES``. Somebody who resigned on the 20th still
        # worked the first nineteen days, and filtering by today's status would
        # erase the days they were actually here.
        employees = (
            Employee.objects.filter(joining_date__lte=month_end)
            .filter(Q(exit_date__isnull=True) | Q(exit_date__gte=month_start))
            .select_related("department")
        )

        department = request.query_params.get("department")
        if department:
            employees = employees.filter(department_id=department)
        segment = request.query_params.get("sap_segment")
        if segment:
            employees = employees.filter(sap_segment__iexact=segment)
        employee_id = request.query_params.get("employee")
        if employee_id:
            employees = employees.filter(pk=employee_id)
        search = request.query_params.get("search")
        if search:
            employees = employees.filter(
                Q(full_name__icontains=search) | Q(employee_code__icontains=search)
            )
        employees = employees.order_by("full_name")

        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(500, max(1, int(request.query_params.get("page_size", 250))))
        except ValueError:
            return Response(
                {"detail": "page and page_size must be whole numbers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        total = employees.count()
        # Materialised to a list, so the ids below are plain integers rather
        # than an offset/limit subquery fed back into another filter.
        page_employees = list(employees[(page - 1) * page_size : page * page_size])
        page_ids = [employee.pk for employee in page_employees]

        # One fetch for the page's whole month. ``values()`` on purpose: the
        # employee and department are written once per row below, so joining
        # them onto every one of ~7,600 cells would be duplicate data on the
        # wire for nothing.
        cells = DailyAttendance.objects.filter(
            employee_id__in=page_ids, date__range=(month_start, month_end)
        ).values("id", "employee_id", "date", "machine_status", "effective_status", "is_overridden")

        by_employee = {}
        for cell in cells:
            by_employee.setdefault(cell["employee_id"], {})[cell["date"].day] = cell

        data = []
        for employee in page_employees:
            found = by_employee.get(employee.pk, {})
            days, totals = {}, {}
            for day in range(1, days_in_month + 1):
                on_date = month_start.replace(day=day)
                cell = found.get(day)
                if cell is None:
                    # No row. Only now does the service window decide whether
                    # this is a day nobody synced or a day that was never
                    # theirs. A real row is never suppressed by the window:
                    # ``joining_date`` defaults to the day the directory was
                    # imported, so for most of this workforce it is a stand-in
                    # rather than a fact, and a recorded punch outranks it.
                    if on_date < employee.joining_date or (
                        employee.exit_date and on_date > employee.exit_date
                    ):
                        continue  # not theirs to have -- no key at all
                    days[str(day)] = None
                    totals["not_synced"] = totals.get("not_synced", 0) + 1
                    continue
                compact = {"id": cell["id"], "m": cell["machine_status"]}
                if cell["is_overridden"]:
                    compact["e"] = cell["effective_status"]
                days[str(day)] = compact
                standing = cell["effective_status"]
                totals[standing] = totals.get(standing, 0) + 1
            data.append(
                {
                    "employee": employee.pk,
                    "employee_code": employee.employee_code,
                    "employee_name": employee.full_name,
                    "department_name": employee.department.name if employee.department else None,
                    "sap_segment": employee.sap_segment,
                    "days": days,
                    "totals": totals,
                }
            )

        # The plant-wide counts are over everybody the filters matched, not over
        # this page, so paging never moves them. ``summarise`` already does the
        # grouped scans; there is no reason to count these twice.
        meta = services.summarise(
            DailyAttendance.objects.filter(
                employee__in=employees, date__range=(month_start, month_end)
            )
        )
        meta["employee_count"] = total

        return Response(
            {
                "month": f"{month_start.year}-{month_start.month:02d}",
                "date_from": month_start,
                "date_to": month_end,
                "days_in_month": days_in_month,
                "meta": meta,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (total + page_size - 1) // page_size or 1,
                },
                "data": data,
            }
        )

    @action(detail=False, methods=["get"])
    def reasons(self, request):
        """The override vocabulary, so the client never hardcodes it."""
        return Response(
            {
                "statuses": [
                    {"value": value, "label": label} for value, label in AttendanceStatus.choices
                ],
                "reason_codes": [
                    {"value": value, "label": label} for value, label in OverrideReason.choices
                ],
            }
        )

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated, CanViewAttendance])
    def source_status(self, request):
        """Is the punch data current, and how fresh is it?

        Worth its own endpoint: a dashboard full of absences looks identical
        whether the factory was closed or the sync has not run since Tuesday,
        and the screen should be able to say which.

        The punch machines are not reachable from this server, so this no longer
        probes them. It reports on the agent that copies punches in from inside
        the plant -- ``reachable`` means "the punch data is current", and
        ``last_agent_run`` says when that was last established.
        """
        info = punch_store.health()
        latest = (
            DailyAttendance.objects.order_by("-synced_at")
            .values_list("synced_at", flat=True)
            .first()
        )
        info["last_sync"] = latest
        return Response(info)

    @action(detail=False, methods=["post"], permission_classes=[IsAuthenticated, CanSyncAttendance])
    def sync(self, request):
        """Roll up the stored punches for a date range on demand.

        Defaults to today and yesterday: a late punch-out lands after midnight,
        so today's row is not final until tomorrow has started.

        This re-derives from punches already copied in; it does not reach the
        machines, and cannot make punches appear that the agent has not brought
        across yet. ``source_status`` is what says whether that has happened.
        """
        date_to = _parse_date(request.data.get("date_to")) or timezone.localdate()
        date_from = _parse_date(request.data.get("date_from")) or (date_to - timedelta(days=1))
        if date_from > date_to:
            return Response(
                {"detail": "date_from is after date_to."}, status=status.HTTP_400_BAD_REQUEST
            )
        # The screen only ever needs a few days, and a typo should not become a
        # two-year scan of the punch table.
        if (date_to - date_from).days > 92:
            return Response(
                {"detail": "Sync at most 92 days at a time; use the management command for more."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # No 503 branch any more: the punches are in our own database, so this
        # cannot fail for want of a factory link. It can legitimately roll up
        # nothing, which is what an agent that has not run looks like.
        totals = services.sync_range(date_from, date_to)
        return Response(totals)

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Export the filtered sheet to .xlsx, both statuses side by side."""
        import openpyxl
        from openpyxl.styles import Font

        rows = self.filter_queryset(self.get_queryset())

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Attendance"
        headers = [
            "Date", "Employee Code", "Name", "Department",
            "Machine Status", "First Punch", "Last Punch", "Punches", "Worked (min)",
            "Final Status", "Changed", "Reason", "Note", "Changed By",
        ]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for row in rows:
            sheet.append([
                row.date.strftime("%Y-%m-%d"),
                row.employee.employee_code,
                row.employee.full_name,
                row.employee.department.name if row.employee.department_id else "",
                row.get_machine_status_display(),
                row.machine_first_punch.strftime("%H:%M") if row.machine_first_punch else "",
                row.machine_last_punch.strftime("%H:%M") if row.machine_last_punch else "",
                row.machine_punch_count,
                row.machine_worked_minutes,
                row.get_effective_status_display(),
                "Yes" if row.is_overridden else "",
                row.get_override_reason_code_display() if row.override_reason_code else "",
                row.override_reason,
                row.overridden_by.full_name if row.overridden_by_id else "",
            ])

        for column_cells in sheet.columns:
            width = max(
                (len(str(c.value)) for c in column_cells if c.value is not None), default=10
            )
            sheet.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 40)

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        span = request.query_params.get("date") or "range"
        response["Content-Disposition"] = f'attachment; filename="attendance_{span}.xlsx"'
        book.save(response)
        return response


class AttendanceRecordViewSet(viewsets.ModelViewSet):
    """Manual, photographed gate marks -- the fallback when the machine is down."""

    serializer_class = AttendanceRecordSerializer
    permission_classes = [IsAuthenticated, CanManageAttendance]

    def get_queryset(self):
        queryset = AttendanceRecord.objects.select_related(
            "employee", "employee__department", "created_by"
        ).all()

        date = self.request.query_params.get("date")
        if date:
            queryset = queryset.filter(date=date)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            queryset = queryset.filter(date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            queryset = queryset.filter(date__lte=date_to)

        employee = self.request.query_params.get("employee")
        if employee:
            queryset = queryset.filter(employee_id=employee)

        department = self.request.query_params.get("department")
        if department:
            queryset = queryset.filter(employee__department_id=department)

        direction = self.request.query_params.get("direction")
        if direction in (AttendanceRecord.Direction.IN, AttendanceRecord.Direction.OUT):
            queryset = queryset.filter(direction=direction)

        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This record cannot be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Export manual marks to .xlsx for a date range."""
        import openpyxl
        from openpyxl.styles import Font

        records = self.filter_queryset(self.get_queryset()).order_by("date", "time")

        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Attendance"
        headers = ["Date", "Time", "Direction", "Employee Code", "Name", "Department", "Marked By"]
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for record in records:
            sheet.append([
                record.date.strftime("%Y-%m-%d"),
                record.time.strftime("%H:%M"),
                record.get_direction_display(),
                record.employee.employee_code,
                record.employee.full_name,
                record.employee.department.name if record.employee.department_id else "",
                record.created_by.full_name if record.created_by else "",
            ])

        for column_cells in sheet.columns:
            width = max(
                (len(str(c.value)) for c in column_cells if c.value is not None), default=10
            )
            sheet.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 40)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from and date_to:
            span = f"{date_from}_to_{date_to}"
        else:
            span = date_from or date_to or "all"

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="attendance_{span}.xlsx"'
        book.save(response)
        return response
