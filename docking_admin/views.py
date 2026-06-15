from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.views_sales_dispatch import get_sales_dispatch_or_404

from .models import DockingScanSkipRequest, DockingScanSkipStatus
from .serializers import (
    DockingScanSkipRequestCreateSerializer,
    DockingScanSkipRequestSerializer,
    DockingScanSkipReviewSerializer,
)
from .services import notify_approvers_of_new_request, notify_requester_of_review

PERM_REQUEST = "docking_admin.can_request_docking_scan_skip"
PERM_VIEW = "docking_admin.can_view_docking_scan_skip"
PERM_APPROVE = "docking_admin.can_approve_docking_scan_skip"

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
        company = request.company.company
        serializer = DockingScanSkipRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entry = get_sales_dispatch_or_404(company, serializer.validated_data["sales_dispatch"])

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

        company = request.company.company
        # Validate the docking entry belongs to the company.
        get_sales_dispatch_or_404(company, entry_id)

        skip_request = (
            scan_skip_queryset(company).filter(sales_dispatch_id=entry_id).first()
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
