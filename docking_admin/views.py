from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.services.sales_dispatch_gatepass import (
    arrival_scan_dockings,
    arrival_scan_status,
    short_bills,
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
    notify_approvers_of_new_partial_requests,
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
        "sales_dispatch", "document", "requested_by", "reviewed_by"
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

        if entry.status in SCAN_CLOSED_STATUSES:
            return Response(
                {"detail": "Box scanning is already closed for this Docking entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Judge partial-ness with the SAME rule as the operator's scan page and the
        # gatepass readiness gate (``arrival_scan_status``: every scan-required docking on
        # the truck, each judged on per-bill/line invoiced quantity or its box total), so
        # this endpoint can never refuse an approval the gate is demanding — which would
        # deadlock the operator (gate wants approval, this says none is needed). Judging
        # only this docking is what deadlocked a truck carrying a fully scanned bill plus a
        # PM-carton bill with no box barcodes: the scan page locked load-wide while this
        # endpoint answered "all boxes are scanned".
        _scanned, _expected, has_scans, is_partial = arrival_scan_status(entry)
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

        # ONE REQUEST PER SHORT BILL. The admin is asked about the goods that are actually
        # missing: a truck carrying a fully scanned Mart bill beside two short Oil ones used
        # to raise a single docking-wide request — filed against whichever docking the
        # operator happened to stand on, which was the complete one. Each request carries
        # its own bill's scanned/expected boxes, and is filed in that bill's own company:
        # on a cross-company truck the Oil bills belong to Oil's approvals queue, whatever
        # company header the operator is working under.
        reason = serializer.validated_data["reason"]
        requests = []
        created_any = False
        for shortfall in short_bills(entry):
            existing = DockingPartialScanRequest.objects.filter(
                sales_dispatch=shortfall.docking,
                document_id=shortfall.document_id,
                status=DockingScanSkipStatus.PENDING,
            ).first()
            if existing:
                requests.append(existing)
                continue
            created_any = True
            requests.append(
                DockingPartialScanRequest.objects.create(
                    company=shortfall.docking.company,
                    sales_dispatch=shortfall.docking,
                    document_id=shortfall.document_id,
                    # Recorded in FULL boxes, the figure the operator's screen shows: a part
                    # box covers the bill's printed loose remainder, so counting it would
                    # put "116 of 116 boxes" on a request raised for 16 unloaded pieces.
                    scanned_boxes=shortfall.scanned_boxes,
                    expected_boxes=shortfall.expected_boxes,
                    scanned_pieces=shortfall.scanned_pieces,
                    expected_pieces=shortfall.expected_pieces,
                    reason=reason,
                    requested_by=request.user,
                    created_by=request.user,
                    updated_by=request.user,
                )
            )
        if not requests:
            return Response(
                {"detail": "All boxes are scanned — no partial-dispatch approval is needed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Only shout when something new was raised; re-submitting an unchanged load just
        # returns the requests already waiting (200), the way the old endpoint did.
        if created_any:
            notify_approvers_of_new_partial_requests(requests)
        return Response(
            DockingPartialScanRequestSerializer(requests, many=True).data,
            status=status.HTTP_201_CREATED if created_any else status.HTTP_200_OK,
        )


class DockingPartialScanRequestForDispatchView(APIView):
    """
    GET /api/v1/docking-admin/partial-scan-requests/by-sales-dispatch/<entry_id>/
    Every partial-dispatch request on this docking's TRUCK, newest first. Used by the
    scan page.

    Truck-wide and cross-company on purpose: the shortfall is judged across the whole load
    and an approval now names one bill, so the operator standing on a fully scanned docking
    must still see the requests raised for its neighbour's bills — otherwise the screen
    shows "no request" while three sit in the admin queue. Never company-filtered for the
    same reason: on a mixed truck the Oil bills' requests belong to Oil.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        if not any(
            request.user.has_perm(p)
            for p in (PARTIAL_PERM_REQUEST, PARTIAL_PERM_VIEW, PARTIAL_PERM_APPROVE)
        ):
            raise PermissionDenied("You do not have access to docking partial dispatch requests.")

        entry = get_sales_dispatch_or_404(request, entry_id)
        docking_ids = [d.pk for d in arrival_scan_dockings(entry)]
        requests = (
            DockingPartialScanRequest.objects.filter(sales_dispatch_id__in=docking_ids)
            .select_related("sales_dispatch", "document", "requested_by", "reviewed_by")
            .prefetch_related("sales_dispatch__documents__items", "sales_dispatch__items")
        )
        return Response(DockingPartialScanRequestSerializer(requests, many=True).data)


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
