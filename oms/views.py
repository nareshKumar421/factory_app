"""APIViews for the OMS invoice-approval proxy.

Thin proxy over :class:`oms.client.OmsClient`: the browser talks only to this
backend (existing JWT + ``Company-Code`` auth, gated by ``oms.*`` permissions) and
this backend talks to OMS. On a successful approve/reject we also write a local
:class:`InvoiceApprovalAudit` row so JI can see which employee acted.

``OmsBaseView`` mirrors ``marketplace/views.py`` ``MpBaseView`` — per-method
permissions and a centralized ``handle_exception`` that maps the domain errors to
HTTP status codes.
"""
import logging

from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import permissions as oms_perms
from .client import OmsClient
from .exceptions import OMSConnectionError, OMSDataError, OMSValidationError
from .models import InvoiceApprovalAudit
from .serializers import (
    InvoiceApprovalAuditSerializer,
    InvoiceListQuerySerializer,
    InvoiceStatusUpdateSerializer,
)

logger = logging.getLogger(__name__)


class OmsBaseView(APIView):
    read_perms = [oms_perms.CanViewInvoice]
    write_perms = [oms_perms.CanApproveInvoice]

    def get_permissions(self):
        extra = self.read_perms if self.request.method in SAFE_METHODS else self.write_perms
        return [IsAuthenticated(), HasCompanyContext()] + [p() for p in extra]

    def handle_exception(self, exc):
        if isinstance(exc, OMSValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, OMSConnectionError):
            logger.error("OMS connection error: %s", exc)
            return Response(
                {"detail": "OMS is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, OMSDataError):
            logger.error("OMS data error: %s", exc)
            return Response(
                {"detail": "Received an unexpected response from OMS."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not OmsClient.is_enabled():
            raise OMSConnectionError("The OMS invoice-approval module is not enabled.")

    @property
    def company(self):
        # HasCompanyContext attaches request.company as a UserCompany.
        return self.request.company.company

    def approver_name(self):
        """Display name recorded on OMS as the approver (its history author)."""
        user = self.request.user
        return (getattr(user, "full_name", "") or user.get_username() or "").strip()


class OmsInvoiceListView(OmsBaseView):
    """GET /api/v1/oms/invoices/?whs=GP-FG&status=PENDING — invoices for a warehouse."""

    def get(self, request):
        query = InvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = OmsClient().list_invoices(
            warehouse=query.validated_data["whs"],
            status=query.validated_data.get("status"),
        )
        return Response(data)


class OmsInvoiceStatusUpdateView(OmsBaseView):
    """PATCH /api/v1/oms/invoices/<id>/status/ — approve or reject."""

    def patch(self, request, pk):
        serializer = InvoiceStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        decision = data["status"]
        rejection_reason = data.get("rejection_reason", "")

        result = OmsClient().update_status(
            pk, decision, rejection_reason or None, user=self.approver_name()
        )

        # Record who actually acted (OMS only ever sees the shared service identity).
        self._write_audit(request, pk, decision, rejection_reason, data, result)
        return Response(result)

    def _write_audit(self, request, pk, decision, rejection_reason, data, result):
        try:
            InvoiceApprovalAudit.objects.create(
                invoice_log_id=pk,
                so_number=data.get("so_number", "") or "",
                party_name=data.get("party_name", "") or "",
                total_amount=data.get("total_amount"),
                decision=decision,
                rejection_reason=rejection_reason or "",
                oms_message=(result or {}).get("message", "")[:255],
                company=self.company,
                created_by=request.user,
            )
        except Exception:
            # Auditing must never break the approval itself — OMS already recorded it.
            logger.exception("Failed to write OMS approval audit for invoice %s", pk)


class OmsInvoiceHistoryView(OmsBaseView):
    """GET /api/v1/oms/invoices/<id>/history/ — OMS audit trail for one invoice."""

    def get(self, request, pk):
        data = OmsClient().get_history(pk)
        return Response(data)


class OmsPendingCountView(OmsBaseView):
    """GET /api/v1/oms/invoices/pending-count/?whs=GP-FG — count for the sidebar badge."""

    def get(self, request):
        whs = (request.query_params.get("whs") or "").strip()
        if not whs:
            raise OMSValidationError("whs (warehouse) is required")
        client = OmsClient()
        pending = len(client.list_invoices(warehouse=whs, status="PENDING"))
        edited = len(client.list_invoices(warehouse=whs, status="EDITED"))
        return Response({"pending": pending, "edited": edited, "total": pending + edited})


class OmsInvoiceAuditView(OmsBaseView):
    """GET /api/v1/oms/invoices/<id>/audit/ — local (JI-side) approval audit rows."""

    def get(self, request, pk):
        rows = InvoiceApprovalAudit.objects.filter(
            invoice_log_id=pk, company=self.company
        )
        return Response(InvoiceApprovalAuditSerializer(rows, many=True).data)
