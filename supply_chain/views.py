"""Supply-chain API.

One dashboard endpoint (the brief's "single brain"), three reference-data
endpoints, a template upload, and the policy that settles the brief's open
questions.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SupplyChainPolicy,
)
from .permissions import CanManageSupplyChainReference, CanViewSupplyChain
from .serializers import (
    DashboardQuerySerializer,
    MachineCapacitySerializer,
    MaterialLeadTimeSerializer,
    MaterialMachineMapSerializer,
    ReferenceImportSerializer,
    SupplyChainPolicySerializer,
)
from .services import SupplyChainError
from .services import planning as planning_service
from .services.alarms import send_supply_chain_alarms
from .services.live_trail import build_live_trail
from .services.template_import import import_reference_workbook

logger = logging.getLogger(__name__)


def _company_code(request):
    return request.company.company.code


class SupplyChainBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSupplyChain]

    def handle_exception(self, exc):
        if isinstance(exc, SupplyChainError):
            return Response(
                {"detail": exc.message, "code": exc.code}, status=exc.status_code
            )
        return super().handle_exception(exc)


class SupplyChainDashboardAPI(SupplyChainBaseView):
    """Steps 6 + 7 in one payload: what to order today, and whether the plan runs."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.dashboard(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class LiveTrailAPI(SupplyChainBaseView):
    """The whole order book, order to purchase order, read live from SAP.

    Not scoped to the caller's company on purpose. The trail is a group view:
    demand comes from every book the factory fills — Oil and Mart — while
    production, stock and procurement come from Oil, which is the only place
    JIVO makes anything. Reading it from the Mart context would be the same
    picture, so it is built once and the same way whoever asks.
    """

    def get(self, request):
        return Response(build_live_trail(scope=request.query_params.get("scope")))


class ProcurementAlarmsAPI(SupplyChainBaseView):
    """Step 6 alone — the procurement action list, most urgent first."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.material_alarms(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class CapacityCheckAPI(SupplyChainBaseView):
    """Step 7 alone — per-line feasibility of the plan."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.capacity_check(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class LeadTimeListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MaterialLeadTime.objects.filter(company_code=_company_code(request))
        return Response(MaterialLeadTimeSerializer(qs, many=True).data)


class MachineCapacityListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MachineCapacity.objects.filter(company_code=_company_code(request))
        return Response(MachineCapacitySerializer(qs, many=True).data)


class MaterialMachineMapListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MaterialMachineMap.objects.filter(company_code=_company_code(request))
        return Response(MaterialMachineMapSerializer(qs, many=True).data)


