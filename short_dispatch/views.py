import logging

from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies

from .permissions import CanCreateShortDispatch, CanViewShortDispatch
from .serializers import (
    ShortDispatchCreateSerializer,
    ShortDispatchDetailSerializer,
    ShortDispatchListSerializer,
    WarehouseOptionSerializer,
)
from .services import ShortDispatchService

logger = logging.getLogger(__name__)


def _service(request):
    return ShortDispatchService(company=request.company.company)


def _allowed_ids(request):
    return user_company_ids(request)


def _bad_request(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ShortDispatchListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateShortDispatch()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewShortDispatch()]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company_id]
        entries = _service(request).list_entries(
            company_ids,
            search=request.GET.get("search") or None,
            from_date=parse_date(request.GET.get("from_date") or ""),
            to_date=parse_date(request.GET.get("to_date") or ""),
        )
        return Response(ShortDispatchListSerializer(entries, many=True).data)

    def post(self, request):
        """The single form. A 201 means SAP already holds the Return Note."""
        serializer = ShortDispatchCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            entry = _service(request).create_and_post(serializer.validated_data, request.user)
        except ValueError as exc:
            return _bad_request(exc)
        return Response(
            ShortDispatchDetailSerializer(entry).data, status=status.HTTP_201_CREATED
        )


class ShortDispatchDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShortDispatch]

    def get(self, request, pk):
        try:
            entry = _service(request).get_entry(pk, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(ShortDispatchDetailSerializer(entry).data)


class ShortDispatchInvoiceLookupAPI(APIView):
    """The bill the form is built from.

    Behind the create permission rather than the view one: it is a live SAP read
    done to *start* a short dispatch, and it exposes what each line was billed at.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateShortDispatch]

    def get(self, request):
        try:
            payload = _service(request).lookup_invoice(request.GET.get("invoice_number") or "")
        except ValueError as exc:
            return _bad_request(exc)
        return Response(payload)


class ShortDispatchWarehousesAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateShortDispatch]

    def get(self, request):
        try:
            warehouses = _service(request).list_warehouses()
        except ValueError as exc:
            return _bad_request(exc)
        except Exception as exc:
            logger.warning("Could not read warehouses for short dispatch: %s", exc)
            return Response(
                {"detail": "Could not read the warehouse list from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(WarehouseOptionSerializer(warehouses, many=True).data)


class ShortDispatchPrintAPI(APIView):
    """SAP's own Return layout for the posted document. A read, so viewing is
    enough -- printing what is already in SAP is not a second chance to post."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewShortDispatch]

    def get(self, request, pk):
        try:
            payload = _service(request).print_payload(pk, _allowed_ids(request))
        except ValueError as exc:
            return _bad_request(exc)
        return Response(payload)
