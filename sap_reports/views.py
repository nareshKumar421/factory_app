"""
sap_reports/views.py

API for the SAP Reports module.

Every endpoint requires JWT authentication, a ``Company-Code`` header, and the
``can_view_sap_reports`` permission; the catalogue-management endpoints require
``can_manage_sap_reports`` on top. Reports are always scoped to the company in
the header -- a saved query lives in one company database and means nothing
outside it.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .exceptions import SapReportError, SapReportParameterError, SapReportSqlError
from .exports import csv_response, xlsx_response
from .models import SapReport, SapReportAccess, SapReportParameter, SapReportRun
from .permissions import CanManageSapReports, CanViewSapReports
from .serializers import (
    CategorySerializer,
    ExportReportSerializer,
    GrantAccessSerializer,
    LookupOptionSerializer,
    LookupQuerySerializer,
    RunReportSerializer,
    SapReportAccessSerializer,
    SapReportDetailSerializer,
    SapReportListSerializer,
    SapReportRunSerializer,
    SapReportSqlSerializer,
    SyncReportsSerializer,
    UpdateReportSerializer,
)
from .services import access as report_access
from .services.catalog import SapReportCatalogService, is_internal_category
from .services.lookups import SapReportLookupService
from .services.runner import SapReportRunner

logger = logging.getLogger(__name__)

SAP_UNAVAILABLE = "SAP system is currently unavailable. Please try again later."


class SapReportBaseAPI(APIView):
    """Shared company scoping and SAP error handling."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapReports]

    @property
    def company(self):
        return self.request.company.company

    def reports(self):
        """The reports of the company in the header that this user may see.

        Scoped through ``services.access``: a plain viewer gets only the
        reports assigned to them (none, if unassigned); superusers and
        catalogue managers get everything. Every report endpoint resolves its
        report through here, so an unassigned report 404s rather than leaking
        that it exists.
        """
        reports = SapReport.objects.for_company(self.company).prefetch_related("parameters")
        return report_access.accessible_reports(self.request.user, reports)

    def get_report(self, slug: str) -> SapReport:
        return get_object_or_404(self.reports(), slug=slug)

    def can_manage(self) -> bool:
        return self.request.user.has_perm("sap_reports.can_manage_sap_reports")

    def handle_exception(self, exc):
        """
        Turns SAP and report problems into answers a user can act on.

        A broken saved query is an ordinary event here -- the SQL is authored in
        SAP by people outside this app -- so it must not surface as a 500.
        """
        if isinstance(exc, SAPConnectionError):
            return Response({"detail": SAP_UNAVAILABLE}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(exc, SAPDataError):
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SapReportParameterError, SapReportSqlError)):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SapReportError):
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return super().handle_exception(exc)


class SapReportListAPI(SapReportBaseAPI):
    """
    The company's SAP reports.

    GET /api/v1/sap-reports/reports/?search=dispatch&include_hidden=true

    Query parameters:
        search         - (optional) matches the report name or description
        include_hidden - (optional) managers only; also returns switched-off and
                         unrunnable reports so they can be fixed
    """

    def get(self, request):
        reports = self.reports()

        include_hidden = (
            request.query_params.get("include_hidden") in ("1", "true", "True")
            and self.can_manage()
        )
        if not include_hidden:
            reports = reports.runnable()

        search = (request.query_params.get("search") or "").strip()
        if search:
            reports = reports.filter(
                Q(sap_name__icontains=search)
                | Q(display_name__icontains=search)
                | Q(description__icontains=search)
            )

        categories = sorted(
            {report.sap_category_name for report in reports if report.sap_category_name}
        )

        return Response(
            {
                "data": SapReportListSerializer(reports, many=True).data,
                "meta": {
                    "company": self.company.code,
                    "total": reports.count(),
                    "categories": categories,
                    "can_manage": self.can_manage(),
                    # True when the per-user assignment rule applies to this
                    # user, so the frontend can say "nothing assigned to you
                    # yet" instead of "no reports exist".
                    "restricted": not report_access.is_unrestricted(request.user),
                },
            }
        )


class SapReportDetailAPI(SapReportBaseAPI):
    """
    One report and the filters it asks for.

    GET   /api/v1/sap-reports/reports/<slug>/
    PATCH /api/v1/sap-reports/reports/<slug>/   (manage permission)
    """

    def get(self, request, slug):
        report = self.get_report(slug)
        return Response({"data": SapReportDetailSerializer(report).data})

    def patch(self, request, slug):
        if not self.can_manage():
            return Response(
                {"detail": "You do not have permission to edit SAP reports."},
                status=status.HTTP_403_FORBIDDEN,
            )

        report = self.get_report(slug)
        serializer = UpdateReportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid report settings.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        changes = serializer.validated_data
        parameter_changes = changes.pop("parameters", [])

        with transaction.atomic():
            for field, value in changes.items():
                setattr(report, field, value)
            report.save()

            for change in parameter_changes:
                self._apply_parameter_change(report, change)

        report.refresh_from_db()
        return Response({"data": SapReportDetailSerializer(report).data})

    @staticmethod
    def _apply_parameter_change(report: SapReport, change: dict) -> None:
        """
        Applies one correction and marks the parameter as human-owned.

        The mark is the whole point: a later sync re-reads the SQL and would
        otherwise put its own guess back over the correction.
        """
        position = change.pop("position")
        parameter = SapReportParameter.objects.filter(report=report, position=position).first()
        if parameter is None:
            return

        for field, value in change.items():
            setattr(parameter, field, value)
        parameter.is_customised = True
        parameter.save()


