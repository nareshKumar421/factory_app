"""SAP's open purchase orders and the planning team's monthly plan workbook,
both moved here from EXIM.

Same shape as the rest of the module: a JWT, the ``Company-Code`` header and a
right; SAP failures map as ``PlanningBaseView`` maps them.
"""

from datetime import datetime

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext

from . import open_pos
from .models_monthly_plan import MonthlyPlanRow, MonthlyPlanUpload
from .monthly_plan_parser import PlanningParseError, parse_planning_workbook
from .permissions import CanRemoveMonthlyPlan, CanUploadMonthlyPlan, CanViewMonthlyPlan, CanViewOpenPOs
from .views import PlanningBaseView

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class OpenPurchaseOrdersAPI(PlanningBaseView):
    """GET [?refresh=1] — every open PO line in SAP for the company."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOpenPOs]

    def get(self, request):
        refresh = request.query_params.get("refresh") in ("1", "true")
        return Response(open_pos.open_pos(request.company.company, refresh=refresh))


# ---------------------------------------------------------------------------
# The monthly plan
# ---------------------------------------------------------------------------


class MonthlyPlanUploadSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField()
    is_latest = serializers.SerializerMethodField()

    class Meta:
        model = MonthlyPlanUpload
        fields = [
            "id", "month", "version", "title", "source_file", "uploaded_by_name", "uploaded_at", "notes",
            "row_count", "commodity_total", "premium_total", "ecom_total", "grand_total", "is_latest",
        ]

    def get_uploaded_by_name(self, obj):
        user = obj.uploaded_by
        return (getattr(user, "full_name", "") or getattr(user, "email", "")) if user else obj.uploaded_by_label

    def get_is_latest(self, obj):
        latest = self.context.get("latest_versions")
        if latest is not None:
            return latest.get(obj.month) == obj.version
        return not MonthlyPlanUpload.objects.filter(
            company_id=obj.company_id, month=obj.month, version__gt=obj.version,
        ).exists()


class MonthlyPlanRowSerializer(serializers.ModelSerializer):
    class Meta:
        model = MonthlyPlanRow
        exclude = ["upload"]


def _uploads(company):
    return MonthlyPlanUpload.objects.filter(company=company).select_related("uploaded_by")


def _latest_versions(company) -> dict:
    latest = {}
    for month, version in _uploads(company).values_list("month", "version"):
        latest[month] = max(version, latest.get(month, 0))
    return latest


def _detail(upload, company):
    data = MonthlyPlanUploadSerializer(upload, context={"latest_versions": _latest_versions(company)}).data
    data["rows"] = MonthlyPlanRowSerializer(upload.rows.all(), many=True).data
    return data


class MonthlyPlanListAPI(PlanningBaseView):
    """GET  — every uploaded version, newest month first.
    POST — a new workbook (multipart ``file``; optional ``month`` YYYY-MM-DD to
    override the month the sheet names, and ``notes``). It becomes the next
    version of its month."""

    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        right = CanUploadMonthlyPlan if self.request.method == "POST" else CanViewMonthlyPlan
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def get(self, request):
        company = request.company.company
        uploads = _uploads(company)
        context = {"latest_versions": _latest_versions(company)}
        return Response({"uploads": MonthlyPlanUploadSerializer(uploads, many=True, context=context).data})

    def post(self, request):
        company = request.company.company
        upload_file = request.FILES.get("file")
        if not upload_file:
            return Response({"detail": "No file was uploaded (expected a 'file' field)."}, status=400)
        if not upload_file.name.lower().endswith((".xlsx", ".xlsm")):
            return Response({"detail": "Upload the plan as an .xlsx workbook."}, status=400)
        if upload_file.size > MAX_UPLOAD_BYTES:
            return Response({"detail": "That file is larger than the 10 MB limit."}, status=400)
        try:
            month, title, rows, warnings, mismatches = parse_planning_workbook(upload_file)
        except PlanningParseError as exc:
            return Response({"detail": str(exc), "code": "plan_unreadable"}, status=400)
        except Exception as exc:  # a corrupt or password-protected workbook
            return Response({"detail": f"Could not read that workbook: {exc}", "code": "plan_unreadable"},
                            status=400)
        override = request.data.get("month")
        if override:
            try:
                month = datetime.strptime(str(override)[:10], "%Y-%m-%d").date().replace(day=1)
            except ValueError:
                return Response({"detail": "month must be YYYY-MM-DD."}, status=400)
        if month is None:
            return Response({"detail": "The sheet does not say which month it is for. Pick the month and "
                                       "upload it again.", "code": "month_unknown"}, status=400)

        with transaction.atomic():
            previous = (
                MonthlyPlanUpload.objects.select_for_update()
                .filter(company=company, month=month).order_by("-version").first()
            )
            upload = MonthlyPlanUpload.objects.create(
                company=company, month=month, version=(previous.version + 1) if previous else 1,
                title=title, source_file=upload_file.name, uploaded_by=request.user,
                notes=str(request.data.get("notes", "") or "")[:2000],
            )
            MonthlyPlanRow.objects.bulk_create([MonthlyPlanRow(upload=upload, **fields) for fields in rows],
                                               batch_size=500)
            upload.recalculate_totals().save()
        return Response(
            {"upload": _detail(upload, company), "warnings": warnings, "mismatches": mismatches,
             "replaced_version": previous.version if previous else None},
            status=status.HTTP_201_CREATED,
        )


class MonthlyPlanDetailAPI(PlanningBaseView):
    """GET one version with its rows (``latest`` for the newest version of the
    newest month); DELETE removes that version."""

    def get_permissions(self):
        right = CanRemoveMonthlyPlan if self.request.method == "DELETE" else CanViewMonthlyPlan
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def _upload(self, request, pk):
        uploads = _uploads(request.company.company)
        return uploads.first() if pk == "latest" else uploads.filter(pk=pk).first()

    def get(self, request, pk):
        upload = self._upload(request, pk)
        if upload is None:
            return Response({"detail": "No monthly plan has been uploaded yet." if pk == "latest"
                             else "No such plan."}, status=404)
        return Response(_detail(upload, request.company.company))

    def delete(self, request, pk):
        upload = self._upload(request, pk)
        if upload is None or pk == "latest":
            return Response({"detail": "No such plan."}, status=404)
        label = str(upload)
        upload.delete()
        return Response({"deleted": label})
