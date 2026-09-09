"""APIViews for the invoice-approval module (OMS + SAP sources).

Head-office billing raises A/R invoices two ways, and the approver page shows
both: entries logged in the external OMS service (the default view — proxied via
:class:`invoice_approval.oms.OmsClient`, ``oms-invoices/`` routes), and drafts
held directly by SAP's approval procedure (behind a "show SAP" toggle — read from
HANA via :class:`sap_client.client.SAPClient`, decided through the Service Layer,
``invoices/`` routes). On a successful decision on either source we also write a
local :class:`InvoiceApprovalAudit` row so JI can see which employee acted —
SAP and OMS themselves only ever see the shared service account.

``ApprovalBaseView`` mirrors ``marketplace/views.py`` ``MpBaseView`` — per-method
permissions and a centralized ``handle_exception`` that maps the SAP domain errors
to HTTP status codes; ``OmsApprovalBaseView`` adds the OMS ones on top.
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.models import SapApproverIdentity
from warehouse.services import warehouse_scope

from . import permissions as approval_perms
from .models import InvoiceApprovalAudit
from .oms import OmsClient, OMSConnectionError, OMSDataError, OMSValidationError
from .serializers import (
    InvoiceApprovalAuditSerializer,
    InvoiceListQuerySerializer,
    InvoiceStatusUpdateSerializer,
    OmsInvoiceListQuerySerializer,
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

    def my_sap_code(self):
        """The SAP account the caller acts as in this company, if mapped."""
        return SapApproverIdentity.code_for(self.request.user, self.company)

    def configured_approvers(self) -> set:
        """SAP user codes this company holds a password for, upper-cased."""
        credentials = settings.SAP_APPROVER_CREDENTIALS.get(self.company.code) or {}
        return set(credentials)


class InvoiceApprovalListView(ApprovalBaseView):
    """GET /api/v1/invoice-approvals/invoices/?whs=GP-FG&status=PENDING."""

    def get(self, request):
        query = InvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        warehouse = query.validated_data["whs"]
        self.assert_manages([warehouse])
        rows = self.sap_client().list_invoice_approvals(
            warehouse=warehouse,
            status=query.validated_data.get("status"),
        )
        # SAP accepts a decision only from the authorizer it named, so say per
        # row whether that is this caller and whether we can sign as them.
        available = self.configured_approvers()
        mine = (self.my_sap_code() or "").upper()
        for row in rows:
            code = (row.get("approver_code") or "").strip().upper()
            row["credentials_configured"] = bool(code) and code in available
            row["is_mine"] = bool(code) and bool(mine) and code == mine
            row["can_decide"] = bool(
                row["is_mine"]
                and row["credentials_configured"]
                and row.get("status") == "PENDING"
            )
        return Response(rows)


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

        # Re-read the stage from SAP: the authorizer is whoever SAP says it is
        # right now, not whoever the page was rendered with.
        stage = client.invoice_approval_stage(pk)
        refusal = self._identity_refusal(stage)
        if refusal is not None:
            return refusal
        approver = (stage.get("approver_code") or "").strip()

        # SAP stamps the authorizer; carry the real actor in the remarks.
        if decision == "REJECTED":
            remarks = f"{rejection_reason} — {self.approver_name()} (Factory app)"
        else:
            remarks = f"Approved by {self.approver_name()} (Factory app)"

        result = client.decide_invoice_approval(
            pk, approve=(decision == "APPROVED"), remarks=remarks, approver=approver
        )

        # Record who actually acted.
        self._write_audit(request, pk, decision, rejection_reason, data, result)

        # A/R invoices raised from THIS app are posted right after their
        # approval (batches allocated FIFO); head-office/OMS drafts are left to
        # their own originators exactly as before.
        if decision == "APPROVED":
            result = dict(result, **self._post_own_ar_draft(request, pk))
        return Response(result)

    def _identity_refusal(self, stage):
        """Refuse unless the caller IS the authorizer SAP named, and signable.

        Three distinct causes, three distinct fixes — a single "not permitted"
        would leave the approver guessing which one applies. Returns ``None``
        when the decision may go ahead.
        """
        if stage["status"] != "PENDING":
            return Response(
                {"detail": f"This invoice is already {stage['status'].lower()} in SAP."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        approver = (stage.get("approver_code") or "").strip()
        if not approver:
            return Response(
                {
                    "detail": (
                        "SAP does not name an authorizer on this request's current "
                        "stage, so it cannot be decided from the app."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        name = stage.get("approver_name")
        who = f"{approver} ({name})" if name else approver

        mine = self.my_sap_code()
        if not mine:
            return Response(
                {
                    "detail": (
                        f"Your account is not linked to a SAP user in "
                        f"{self.company.code}, so the app cannot tell whether you are "
                        f"{who}. Ask an administrator to map you on the SAP "
                        "Identities page."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if mine.upper() != approver.upper():
            return Response(
                {
                    "detail": (
                        f"This invoice is waiting on {who}. You act as {mine}, and SAP "
                        f"accepts a decision only from the authorizer it named — so "
                        f"only {approver} can decide this one."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if approver.upper() not in self.configured_approvers():
            return Response(
                {
                    "detail": (
                        f"Your SAP password for {who} is not configured, so the app "
                        "cannot sign in as you to record this. Ask an administrator "
                        "to add it, or decide this one in SAP."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _post_own_ar_draft(self, request, wdd_code) -> dict:
        from ar_invoice.services import ARInvoiceService

        service = ARInvoiceService(company_code=self.company.code)
        posting = service.find_by_approval_code(wdd_code)
        if posting is None:
            return {}
        try:
            posting = service.post_approved_draft(posting.id, request.user)
        except (ValueError, SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            logger.warning(
                "Approved A/R draft %s could not be posted: %s",
                posting.sap_draft_entry, exc,
            )
            return {
                "posting_id": posting.id,
                "posting_status": posting.status,
                "warning": (
                    "Approved in SAP, but posting the invoice failed: "
                    f"{exc} — retry from the AR Invoices page."
                ),
            }
        return {
            "posting_id": posting.id,
            "posting_status": posting.status,
            "sap_doc_entry": posting.sap_doc_entry,
            "sap_doc_num": posting.sap_doc_num,
        }

    def _write_audit(self, request, pk, decision, rejection_reason, data, result):
        try:
            InvoiceApprovalAudit.objects.create(
                approval_code=pk,
                source=InvoiceApprovalAudit.SOURCE_SAP,
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
            approval_code=pk,
            company=self.company,
            source=InvoiceApprovalAudit.SOURCE_SAP,
        )
        return Response(InvoiceApprovalAuditSerializer(rows, many=True).data)


# ──────────────────────────────────────────────────────────────────────────
# OMS invoices — the external OMS service where head-office billing logs A/R
# invoices before they reach SAP. Same page, same permissions and warehouse
# scoping as the SAP A/R views above; only the backend the data comes from
# (and the id-space, OMS's own invoice-log ids) differs.
# ──────────────────────────────────────────────────────────────────────────


class OmsApprovalBaseView(ApprovalBaseView):
    """Adds OMS exception mapping + the OMS_ENABLED gate to ApprovalBaseView."""

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


class OmsInvoiceListView(OmsApprovalBaseView):
    """GET /api/v1/invoice-approvals/oms-invoices/?whs=GP-FG&status=PENDING."""

    def get(self, request):
        query = OmsInvoiceListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        warehouse = query.validated_data["whs"]
        self.assert_manages([warehouse])
        data = OmsClient().list_invoices(
            warehouse=warehouse,
            status=query.validated_data.get("status"),
        )
        return Response(data)


class OmsInvoiceStatusUpdateView(OmsApprovalBaseView):
    """PATCH /api/v1/invoice-approvals/oms-invoices/<pk>/status/ — approve or reject.

    ``pk`` is the OMS invoice-log id. OMS has no per-id read endpoint to resolve
    the invoice's warehouse server-side, so the frontend sends it in the body and
    we scope on that (the list the row came from was already scope-checked).
    """

    def patch(self, request, pk):
        serializer = InvoiceStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        decision = data["status"]
        rejection_reason = data.get("rejection_reason", "")

        warehouse = (data.get("warehouse") or "").strip()
        if warehouse:
            self.assert_manages([warehouse])

        result = OmsClient().update_status(
            pk, decision, rejection_reason or None, user=self.approver_name()
        )

        # Record who actually acted (OMS only ever sees the shared service identity).
        self._write_audit(request, pk, decision, rejection_reason, data, result)
        return Response(result)

    def _write_audit(self, request, pk, decision, rejection_reason, data, result):
        try:
            InvoiceApprovalAudit.objects.create(
                approval_code=pk,
                source=InvoiceApprovalAudit.SOURCE_OMS,
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
            # Auditing must never break the approval itself — OMS already recorded it.
            logger.exception("Failed to write OMS approval audit for invoice %s", pk)


class OmsInvoiceHistoryView(OmsApprovalBaseView):
    """GET /api/v1/invoice-approvals/oms-invoices/<pk>/history/ — OMS audit trail."""

    def get(self, request, pk):
        data = OmsClient().get_history(pk)
        return Response(data)


class OmsInvoicePendingCountView(OmsApprovalBaseView):
    """GET /api/v1/invoice-approvals/oms-invoices/pending-count/?whs=GP-FG — badge."""

    def get(self, request):
        whs = (request.query_params.get("whs") or "").strip()
        if not whs:
            raise OMSValidationError("whs (warehouse) is required")
        self.assert_manages([whs])
        pending = len(OmsClient().list_invoices(warehouse=whs, status="PENDING"))
        return Response({"pending": pending, "total": pending})


class OmsInvoiceAuditView(OmsApprovalBaseView):
    """GET /api/v1/invoice-approvals/oms-invoices/<pk>/audit/ — local audit rows."""

    def get(self, request, pk):
        rows = InvoiceApprovalAudit.objects.filter(
            approval_code=pk,
            company=self.company,
            source=InvoiceApprovalAudit.SOURCE_OMS,
        )
        return Response(InvoiceApprovalAuditSerializer(rows, many=True).data)