class SapReportSqlAPI(SapReportBaseAPI):
    """
    The saved query's SQL, for an admin diagnosing a report.

    GET /api/v1/sap-reports/reports/<slug>/sql/
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewSapReports,
        CanManageSapReports,
    ]

    def get(self, request, slug):
        report = self.get_report(slug)
        return Response({"data": SapReportSqlSerializer(report).data})


class RunSapReportAPI(SapReportBaseAPI):
    """
    Runs a report and returns its result set.

    POST /api/v1/sap-reports/reports/<slug>/run/
    Body: {"parameters": {"0": "2026-08-01", "1": "2026-08-22"}, "row_limit": 5000}

    The response is ``{columns, rows, meta}``: ``columns`` describes each column
    (key, label, type), ``rows`` are plain value arrays in that order, and
    ``meta`` carries the row count, whether the row ceiling cut the result short,
    how long SAP took, and the filters that were used.
    """

    def post(self, request, slug):
        report = self.get_report(slug)

        serializer = RunReportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid run request.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        runner = SapReportRunner(company=self.company, user=request.user)
        result = runner.run(
            report,
            serializer.validated_data.get("parameters") or {},
            row_limit=serializer.validated_data.get("row_limit"),
        )
        return Response(result)


class ExportSapReportAPI(SapReportBaseAPI):
    """
    Runs a report and returns it as a file.

    POST /api/v1/sap-reports/reports/<slug>/export/
    Body: {"parameters": {...}, "export_format": "xlsx"}
    """

    def post(self, request, slug):
        report = self.get_report(slug)

        serializer = ExportReportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid export request.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        export_format = serializer.validated_data["export_format"]
        runner = SapReportRunner(company=self.company, user=request.user)
        result = runner.run(
            report,
            serializer.validated_data.get("parameters") or {},
            row_limit=serializer.validated_data.get("row_limit"),
            export_format=export_format,
        )

        if export_format == "csv":
            return csv_response(report.title, result["columns"], result["rows"])
        return xlsx_response(report.title, result["columns"], result["rows"], result["meta"])


class SapReportParameterOptionsAPI(SapReportBaseAPI):
    """
    Options for one of a report's filters.

    GET /api/v1/sap-reports/reports/<slug>/parameters/<position>/options/?search=BH

    Returns ``[]`` for a free-text or numeric prompt, which the frontend renders
    as a plain input.
    """

    def get(self, request, slug, position):
        report = self.get_report(slug)
        parameter = get_object_or_404(report.parameters, position=position)

        serializer = LookupQuerySerializer(
            data={
                "kind": parameter.kind,
                "search": request.query_params.get("search", ""),
            }
        )
        serializer.is_valid(raise_exception=True)

        options = SapReportLookupService(self.company).options_for(
            parameter.kind,
            serializer.validated_data["search"],
        )
        return Response(
            {
                "data": LookupOptionSerializer(options, many=True).data,
                "meta": {"kind": parameter.kind, "label": parameter.label},
            }
        )


class SapReportRunHistoryAPI(SapReportBaseAPI):
    """
    Recent runs, for one report or for the whole company.

    GET /api/v1/sap-reports/reports/<slug>/runs/
    GET /api/v1/sap-reports/runs/                 (manage permission)
    """

    PAGE_SIZE = 50

    def get(self, request, slug=None):
        runs = SapReportRun.objects.filter(company=self.company).select_related(
            "report", "run_by"
        )

        if slug:
            runs = runs.filter(report=self.get_report(slug))
        elif not self.can_manage():
            # One report's history is context for the numbers on screen; the
            # company-wide feed is an audit trail, and that is a manager's view.
            return Response(
                {"detail": "You do not have permission to view all report runs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        total = runs.count()
        return Response(
            {
                "data": SapReportRunSerializer(runs[: self.PAGE_SIZE], many=True).data,
                "meta": {"total": total, "returned": min(total, self.PAGE_SIZE)},
            }
        )


class SapReportCategoriesAPI(SapReportBaseAPI):
    """
    SAP's own query categories, so an admin can see what is available to sync.

    GET /api/v1/sap-reports/categories/
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewSapReports,
        CanManageSapReports,
    ]

    def get(self, request):
        categories = SapReportCatalogService(self.company).list_categories()
        for category in categories:
            # SAP machinery (dashboards, approval templates, ...) that a
            # default sync skips; syncable by name if someone insists.
            category["is_internal"] = is_internal_category(category["category_name"])
        return Response(
            {
                "data": CategorySerializer(categories, many=True).data,
                "meta": {"company": self.company.code},
            }
        )


