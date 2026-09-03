"""APIViews for the SAP invoice-approval module.

Head-office billing raises A/R invoices in SAP; the approval procedure holds each
one as a draft with a pending approval request. These views read those requests
straight from SAP (HANA, via :class:`sap_client.client.SAPClient`) and record the
approve/reject decision back through the Service Layer. On a successful decision
we also write a local :class:`InvoiceApprovalAudit` row so JI can see which
employee acted — SAP itself only ever sees the shared Service Layer account.

``ApprovalBaseView`` mirrors ``marketplace/views.py`` ``MpBaseView`` — per-method
permissions and a centralized ``handle_exception`` that maps the SAP domain errors
to HTTP status codes.
"""
import logging

from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from warehouse.services import warehouse_scope

from . import permissions as approval_perms
from .models import InvoiceApprovalAudit
from .serializers import (
    InvoiceApprovalAuditSerializer,
    InvoiceListQuerySerializer,
    InvoiceStatusUpdateSerializer,
)

logger = logging.getLogger(__name__)


class ApprovalBaseView(APIView):
    read_perms = [approval_perms.CanViewInvoice]
    write_perms = [approval_perms.CanApproveInvoice]

    def get_permissions(self):
        extra = self.read_perms if self.request.method in SAFE_METHODS else self.write_perms
        return [IsAuthenticated(), HasCompanyContext()] + [p() for p in extra]

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
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
                {"detail": "Received an unexpected response from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return super().handle_exception(exc)

    @property
    def company(self):
        # HasCompanyContext attaches request.company as a UserCompany.
        return self.request.company.company

    def sap_client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)

    def assert_manages(self, warehouses):
        """Refuse unless the acting user manages every warehouse named.

        Ties the invoice-approval page to the same per-user warehouse scoping the
        stock-movement screens use (``warehouse.UserWarehouse``): a manager only
        sees and acts on invoices shipping from a warehouse they run. Superusers
        are unrestricted; a user assigned no warehouse is blocked (by design).
        """
        warehouse_scope.assert_manages(
            self.request.user,
            self.company.code,
            warehouses,
            action="view or act on invoices for this warehouse",
        )

    def approver_name(self):
        """Display name recorded in the decision remarks and the local audit."""
        user = self.request.user
        return (getattr(user, "full_name", "") or user.get_username() or "").strip()


class InvoiceApprovalListView(ApprovalBaseView):
    """GET /api/v1/invoice-approvals/invoices/?whs=GP-FG&status=PENDING."""

    def get(self, request):
        query = InvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        warehouse = query.validated_data["whs"]
        self.assert_manages([warehouse])
        data = self.sap_client().list_invoice_approvals(
            warehouse=warehouse,
            status=query.validated_data.get("status"),
        )
        return Response(data)


class InvoiceApprovalStatusUpdateView(ApprovalBaseView):
    """PATCH /api/v1/invoice-approvals/invoices/<pk>/status/ — approve or reject.

    ``pk`` is the SAP approval-request code (OWDD.WddCode).
    """

    def patch(self, request, pk):
        serializer = InvoiceStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        decision = data["status"]
        rejection_reason = data.get("rejection_reason", "")

        # Only a manager of the invoice's warehouse may decide it.
        client = self.sap_client()
        self.assert_manages(client.invoice_approval_warehouses(pk))

        # SAP sees the shared SL account; carry the real actor in the remarks.
        if decision == "REJECTED":
            remarks = f"{rejection_reason} — {self.approver_name()} (Factory app)"
        else:
            remarks = f"Approved by {self.approver_name()} (Factory app)"

        result = client.decide_invoice_approval(
            pk, approve=(decision == "APPROVED"), remarks=remarks
        )

        # Record who actually acted.
        self._write_audit(request, pk, decision, rejection_reason, data, result)
        return Response(result)

    def _write_audit(self, request, pk, decision, rejection_reason, data, result):
        try:
            InvoiceApprovalAudit.objects.create(
                approval_code=pk,
                so_number=data.get("so_number", "") or "",
                party_name=data.get("party_name", "") or "",
                total_amount=data.get("total_amount"),
                decision=decision,
                rejection_reason=rejection_reason or "",
                sap_message=(result or {}).get("message", "")[:255],
                company=self.company,
                created_by=request.user,
            )
        except Exception:
            # Auditing must never break the decision itself — SAP already recorded it.
            logger.exception("Failed to write approval audit for request %s", pk)


class InvoiceApprovalHistoryView(ApprovalBaseView):
    """GET /api/v1/invoice-approvals/invoices/<pk>/history/ — SAP approval trail."""

    def get(self, request, pk):
        client = self.sap_client()
        self.assert_manages(client.invoice_approval_warehouses(pk))
        data = client.invoice_approval_history(pk)
        return Response(data)


class InvoiceApprovalPendingCountView(ApprovalBaseView):
    """GET /api/v1/invoice-approvals/invoices/pending-count/?whs=GP-FG — sidebar badge."""

    def get(self, request):
        whs = (request.query_params.get("whs") or "").strip()
        if not whs:
            raise SAPValidationError("whs (warehouse) is required")
        self.assert_manages([whs])
        pending = self.sap_client().count_pending_invoice_approvals(whs)
        return Response({"pending": pending, "total": pending})


class InvoiceApprovalAuditView(ApprovalBaseView):
    """GET /api/v1/invoice-approvals/invoices/<pk>/audit/ — local (JI-side) audit rows."""

    def get(self, request, pk):
        rows = InvoiceApprovalAudit.objects.filter(
            approval_code=pk, company=self.company
        )
        return Response(InvoiceApprovalAuditSerializer(rows, many=True).data)
