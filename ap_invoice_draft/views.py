import logging

from django.http import Http404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import APInvoiceDraft
from .permissions import CanCreateAPInvoiceDraft, CanViewAPInvoiceDraft
from .serializers import (
    APInvoiceDraftCreateSerializer,
    APInvoiceDraftDetailSerializer,
    APInvoiceDraftListSerializer,
    OpenGRPOSerializer,
)
from .services import APInvoiceDraftService

logger = logging.getLogger(__name__)


def _bad_request(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _sap_down(exc):
    return Response({"detail": f"SAP is not answering: {exc}"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _entry(request, pk) -> APInvoiceDraft:
    """The entry, if it belongs to one of the user's companies."""
    entry = (
        APInvoiceDraft.objects.filter(
            pk=pk, company_id__in=user_company_ids(request), is_active=True,
        )
        .select_related("company", "created_by")
        .first()
    )
    if entry is None:
        raise Http404
    return entry


def _detail(request, entry, code=status.HTTP_200_OK):
    entry = _entry(request, entry.pk)  # fresh
    return Response(
        APInvoiceDraftDetailSerializer(entry, context={"request": request}).data, status=code,
    )


class OpenGRPOListAPI(APIView):
    """GRPOs a bill can be entered against: open, material, newest first."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateAPInvoiceDraft]

    def get(self, request):
        service = APInvoiceDraftService(request.company.company)
        try:
            rows = service.open_grpos(request.GET.get("search") or "")
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_down(exc)
        return Response(OpenGRPOSerializer(rows, many=True).data)


class APInvoiceDraftListCreateAPI(APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateAPInvoiceDraft()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewAPInvoiceDraft()]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company_id]
        entries = APInvoiceDraftService(request.company.company).list_entries(
            company_ids, search=request.GET.get("search") or None,
        )
        return Response(APInvoiceDraftListSerializer(entries, many=True).data)

    def post(self, request):
        """The bill and its GRPO. 201 whether or not SAP took the draft: the
        entry says which, and offers the retry."""
        serializer = APInvoiceDraftCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Pick a GRPO and upload the bill.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = APInvoiceDraftService(request.company.company)
        try:
            entry = service.create(
                serializer.validated_data["grpo_doc_entry"],
                serializer.validated_data["invoice_file"],
                request.user,
            )
        except ValueError as exc:
            return _bad_request(exc)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_down(exc)
        return _detail(request, entry, status.HTTP_201_CREATED)


class APInvoiceDraftDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewAPInvoiceDraft]

    def get(self, request, pk):
        return _detail(request, _entry(request, pk))


class APInvoiceDraftSendToSapAPI(APIView):
    """Try the SAP draft again; links the one SAP has if it made it after all."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateAPInvoiceDraft]

    def post(self, request, pk):
        entry = _entry(request, pk)
        APInvoiceDraftService(entry.company).send_to_sap(entry, request.user)
        return _detail(request, entry)
