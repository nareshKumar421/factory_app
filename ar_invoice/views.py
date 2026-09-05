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
    ARInvoicePostingSerializer,
    CustomerSearchQuerySerializer,
    LineDefaultsQuerySerializer,
    OpenSOLinesQuerySerializer,
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
