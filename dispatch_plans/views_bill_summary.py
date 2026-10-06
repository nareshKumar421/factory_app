"""API for the bill summary — raised by dispatch, dated by the warehouse.

SAP failures are reported as 502 rather than 400, so the frontend can tell "SAP
said no" from "you asked for something impossible". That distinction matters more
here than usual: a refused SAP stamp does not undo an approval or a pick, and the
screen has to say so rather than implying the whole action failed.

The batch endpoints — submitting a truck, approving a truck — answer 200 with
what went through AND what did not, rather than failing the request over one bad
bill. Eight bills are eight invoices; the one SAP would not take is worth naming,
not worth making somebody re-do the other seven for.
"""

import logging
from datetime import date

from django.db.models import Q
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
    CanApproveBillSummary,
    CanCancelBillSummary,
    CanCreateBillSummary,
    CanPickBillSummary,
    CanPrintInvoice,
    CanReconcileBillSummaryWithSap,
    CanViewBillSummary,
)
from .serializers_bill_summary import (
    BillSummaryApproveSerializer,
    BillSummaryBulkSubmitSerializer,
    BillSummaryCancelSerializer,
    BillSummaryDetailSerializer,
    BillSummaryGenerateSerializer,
    BillSummaryListSerializer,
    BillSummaryRejectSerializer,
    BillSummaryResubmitSerializer,
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
            .select_related(
                "company", "issued_by", "picked_by",
                "approved_by", "rejected_by", "printed_by",
            )
            .prefetch_related("lines")
        )
        for field in ("status", "sap_status", "sap_invoice_doc_num"):
            value = request.query_params.get(field)
            if value:
                rows = rows.filter(**{field: value})
        # A sheet still with the warehouse has no dispatch date, so a window on
        # the dispatch date alone would hide exactly the sheets somebody opening
        # this screen is looking for. Those fall back to when they were sent.
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            rows = rows.filter(
                Q(dispatch_date__gte=date_from)
                | Q(dispatch_date__isnull=True, submitted_at__date__gte=date_from)
            )
        if date_to:
            rows = rows.filter(
                Q(dispatch_date__lte=date_to)
                | Q(dispatch_date__isnull=True, submitted_at__date__lte=date_to)
            )
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


class BillSummaryBulkSubmitAPI(APIView):
    """Raise a sheet for each of a truck's bills and send the lot to the warehouse.

    What the popup behind vehicle linking posts. With `dry_run` it answers "how
    many would this be?" without writing anything, which is the question the
    popup has to answer before it can ask its own.

    One company per call: each bill's plan lives in its own company, and the
    linking screen already links company by company for the same reason.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBillSummary]

    def post(self, request):
        serializer = BillSummaryBulkSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = _service(request).submit_bills(
                data["doc_entries"], dry_run=data["dry_run"]
            )
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)
        return Response(
            {
                "dry_run": result["dry_run"],
                "eligible": result["eligible"],
                "skipped": result["skipped"],
                "created": BillSummaryListSerializer(
                    result["created"], many=True
                ).data,
            },
            status=status.HTTP_200_OK if result["dry_run"] else status.HTTP_201_CREATED,
        )


class BillSummaryApproveAPI(APIView):
    """The warehouse's decision: one dispatch date over one or many sheets.

    This is the call that writes to SAP. A sheet SAP then refuses stays approved
    with the refusal recorded on it — the warehouse's decision stands, and the
    posting is retried from the sheet.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveBillSummary]

    def post(self, request):
        serializer = BillSummaryApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            approved, refused = _service(request).approve(
                data["ids"], data["dispatch_date"]
            )
        except BillSummaryError as exc:
            return _bad(exc)
        return Response({
            "approved": BillSummaryListSerializer(approved, many=True).data,
            "refused": refused,
        })