class SupplyChainPolicyAPI(SupplyChainBaseView):
    """Read the policy with GET; changing it needs the reference-data permission."""

    def get(self, request):
        policy = SupplyChainPolicy.for_company(_company_code(request))
        return Response(SupplyChainPolicySerializer(policy).data)

    def put(self, request):
        for permission in (CanManageSupplyChainReference(),):
            if not permission.has_permission(request, self):
                return Response(
                    {"detail": "You cannot change the supply chain policy.",
                     "code": "FORBIDDEN"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        company_code = _company_code(request)
        policy, _ = SupplyChainPolicy.objects.get_or_create(company_code=company_code)
        serializer = SupplyChainPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ReferenceTemplateUploadAPI(APIView):
    """Upload the JIVO Supply Chain Reference Template workbook."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageSupplyChainReference]

    def handle_exception(self, exc):
        if isinstance(exc, SupplyChainError):
            return Response(
                {"detail": exc.message, "code": exc.code}, status=exc.status_code
            )
        return super().handle_exception(exc)

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "Attach the reference template as 'file'.", "code": "NO_FILE"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record = import_reference_workbook(
            _company_code(request), upload.read(),
            filename=getattr(upload, "name", ""), user=request.user,
        )
        return Response(
            ReferenceImportSerializer(record).data, status=status.HTTP_201_CREATED
        )


class ReferenceImportHistoryAPI(SupplyChainBaseView):
    def get(self, request):
        qs = ReferenceImport.objects.filter(company_code=_company_code(request))[:25]
        return Response(ReferenceImportSerializer(qs, many=True).data)


class FloorAuditAPI(SupplyChainBaseView):
    """Where the procedure's min_stock differs from the brief's own 35% rule.

    "Buffers erode unnoticed" is one of the five problems the brief names — this
    is what noticing looks like.
    """

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.floor_audit(
            _company_code(request), forecast_id=query.validated_data.get("forecast_id"),
        ))


class FloorConventionAPI(SupplyChainBaseView):
    """Evidence for whether the floor is ADDED to demand or SUBTRACTED.

    The brief's steps 3 and 5 contradict each other. This infers which the
    procedure actually does, from the numbers it already returns.
    """

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.floor_convention_audit(
            _company_code(request), forecast_id=query.validated_data.get("forecast_id"),
        ))


class AlarmPreviewAPI(SupplyChainBaseView):
    """What each department WOULD be sent, without sending it."""

    def get(self, request):
        return Response({"subscriptions": send_supply_chain_alarms(
            _company_code(request), company=request.company.company, dry_run=True,
        )})


class AlarmSendAPI(APIView):
    """Send the alarms now. Normally a cron runs this; this is the manual push."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageSupplyChainReference]

    def post(self, request):
        return Response({"subscriptions": send_supply_chain_alarms(
            _company_code(request), company=request.company.company,
            force=bool(request.data.get("force")),
        )})


# ── The daily operating loop ──────────────────────────────────────────────────

from .models import DailyRun, DailyRunRow, MonitoredSku, OperatingParameters  # noqa: E402
from .serializers import (  # noqa: E402
    DailyRunRowSerializer,
    DailyRunSerializer,
    DataQualityIssueSerializer,
    MonitoredSkuSerializer,
    OperatingParametersSerializer,
    VerdictInputSerializer,
)
from .services import operations as ops  # noqa: E402
from .services.daily_run import build_daily_run  # noqa: E402


def _run_payload(run):
    """A run and everything needed to act on it, in one response.

    One payload rather than four calls: the morning routine is read top to
    bottom, and a screen that loads its own alarm count separately from its own
    data-quality list can show them disagreeing.
    """
    return {
        "run": DailyRunSerializer(run).data,
        "rows": DailyRunRowSerializer(
            run.rows.select_related("row_verdict"), many=True
        ).data,
        "issues": DataQualityIssueSerializer(run.issues.all(), many=True).data,
        "verdict_progress": ops.verdict_progress(run),
        "unassigned_red_rows": ops.unassigned_red_rows(run).count(),
    }


class DailyRunListAPI(SupplyChainBaseView):
    """Recent runs, newest first."""

    def get(self, request):
        qs = DailyRun.objects.filter(company_code=_company_code(request))[:30]
        return Response(DailyRunSerializer(qs, many=True).data)


class DailyRunDetailAPI(SupplyChainBaseView):
    """One run — by id, by ``?date=``, or the latest."""

    def get(self, request, run_id=None):
        run = ops.get_run(
            _company_code(request),
            run_date=request.query_params.get("date") or None,
            run_id=run_id,
        )
        return Response(_run_payload(run))


class DailyRunGenerateAPI(APIView):
    """Build today's run. Normally the 07:30 job; this is the manual trigger."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageSupplyChainReference]

    def handle_exception(self, exc):
        if isinstance(exc, SupplyChainError):
            return Response({"detail": exc.message, "code": exc.code}, status=exc.status_code)
        return super().handle_exception(exc)

    def post(self, request):
        run = build_daily_run(
            _company_code(request), run_date=request.data.get("date") or None
        )
        return Response(_run_payload(run), status=status.HTTP_201_CREATED)


class DailyRunReviewAPI(DailyRunGenerateAPI):
    """The analyst's 08:00 check."""

    def post(self, request, run_id):
        run = ops.get_run(_company_code(request), run_id=run_id)
        ops.review_run(
            run, user=request.user,
            comment=request.data.get("comment", ""),
            override=bool(request.data.get("override")),
        )
        return Response(_run_payload(run))


class DailyRunPublishAPI(DailyRunGenerateAPI):
    """Send it to the buyer and the HODs."""

    def post(self, request, run_id):
        run = ops.get_run(_company_code(request), run_id=run_id)
        run, message = ops.publish_run(
            run, user=request.user, company=request.company.company,
            comment=request.data.get("comment", ""),
        )
        payload = _run_payload(run)
        payload["message"] = message
        return Response(payload)


class DailyRunRowOwnerAPI(DailyRunGenerateAPI):
    """Put a name against a red row — an alarm nobody owns will not get done."""

    def post(self, request, row_id):
        row = DailyRunRow.objects.filter(
            id=row_id, run__company_code=_company_code(request)
        ).first()
        if row is None:
            return Response({"detail": "Row not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        ops.assign_owner(row, request.data.get("owner", ""), user=request.user)
        return Response(DailyRunRowSerializer(row).data)


class DailyRunVerdictAPI(SupplyChainBaseView):
    """The buyer's answer after the phone call.

    Only the view permission is required: the person who made the call is the one
    who knows what happened, and putting this behind an admin permission is how
    the verdict log ends up empty.
    """

    def post(self, request, row_id):
        row = DailyRunRow.objects.filter(
            id=row_id, run__company_code=_company_code(request)
        ).first()
        if row is None:
            return Response({"detail": "Row not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        payload = VerdictInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ops.record_verdict(
            row, payload.validated_data["outcome"],
            note=payload.validated_data.get("note", ""),
            promised_date=payload.validated_data.get("supplier_promised_date"),
            user=request.user,
        )
        row.refresh_from_db()
        return Response(DailyRunRowSerializer(row).data)


class WeeklyReviewAPI(SupplyChainBaseView):
    """The Monday step — is the system getting more trustworthy, or less?"""

    def get(self, request):
        weeks = int(request.query_params.get("weeks", 4))
        return Response(ops.weekly_review(_company_code(request), weeks=weeks))


class MonitoredSkuListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MonitoredSku.objects.filter(
            company_code=_company_code(request)
        ).prefetch_related("components")
        return Response(MonitoredSkuSerializer(qs, many=True).data)


class OperatingParametersAPI(SupplyChainBaseView):
    """Read with the view permission; change needs manage AND a written reason."""

    def get(self, request):
        params = OperatingParameters.for_company(_company_code(request))
        return Response(OperatingParametersSerializer(params).data)

    def put(self, request):
        if not CanManageSupplyChainReference().has_permission(request, self):
            return Response({"detail": "You cannot change the operating parameters.",
                             "code": "FORBIDDEN"}, status=status.HTTP_403_FORBIDDEN)
        company_code = _company_code(request)
        params, _ = OperatingParameters.objects.get_or_create(company_code=company_code)
        serializer = OperatingParametersSerializer(params, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=getattr(request.user, "email", "") or "")
        return Response(serializer.data)
