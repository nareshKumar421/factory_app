from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.services.sales_dispatch_gatepass import (
    load_scan_status,
    scanned_full_box_count,
)
from gate_core.views_sales_dispatch import get_sales_dispatch_or_404

from .models import (
    DockingPartialScanRequest,
    DockingScanSkipRequest,
    DockingScanSkipStatus,
)
from .serializers import (
    DockingPartialScanRequestCreateSerializer,
    DockingPartialScanRequestSerializer,
    DockingScanSkipRequestCreateSerializer,
    DockingScanSkipRequestSerializer,
    DockingScanSkipReviewSerializer,
)
from .services import (
    notify_approvers_of_new_partial_request,
    notify_approvers_of_new_request,
    notify_requester_of_partial_review,
    notify_requester_of_review,
)

PERM_REQUEST = "docking_admin.can_request_docking_scan_skip"
PERM_VIEW = "docking_admin.can_view_docking_scan_skip"
PERM_APPROVE = "docking_admin.can_approve_docking_scan_skip"

PARTIAL_PERM_REQUEST = "docking_admin.can_request_docking_partial_scan"
PARTIAL_PERM_VIEW = "docking_admin.can_view_docking_partial_scan"
PARTIAL_PERM_APPROVE = "docking_admin.can_approve_docking_partial_scan"

# Docking statuses in which box scanning is closed, so a skip request is moot.
SCAN_CLOSED_STATUSES = {
    "GATEPASS_PRINTED",
    "PRINT_COMMITTED",
    "DISPATCHED",
    "REJECTED",
    "CANCELLED",
}


def scan_skip_queryset(company):
    return DockingScanSkipRequest.objects.filter(company=company).select_related(
        "sales_dispatch", "requested_by", "reviewed_by"
    )


class DockingScanSkipRequestListCreateView(APIView):
    """
    GET  /api/v1/docking-admin/scan-skip-requests/         -> admin queue (filter ?status=&sales_dispatch=)
    POST /api/v1/docking-admin/scan-skip-requests/         -> operator raises a skip request
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": PERM_VIEW,
        "POST": PERM_REQUEST,
    }

    def get(self, request):
        company = request.company.company
        queryset = scan_skip_queryset(company)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())

        sales_dispatch = request.query_params.get("sales_dispatch")
        if sales_dispatch:
            queryset = queryset.filter(sales_dispatch_id=sales_dispatch)

        serializer = DockingScanSkipRequestSerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = DockingScanSkipRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Resolve the docking across the user's companies, then act on its own
        # company (the docking may belong to a company other than the active header).
        entry = get_sales_dispatch_or_404(request, serializer.validated_data["sales_dispatch"])
        company = entry.company

        if entry.status in SCAN_CLOSED_STATUSES:
            return Response(
                {"detail": "Box scanning is already closed for this Docking entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = scan_skip_queryset(company).filter(
            sales_dispatch=entry, status=DockingScanSkipStatus.PENDING
        ).first()
        if existing:
            return Response(
                DockingScanSkipRequestSerializer(existing).data,
                status=status.HTTP_200_OK,
            )

        skip_request = DockingScanSkipRequest.objects.create(
            company=company,
            sales_dispatch=entry,
            reason=serializer.validated_data["reason"],
            requested_by=request.user,
            created_by=request.user,
            updated_by=request.user,
        )
        notify_approvers_of_new_request(skip_request)
        return Response(
            DockingScanSkipRequestSerializer(skip_request).data,
            status=status.HTTP_201_CREATED,
        )


class DockingScanSkipRequestForDispatchView(APIView):
    """
    GET /api/v1/docking-admin/scan-skip-requests/by-sales-dispatch/<entry_id>/
    Returns the latest skip request for a docking entry, or null. Used by the scan page.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        if not any(request.user.has_perm(p) for p in (PERM_REQUEST, PERM_VIEW, PERM_APPROVE)):
            raise PermissionDenied("You do not have access to docking scan skip requests.")

        # Resolve the docking across the user's companies, then read skip requests
        # for its own company (it may differ from the active Company-Code header).
        entry = get_sales_dispatch_or_404(request, entry_id)

        skip_request = (
            scan_skip_queryset(entry.company).filter(sales_dispatch_id=entry_id).first()
        )
        if not skip_request:
            return Response(None)
        return Response(DockingScanSkipRequestSerializer(skip_request).data)


class DockingScanSkipRequestReviewBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": PERM_APPROVE}
    target_status = None

    def post(self, request, pk):
        company = request.company.company
        skip_request = get_object_or_404(scan_skip_queryset(company), pk=pk)

        if not skip_request.is_pending:
            return Response(
                {"detail": f"This request is already {skip_request.get_status_display().lower()}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = DockingScanSkipReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notes = serializer.validated_data.get("notes", "").strip()

        if self.target_status == DockingScanSkipStatus.REJECTED and not notes:
            return Response(
                {"notes": ["A note is required when rejecting a scan skip request."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        skip_request.mark_reviewed(
            status=self.target_status, reviewer=request.user, notes=notes
        )
        notify_requester_of_review(skip_request)
        return Response(DockingScanSkipRequestSerializer(skip_request).data)


class DockingScanSkipRequestApproveView(DockingScanSkipRequestReviewBaseView):
    """POST /api/v1/docking-admin/scan-skip-requests/<pk>/approve/"""

    target_status = DockingScanSkipStatus.APPROVED


class DockingScanSkipRequestRejectView(DockingScanSkipRequestReviewBaseView):
    """POST /api/v1/docking-admin/scan-skip-requests/<pk>/reject/"""

    target_status = DockingScanSkipStatus.REJECTED


# ---------------------------------------------------------------------------
# Partial-dispatch approval: dispatch a docking with only SOME boxes scanned.
# Mirrors the scan-skip flow; the zero-scan case stays with scan-skip.
# ---------------------------------------------------------------------------


def partial_scan_queryset(company):
    return DockingPartialScanRequest.objects.filter(company=company).select_related(
        "sales_dispatch", "requested_by", "reviewed_by"
    ).prefetch_related(
        # Needed by the serializer's resolved expected-box count (mirrors the scan page).
        "sales_dispatch__documents__items",
        "sales_dispatch__items",
    )


class DockingPartialScanRequestListCreateView(APIView):
    """
    GET  /api/v1/docking-admin/partial-scan-requests/    -> admin queue (?status=&sales_dispatch=)
    POST /api/v1/docking-admin/partial-scan-requests/    -> operator raises a partial-dispatch request
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": PARTIAL_PERM_VIEW,
        "POST": PARTIAL_PERM_REQUEST,
    }

    def get(self, request):
        company = request.company.company
        queryset = partial_scan_queryset(company)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())

        sales_dispatch = request.query_params.get("sales_dispatch")
        if sales_dispatch:
            queryset = queryset.filter(sales_dispatch_id=sales_dispatch)

        serializer = DockingPartialScanRequestSerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = DockingPartialScanRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entry = get_sales_dispatch_or_404(request, serializer.validated_data["sales_dispatch"])
        company = entry.company

        if entry.status in SCAN_CLOSED_STATUSES:
            return Response(
                {"detail": "Box scanning is already closed for this Docking entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Judge partial-ness with the SAME rule as the gatepass readiness gate
        # (``load_scan_status``: load-wide box total OR per-bill/line invoiced quantity),
        # so this endpoint can never refuse an approval the gate is demanding — which
        # would deadlock the operator (gate wants approval, this says none is needed).
        scanned, expected, has_scans, is_partial = load_scan_status(entry)
        # Recorded in FULL boxes, the same figure the operator's screen shows against
        # expected_boxes: a part box covers a bill's printed loose remainder, so counting
        # it here would put "116 of 116 boxes" on a request raised because 16 pieces are
        # still unloaded.
        scanned_full_boxes = scanned_full_box_count(entry)
        if not has_scans:
            return Response(
                {"detail": "No boxes are scanned — request a scan skip instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not is_partial:
            return Response(
                {"detail": "All boxes are scanned — no partial-dispatch approval is needed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = partial_scan_queryset(company).filter(
            sales_dispatch=entry, status=DockingScanSkipStatus.PENDING
        ).first()
        if existing:
            return Response(
                DockingPartialScanRequestSerializer(existing).data,
                status=status.HTTP_200_OK,
            )

        partial_request = DockingPartialScanRequest.objects.create(
            company=company,
            sales_dispatch=entry,
            scanned_boxes=scanned_full_boxes,
            expected_boxes=expected,
            reason=serializer.validated_data["reason"],
            requested_by=request.user,
            created_by=request.user,
            updated_by=request.user,
        )
        notify_approvers_of_new_partial_request(partial_request)
        return Response(
            DockingPartialScanRequestSerializer(partial_request).data,
            status=status.HTTP_201_CREATED,
        )


class DockingPartialScanRequestForDispatchView(APIView):
    """
    GET /api/v1/docking-admin/partial-scan-requests/by-sales-dispatch/<entry_id>/
    Latest partial-dispatch request for a docking entry, or null. Used by the scan page.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        if not any(
            request.user.has_perm(p)
            for p in (PARTIAL_PERM_REQUEST, PARTIAL_PERM_VIEW, PARTIAL_PERM_APPROVE)
        ):
            raise PermissionDenied("You do not have access to docking partial dispatch requests.")

        entry = get_sales_dispatch_or_404(request, entry_id)
        partial_request = (
            partial_scan_queryset(entry.company).filter(sales_dispatch_id=entry_id).first()
        )
        if not partial_request:
            return Response(None)
        return Response(DockingPartialScanRequestSerializer(partial_request).data)


class DockingPartialScanRequestReviewBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": PARTIAL_PERM_APPROVE}
    target_status = None

    def post(self, request, pk):
        company = request.company.company
        partial_request = get_object_or_404(partial_scan_queryset(company), pk=pk)

        if not partial_request.is_pending:
            return Response(
                {"detail": f"This request is already {partial_request.get_status_display().lower()}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = DockingScanSkipReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notes = serializer.validated_data.get("notes", "").strip()

        if self.target_status == DockingScanSkipStatus.REJECTED and not notes:
            return Response(
                {"notes": ["A note is required when rejecting a request."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        partial_request.mark_reviewed(
            status=self.target_status, reviewer=request.user, notes=notes
        )
        notify_requester_of_partial_review(partial_request)
        return Response(DockingPartialScanRequestSerializer(partial_request).data)


class DockingPartialScanRequestApproveView(DockingPartialScanRequestReviewBaseView):
    """POST /api/v1/docking-admin/partial-scan-requests/<pk>/approve/"""

    target_status = DockingScanSkipStatus.APPROVED


class DockingPartialScanRequestRejectView(DockingPartialScanRequestReviewBaseView):
    """POST /api/v1/docking-admin/partial-scan-requests/<pk>/reject/"""

    target_status = DockingScanSkipStatus.REJECTED