class BillSummaryRejectAPI(APIView):
    """Send a sheet back to the dispatch desk with what is wrong with it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveBillSummary]

    def post(self, request, pk):
        serializer = BillSummaryRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            summary = _service(request).reject(pk, serializer.validated_data["reason"])
        except BillSummaryError as exc:
            return _bad(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryResubmitAPI(APIView):
    """Correct a sheet the warehouse has not approved and send it again."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBillSummary]

    def post(self, request, pk):
        serializer = BillSummaryResubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            summary = _service(request).resubmit(pk, serializer.validated_data)
        except BillSummaryError as exc:
            return _bad(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryPrintedAPI(APIView):
    """The approved sheet has been printed for signing and walking downstairs."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBillSummary]

    def post(self, request, pk):
        try:
            summary = _service(request).mark_printed(pk)
        except BillSummaryError as exc:
            return _bad(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummarySapListAPI(APIView):
    """Dispatches stamped onto the invoice in SAP, with no app sheet behind them.

    Its own endpoint rather than a flag on the list above: this one reads SAP and
    the other reads Postgres, so folding them together would make every visit to
    the screen wait on HANA for rows most users are not asking for.

    The window defaults to the current month. These are read by dispatch date
    out of a table holding years of invoices, and an unbounded sweep of it is a
    minute of the shared SAP box for a screen nobody scrolls that far down.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request):
        today = date.today()
        params = request.query_params
        filters = {
            "date_from": params.get("date_from") or today.replace(day=1).isoformat(),
            "date_to": params.get("date_to") or today.isoformat(),
            "doc_num": params.get("sap_invoice_doc_num") or "",
        }
        try:
            return Response(_service(request).list_sap_summaries(filters))
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)


class BillSummarySapDetailAPI(APIView):
    """One stamped bill, in the same shape the app's own sheets are returned in."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request, doc_entry):
        try:
            return Response(_service(request).get_sap_summary(doc_entry))
        except BillSummaryError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND
            )
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)


class BillSummarySapAdoptAPI(APIView):
    """Take a stamped bill onto the app's books so it can be acted on.

    Allowed to anyone who could have issued or cancelled the sheet themselves:
    adopting writes a record of a dispatch SAP already holds, and refusing it to
    someone who may cancel would leave them looking at a button that cannot work.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def post(self, request, doc_entry):
        if not (
            CanCreateBillSummary().has_permission(request, self)
            or CanCancelBillSummary().has_permission(request, self)
        ):
            return Response(
                {"detail": "You cannot take over a bill summary."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            summary = _service(request).adopt_sap_summary(doc_entry)
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBillSummary]

    def get(self, request, pk):
        summary = (
            BillSummary.objects.filter(
                pk=pk, company__code=request.company.company.code, is_active=True
            )
            .select_related(
                "company", "issued_by", "picked_by",
                "approved_by", "rejected_by", "printed_by",
            )
            .first()
        )
        if summary is None:
            return Response(
                {"detail": "Bill summary not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(BillSummaryDetailSerializer(summary).data)


class BillSummaryInvoicePrintAPI(APIView):
    """The BILL, not the summary — SAP's own TAX INVOICE, as data.

    Keyed by the invoice's `DocEntry` rather than by a sheet id so that the one
    endpoint serves both kinds of row the screen opens: a sheet this app issued,
    and a dispatch stamped straight onto the invoice in SAP, which has no record
    here to key off.

    Read only when somebody asks for it: every print is a HANA read, and most
    people open a sheet to check it rather than to reprint the bill.

    Open to the dispatch planners as well as the bill-summary desk: the Plan page
    hands them the bill for a row on their own board, and they have no reason to
    hold a picking-sheet permission for it.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPrintInvoice]

    def get(self, request, doc_entry):
        try:
            return Response(_service(request).invoice_print_payload(doc_entry))
        except BillSummaryError as exc:
            return _bad(exc)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_down(exc)


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
    """Retry the SAP posting for a sheet whose posting failed.

    Open to the warehouse as well as dispatch: approving is what sent the
    posting, so the desk that approved is often the one looking at the refusal.
    """

    permission_classes = [
        IsAuthenticated, HasCompanyContext, CanReconcileBillSummaryWithSap,
    ]

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
