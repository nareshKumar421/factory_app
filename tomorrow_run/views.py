"""
tomorrow_run/views.py

GET  /api/v1/tomorrow-run/plan/            the plan the page shows, with the latest 7 pm check
POST /api/v1/tomorrow-run/choice/          "run this first on that machine" (job="" clears it)
POST /api/v1/tomorrow-run/rebuild/         read everything again now and re-plan
GET  /api/v1/tomorrow-run/sheets/          the planning sheets put in, newest (in charge) first
POST /api/v1/tomorrow-run/sheets/          put in a planning sheet (multipart: file, stock_date?)
GET  /api/v1/tomorrow-run/sheets/<id>/     one sheet with its lines

Every endpoint needs a JWT, the Company-Code header and one of this app's rights.
"""

import logging

from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import constants as C
from . import services
from .inputs import InputsUnavailable, user_name
from .models import PlanningSheet
from .permissions import CanManageTomorrowRun, CanPickTomorrowRun, CanViewTomorrowRun, MANAGE, PICK
from .serializers import ChoiceSerializer, PlanningSheetSerializer, SheetUploadSerializer
from .sheet_parser import SheetError

logger = logging.getLogger(__name__)


def _company(request):
    return request.company.company


def envelope(request, plan):
    company = _company(request)
    return {
        "plan": plan.plan if plan else None,
        "check": services.check_payload(services.latest_check(company)),
        "meta": {
            "plan_id": plan.id if plan else None,
            "read_at": plan.read_at.isoformat() if plan else None,
            "trigger": plan.trigger if plan else None,
            "built_by": user_name(plan.built_by) if plan and plan.built_by else "the 7 pm read",
            "can_pick": request.user.has_perm(PICK),
            "can_manage": request.user.has_perm(MANAGE),
            "reasons": list(C.PICK_REASONS),
            "you": user_name(request.user),
        },
    }


class _API(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTomorrowRun]


class PlanAPI(_API):
    def get(self, request):
        return Response(envelope(request, services.current(_company(request))))


class ChoiceAPI(_API):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPickTomorrowRun]

    def post(self, request):
        s = ChoiceSerializer(data=request.data)
        if not s.is_valid():
            return Response({"detail": "Invalid pick.", "errors": s.errors}, status=status.HTTP_400_BAD_REQUEST)
        d = s.validated_data
        plan = services.current(_company(request))
        if plan is None or plan.for_date != d["for_date"]:
            return Response(
                {"detail": "The page is showing an older plan than the one now in charge. Reload it and pick again."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            plan = services.pick(plan, machine=d["machine"], job=d["job"], why=d.get("why") or "",
                                 other=d.get("other") or "", user=request.user)
        except services.PickRefused as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(envelope(request, plan))


class RebuildAPI(_API):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageTomorrowRun]

    def post(self, request):
        try:
            plan = services.build(_company(request), user=request.user, trigger="manual")
        except InputsUnavailable as e:
            return Response(
                {"detail": f"The plan was not read again — {e}. The plan already in charge stands."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(envelope(request, plan))


class SheetListAPI(_API):
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanManageTomorrowRun()]
        return super().get_permissions()

    def get(self, request):
        sheets = PlanningSheet.objects.filter(company=_company(request)).select_related("uploaded_by")[:24]
        in_charge = sheets[0].id if sheets else None
        data = PlanningSheetSerializer(sheets, many=True, context={"request": request, "in_charge_id": in_charge}).data
        return Response({"results": data})

    def post(self, request):
        s = SheetUploadSerializer(data=request.data)
        if not s.is_valid():
            return Response({"detail": "Invalid planning sheet.", "errors": s.errors},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            sheet = services.put_in_sheet(_company(request), s.validated_data["file"], user=request.user,
                                          stock_date=s.validated_data.get("stock_date"))
        except (SheetError, ValueError) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        data = PlanningSheetSerializer(sheet, context={"request": request, "in_charge_id": sheet.id}).data
        data["lines"] = [
            {"row": ln.row, "code": ln.code, "name": ln.name, "plan_l": ln.plan_l, "ecom_l": ln.ecom_l,
             "stock_l": ln.stock_l, "net_l": ln.net_l, "machine": ln.machine}
            for ln in sheet.lines.all()
        ]
        return Response(data, status=status.HTTP_201_CREATED)


class SheetDetailAPI(_API):
    def get(self, request, pk):
        sheet = PlanningSheet.objects.filter(company=_company(request), pk=pk).first()
        if sheet is None:
            return Response({"detail": "No such planning sheet."}, status=status.HTTP_404_NOT_FOUND)
        newest = PlanningSheet.objects.filter(company=_company(request)).values_list("id", flat=True).first()
        data = PlanningSheetSerializer(sheet, context={"request": request, "in_charge_id": newest}).data
        data["lines"] = [
            {"row": ln.row, "code": ln.code, "name": ln.name, "plan_l": ln.plan_l, "ecom_l": ln.ecom_l,
             "stock_l": ln.stock_l, "net_l": ln.net_l, "machine": ln.machine}
            for ln in sheet.lines.all()
        ]
        return Response(data)
