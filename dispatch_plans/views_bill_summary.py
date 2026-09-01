"""API for the bill summary — the picking sheet handed to the warehouse floor.

SAP failures are reported as 502 rather than 400, so the frontend can tell "SAP
said no" from "you asked for something impossible". That distinction matters more
here than usual: a refused SAP stamp does not undo a pick, and the screen has to
say so rather than implying the whole action failed.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from .bill_summary_service import BillSummaryError, BillSummaryService
from .models_bill_summary import BillSummary
from .permissions import (
    CanCancelBillSummary,
    CanCreateBillSummary,
    CanPickBillSummary,
    CanViewBillSummary,
)
from .serializers_bill_summary import (
    BillSummaryCancelSerializer,
    BillSummaryDetailSerializer,
    BillSummaryGenerateSerializer,
    BillSummaryListSerializer,
)

logger = logging.getLogger(__name__)


def _service(request) -> BillSummaryService:
    return BillSummaryService(request.company.company.code, request.user)


def _bad(exc) -> Response:
    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _sap_down(exc) -> Response:
    logger.error("SAP error in the bill summary flow: %s", exc)
    return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class BillSummaryLookupAPI(APIView):
    """Search a bill and get the form filled in as far as the app can manage.

    Returns the bill's lines, a `prefill` block from the dispatch plan, and
    `missing` naming what the user still has to supply. Naming the gaps up front
    is the point: the user should see "the bilty is missing" here rather than
    discover it when SAP refuses the posting.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request):
        bill_number = request.query_params.get("bill_number") or ""
        try:
            return Response(_service(request).lookup(bill_number))
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)


class BillSummaryListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request):
        rows = (
            BillSummary.objects.filter(
                company__code=request.company.company.code, is_active=True
            )
            .select_related("company", "issued_by", "picked_by")
            .prefetch_related("lines")
        )
        for field in ("status", "sap_status", "sap_invoice_doc_num"):
            value = request.query_params.get(field)
            if value:
                rows = rows.filter(**{field: value})
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            rows = rows.filter(dispatch_date__gte=date_from)
        if date_to:
            rows = rows.filter(dispatch_date__lte=date_to)
        return Response(BillSummaryListSerializer(rows, many=True).data)

    def post(self, request):
        if not CanCreateBillSummary().has_permission(request, self):
            return Response(
                {"detail": "You cannot issue a bill summary."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = BillSummaryGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            summary = _service(request).generate(serializer.validated_data)
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)
        return Response(
            BillSummaryDetailSerializer(summary).data, status=status.HTTP_201_CREATED
        )


class BillSummaryDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request, pk):
        summary = (
            BillSummary.objects.filter(
                pk=pk, company__code=request.company.company.code, is_active=True
            )
            .select_related("company", "issued_by", "picked_by")
            .first()
        )
        if summary is None:
            return Response(
                {"detail": "Bill summary not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryPickAPI(APIView):
    """The floor has fetched the goods — who and when, nothing more."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPickBillSummary]

    def post(self, request, pk):
        try:
            summary = _service(request).mark_picked(pk)
        except BillSummaryError as exc:
            return _bad(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryStampAPI(APIView):
    """Retry the SAP posting for a sheet whose posting failed."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBillSummary]

    def post(self, request, pk):
        try:
            summary = _service(request).post_to_sap(pk)
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryCancelAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCancelBillSummary]

    def post(self, request, pk):
        serializer = BillSummaryCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            summary = _service(request).cancel(pk, serializer.validated_data["reason"])
        except BillSummaryError as exc:
            return _bad(exc)
        return Response(BillSummaryDetailSerializer(summary).data)
