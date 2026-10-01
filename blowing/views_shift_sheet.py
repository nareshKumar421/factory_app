"""Shift-sheet entry: the floor's Excel, read and booked as completed runs.

See ``services/shift_sheet.py`` for the sheet's layout and the rules a row is
booked by. Two endpoints:

* ``POST shift-sheet/parse/`` — an uploaded ``file`` read into rows for the
  page's grid. Nothing is matched against runs and nothing is written.
* ``POST shift-sheet/`` — the grid's rows. Without ``commit`` it answers with
  the plan (run number, meter readings, cost, duplicates) and writes nothing;
  with ``commit: true`` it makes the plan again under a lock and books it, or
  books nothing and answers 400 with the plan when a row needs fixing.

Booking a sheet both creates and completes runs, so it takes both rights.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import BlowingMachine
from .permissions import CanCompleteBlowingRun, CanCreateBlowingRun
from .services import shift_sheet

_PERMISSIONS = [IsAuthenticated, HasCompanyContext, CanCreateBlowingRun, CanCompleteBlowingRun]


def _bad(detail, **extra):
    return Response({'detail': detail, **extra}, status=status.HTTP_400_BAD_REQUEST)


class ShiftSheetParseAPI(APIView):
    permission_classes = _PERMISSIONS

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return _bad('Attach the shift sheet as `file`.')
        if upload.size > shift_sheet.MAX_UPLOAD_BYTES:
            return _bad('That file is too large to be a shift sheet (5 MB at most).')
        try:
            parsed = shift_sheet.parse_workbook(upload, request.company.company)
        except shift_sheet.SheetError as exc:
            return _bad(str(exc))
        return Response({'file_name': upload.name, **parsed})


class ShiftSheetAPI(APIView):
    permission_classes = _PERMISSIONS

    def post(self, request):
        company = request.company.company
        rows = request.data.get('rows')
        if not isinstance(rows, list) or not rows:
            return _bad('Send the sheet as `rows`, one entry per shift.')
        if len(rows) > shift_sheet.MAX_ROWS:
            return _bad(f'At most {shift_sheet.MAX_ROWS} rows at a time.')
        try:
            machine_id = int(request.data.get('machine_id'))
        except (TypeError, ValueError):
            return _bad('Pick the machine.')
        machine = BlowingMachine.objects.filter(
            id=machine_id, company=company, is_active=True).first()
        if machine is None:
            return _bad('That blowing machine is not an active machine of this company.')

        commit = request.data.get('commit') in (True, 'true', 'True')
        if not commit:
            plan = shift_sheet.plan_rows(company, machine, rows)
            return Response({'committed': False, **shift_sheet.public_plan(plan)})

        source = str(request.data.get('source') or '').strip()[:120]
        try:
            result = shift_sheet.apply_rows(company, machine.id, rows, request.user, source=source)
        except shift_sheet.ShiftSheetRefused as exc:
            return _bad(str(exc), committed=False, **exc.plan)
        except ValueError as exc:
            return _bad(str(exc), committed=False)
        return Response({'committed': True, **result}, status=status.HTTP_201_CREATED)