class SyncSapReportsAPI(SapReportBaseAPI):
    """
    Mirrors SAP's saved queries into the catalogue.

    POST /api/v1/sap-reports/sync/
    Body: {"category": "GST R1", "dry_run": false}

    With no category named, every report category is synced (SAP's internal
    machinery categories are skipped). New queries appear, edited SQL is
    refreshed, and a query deleted in SAP is flagged. Friendly names,
    descriptions and corrected parameter labels are never overwritten.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewSapReports,
        CanManageSapReports,
    ]

    def post(self, request):
        serializer = SyncReportsSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid sync request.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        options = serializer.validated_data
        category = (
            None
            if options["all_categories"]
            else ((options.get("category") or "").strip() or None)
        )

        summary = SapReportCatalogService(self.company).sync(
            category_name=category,
            dry_run=options["dry_run"],
        )
        logger.info("SAP report sync for %s: %s", self.company.code, summary)
        return Response({"data": summary})


class SapReportAccessListAPI(SapReportBaseAPI):
    """
    Who may run which report -- the SAP-report twin of Warehouse Managers.

    GET  /api/v1/sap-reports/access/?user=<id>
    POST /api/v1/sap-reports/access/
    Body: {"user": 7, "report_slugs": ["stock-transfer-report", "exp-date"]}

    Assignments are per company, because the reports are. Deciding who sees
    what is an admin's job, so both verbs need the manage permission --
    letting a viewer widen their own shelf would defeat the point.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewSapReports,
        CanManageSapReports,
    ]

    def get(self, request):
        rows = (
            SapReportAccess.objects.filter(report__company=self.company)
            .select_related("user", "report", "assigned_by")
            .order_by("user__full_name", "report__sap_name")
        )
        user_id = request.query_params.get("user")
        if user_id:
            rows = rows.filter(user_id=user_id)
        if request.query_params.get("active_only") == "true":
            rows = rows.filter(is_active=True)
        return Response({"data": SapReportAccessSerializer(rows, many=True).data})

    def post(self, request):
        serializer = GrantAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        reports = SapReport.objects.for_company(self.company).filter(
            slug__in=data["report_slugs"]
        )
        found = {report.slug: report for report in reports}
        unknown = sorted(set(data["report_slugs"]) - set(found))
        if unknown:
            return Response(
                {"detail": f"No such report in this company: {', '.join(unknown)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created, reactivated, already = [], [], []
        with transaction.atomic():
            for slug in data["report_slugs"]:
                # Reactivate rather than create a second row: the unique
                # constraint would refuse a duplicate, and re-adding a user
                # who was removed should just work.
                row, was_created = SapReportAccess.objects.get_or_create(
                    user_id=data["user"],
                    report=found[slug],
                    defaults={"assigned_by": request.user},
                )
                if was_created:
                    created.append(slug)
                elif not row.is_active:
                    row.is_active = True
                    row.assigned_by = request.user
                    row.save(update_fields=["is_active", "assigned_by", "updated_at"])
                    reactivated.append(slug)
                else:
                    already.append(slug)

        rows = (
            SapReportAccess.objects.filter(
                user_id=data["user"], report__company=self.company, is_active=True
            )
            .select_related("user", "report", "assigned_by")
            .order_by("report__sap_name")
        )
        return Response(
            {
                "created": created,
                "reactivated": reactivated,
                "already_assigned": already,
                "assignments": SapReportAccessSerializer(rows, many=True).data,
            },
            status=status.HTTP_201_CREATED if (created or reactivated) else status.HTTP_200_OK,
        )


class SapReportAccessDetailAPI(SapReportBaseAPI):
    """
    Deactivate (or restore) one assignment.

    PATCH  /api/v1/sap-reports/access/<pk>/   Body: {"is_active": true|false}
    DELETE /api/v1/sap-reports/access/<pk>/

    Deactivates rather than deletes: past runs in the audit trail were made on
    the strength of this row, and the record of who was allowed should survive
    a reassignment.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewSapReports,
        CanManageSapReports,
    ]

    def _get(self, pk):
        return (
            SapReportAccess.objects.filter(pk=pk, report__company=self.company)
            .select_related("user", "report", "assigned_by")
            .first()
        )

    def patch(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = bool(request.data.get("is_active", True))
        row.save(update_fields=["is_active", "updated_at"])
        return Response(SapReportAccessSerializer(row).data)

    def delete(self, request, pk):
        row = self._get(pk)
        if row is None:
            return Response(
                {"detail": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND
            )
        row.is_active = False
        row.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
