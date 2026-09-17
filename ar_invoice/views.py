"""APIViews for creating SAP A/R invoices and tracking their approval drafts.

Mirrors ``ap_invoice.views``: per-method permissions plus a centralized
``handle_exception`` mapping the SAP domain errors (and the service's
``ValueError`` validations) to HTTP statuses.
"""
import json
import logging

from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import permissions as ar_perms
from .serializers import (
    ARInvoiceCreateSerializer,
    ARInvoicePaymentSerializer,
    ARInvoicePaymentWriteSerializer,
    ARInvoicePostingSerializer,
    CustomerCreditQuerySerializer,
    CustomerSearchQuerySerializer,
    LineDefaultsQuerySerializer,
    OpenSOLinesQuerySerializer,
    SapCashSaleQuerySerializer,
    WarehouseItemsQuerySerializer,
)
from .services import ARInvoiceService

logger = logging.getLogger(__name__)


class ARInvoiceBaseView(APIView):
    read_perms = [ar_perms.CanViewARInvoice]
    write_perms = [ar_perms.CanCreateARInvoice]

    def get_permissions(self):
        extra = self.read_perms if self.request.method in SAFE_METHODS else self.write_perms
        return [IsAuthenticated(), HasCompanyContext()] + [p() for p in extra]

    def handle_exception(self, exc):
        if isinstance(exc, (ValueError, SAPValidationError)):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP connection error: %s", exc)
            return Response(
                {"detail": "SAP is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error: %s", exc)
            return Response(
                {"detail": f"SAP error: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return super().handle_exception(exc)

    def service(self) -> ARInvoiceService:
        return ARInvoiceService(company_code=self.request.company.company.code)

    def posting_response(self, posting, http_status=status.HTTP_200_OK):
        return Response(
            ARInvoicePostingSerializer(posting, context={"request": self.request}).data,
            status=http_status,
        )


class CustomerSearchView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/customers/?search=jivo — type-ahead picker feed."""

    def get(self, request):
        query = CustomerSearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        client = SAPClient(company_code=request.company.company.code)
        return Response(
            client.search_customers(search=query.validated_data.get("search") or None)
        )


class CustomerCreditView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/customer-credit/?customer_code=CUSTA000123

    The customer's credit limit and what is already drawn against it, so the
    operator sees the position while raising the invoice instead of learning it
    from SAP's refusal. Read-only and non-blocking: SAP still runs its own check
    at posting, and a customer with no limit set is a normal customer.
    """

    def get(self, request):
        query = CustomerCreditQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        client = SAPClient(company_code=request.company.company.code)
        credit = client.customer_credit_status(query.validated_data["customer_code"])
        if credit is None:
            return Response(
                {"detail": "No such customer in SAP for this company."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(credit)


class OpenSOLinesView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/open-so-lines/?customer_code=CUSTA000123&search="""

    def get(self, request):
        query = OpenSOLinesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        lines = self.service().open_so_lines(
            customer_code=query.validated_data["customer_code"],
            search=query.validated_data.get("search") or None,
        )
        return Response(lines)


class WarehouseItemsView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/items/?warehouse=GP-FG&search=mustard — the
    item picker for a direct (cash) sale, with live on-hand/available."""

    def get(self, request):
        query = WarehouseItemsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        client = SAPClient(company_code=request.company.company.code)
        return Response(
            client.get_warehouse_stock(
                query.validated_data["warehouse"],
                search=query.validated_data.get("search") or "",
            )
        )


class LineDefaultsView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/line-defaults/?customer_code=&item_code= — the
    price and tax code the customer last paid for the item, to prefill a line."""

    def get(self, request):
        query = LineDefaultsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        client = SAPClient(company_code=request.company.company.code)
        defaults = client.ar_last_sale_defaults(
            query.validated_data["customer_code"],
            [query.validated_data["item_code"]],
        )
        return Response(defaults.get(query.validated_data["item_code"], {}))


class ARInvoiceListCreateView(ARInvoiceBaseView):
    """GET (history) / POST (create + post to SAP) /api/v1/ar-invoices/invoices/."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        postings = self.service().get_history()
        return Response(
            ARInvoicePostingSerializer(
                postings, many=True, context={"request": request}
            ).data
        )

    def post(self, request):
        if request.content_type and "multipart" in request.content_type:
            raw = request.data.get("data", "{}")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid JSON in 'data' field"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed = request.data
            attachments = []

        serializer = ARInvoiceCreateSerializer(data=parsed)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        common = {
            "user": request.user,
            "customer_code": data["customer_code"],
            "customer_ref": data.get("customer_ref", ""),
            "attachments": attachments,
            "doc_date": data.get("doc_date"),
            "doc_due_date": data.get("doc_due_date"),
            "tax_date": data.get("tax_date"),
            "comments": data.get("comments", ""),
        }
        if data.get("direct_lines"):
            posting = self.service().create_direct_invoice(
                direct_lines=data["direct_lines"], **common
            )
        else:
            posting = self.service().create_invoice(line_keys=data["lines"], **common)
        return self.posting_response(posting, http_status=status.HTTP_201_CREATED)


class ARCashSaleHistoryView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/sap-invoices/?date_from=&date_to=&search=

    The cash sales as SAP holds them — including the ones the counter raised in
    SAP directly, which this app's own History cannot know about. A read of a
    posted document, so the view permission is enough.
    """

    def get(self, request):
        query = SapCashSaleQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        return Response(
            self.service().sap_cash_sale_history(
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
                search=data.get("search") or None,
                limit=data.get("limit") or 500,
            )
        )


class ARCashSalePrintView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/sap-invoices/<doc_entry>/print/ — the TAX INVOICE
    for a cash sale off SAP's own book.

    The sibling endpoint prints by this app's record id, which only the bills we
    raised have. The counter's bills are SAP's alone, so this one is keyed by
    SAP's DocEntry — the id the cash-sale list already carries for every row.
    """

    def get(self, request, doc_entry):
        try:
            return Response(self.service().sap_print_payload(doc_entry))
        except ValueError as e:
            # No such invoice, not a cash sale, or cancelled — all "no sheet to
            # print"; the message tells the operator which.
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)


class ARInvoiceDetailView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/invoices/<pk>/"""

    def get(self, request, pk):
        try:
            posting = self.service().get_posting(pk)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return self.posting_response(posting)


class ARInvoicePostView(ARInvoiceBaseView):
    """POST /api/v1/ar-invoices/invoices/<pk>/post/ — retry a PENDING/FAILED post."""

    def post(self, request, pk):
        posting = self.service().post_to_sap(pk, request.user)
        return self.posting_response(posting)


class ARInvoiceRefreshView(ARInvoiceBaseView):
    """POST /api/v1/ar-invoices/invoices/<pk>/refresh/ — re-read draft/approval state."""

    def post(self, request, pk):
        posting = self.service().refresh_from_sap(pk, request.user)
        return self.posting_response(posting)


class ARInvoiceCancelView(ARInvoiceBaseView):
    """POST /api/v1/ar-invoices/invoices/<pk>/cancel/ — abandon a PENDING/FAILED
    record and release its Sales Order lines."""

    def post(self, request, pk):
        posting = self.service().cancel(pk, request.user)
        return self.posting_response(posting)


class ARInvoicePostDraftView(ARInvoiceBaseView):
    """POST /api/v1/ar-invoices/invoices/<pk>/post-draft/ — allocate batches and
    add the approved draft as the real invoice."""

    def post(self, request, pk):
        posting = self.service().post_approved_draft(pk, request.user)
        return self.posting_response(posting)


class ARInvoicePaymentView(ARInvoiceBaseView):
    """PUT / DELETE /api/v1/ar-invoices/payments/<doc_entry>/

    Whether an invoice has actually been paid, as this app records it. Keyed by
    SAP's ``DocEntry`` so one mark covers the bill in both History books — and
    so the counter's own SAP-raised bills, which have no record here, can be
    tracked at all.

    Marking is its own permission: the cashier who takes the money is rarely the
    person allowed to raise invoices.
    """

    write_perms = [ar_perms.CanMarkARInvoicePayment]

    def put(self, request, doc_entry):
        serializer = ARInvoicePaymentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = self.service().set_payment(
            doc_entry, request.user, **serializer.validated_data
        )
        return Response(ARInvoicePaymentSerializer(payment).data)

    def delete(self, request, doc_entry):
        """Drop the mark — for one made against the wrong bill. Untracked is
        not the same as unpaid, which is what a PENDING mark says."""
        self.service().clear_payment(doc_entry)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ARInvoicePrintView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/invoices/<pk>/print/ — SAP's TAX INVOICE, as data.

    A read, so it needs only the view permission: printing a bill the warehouse
    already raised is not a second chance to post one.
    """

    def get(self, request, pk):
        try:
            return Response(self.service().print_payload(pk))
        except ValueError as e:
            # "not found" and "not posted yet" both mean there is no sheet to
            # print; the message distinguishes them for the operator.
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
