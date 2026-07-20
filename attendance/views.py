from django.db.models import ProtectedError
from django.http import HttpResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Employee, AttendanceRecord
from .serializers import EmployeeSerializer, AttendanceRecordSerializer
from .permissions import CanManageEmployee, CanManageAttendance


# -------------------------------------------------
# Employee master (attendance config)
# -------------------------------------------------
class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated, CanManageEmployee]

    def get_queryset(self):
        qs = Employee.objects.select_related("department").all()

        active = self.request.query_params.get("is_active")
        if active in ("true", "false"):
            qs = qs.filter(is_active=(active == "true"))

        department = self.request.query_params.get("department")
        if department:
            qs = qs.filter(department_id=department)

        search = self.request.query_params.get("search")
        if search:
            from django.db.models import Q

            qs = qs.filter(
                Q(name__icontains=search) | Q(employee_code__icontains=search)
            )
        return qs

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {
                    "detail": (
                        "This employee has attendance records and cannot be "
                        "deleted. Mark them inactive instead."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


# -------------------------------------------------
# Attendance records
# -------------------------------------------------
class AttendanceRecordViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceRecordSerializer
    permission_classes = [IsAuthenticated, CanManageAttendance]

    def get_queryset(self):
        qs = AttendanceRecord.objects.select_related(
            "employee", "employee__department", "created_by"
        ).all()

        date = self.request.query_params.get("date")
        if date:
            qs = qs.filter(date=date)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(date__lte=date_to)

        employee = self.request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)

        department = self.request.query_params.get("department")
        if department:
            qs = qs.filter(employee__department_id=department)

        direction = self.request.query_params.get("direction")
        if direction in (AttendanceRecord.Direction.IN, AttendanceRecord.Direction.OUT):
            qs = qs.filter(direction=direction)

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=["get"])
    def export(self, request):
        """
        Export attendance records to .xlsx for a date range.
        Query params: date_from, date_to (both YYYY-MM-DD; optional), and the
        same filters as the list endpoint (employee, department, direction).
        """
        import openpyxl
        from openpyxl.styles import Font

        # Reuse list filtering, then order chronologically for the sheet.
        records = self.filter_queryset(self.get_queryset()).order_by("date", "time")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Attendance"

        headers = ["Date", "Time", "Direction", "Employee Code", "Name", "Department", "Marked By"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        for r in records:
            ws.append([
                r.date.strftime("%Y-%m-%d"),
                r.time.strftime("%H:%M"),
                r.get_direction_display(),
                r.employee.employee_code,
                r.employee.name,
                r.employee.department.name if r.employee.department_id else "",
                r.created_by.full_name if r.created_by else "",
            ])

        # Auto-ish column widths.
        for column_cells in ws.columns:
            width = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
            ws.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 40)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from and date_to:
            span = f"{date_from}_to_{date_to}"
        else:
            span = date_from or date_to or "all"
        filename = f"attendance_{span}.xlsx"

        response = HttpResponse(
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response
