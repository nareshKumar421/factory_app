"""BOM Changes API.

Ported from SAP Portal's ``/api/bom-requests`` (``backend_v1/server.js``) and its
BOM reads ``/api/sap/bom/search`` and ``/api/sap/bom/:treeCode``
(``routes/sap.js`` lines 799-839).

Every view: login, the ``Company-Code`` context, then a BOM right. Requests
are this company's only (another company's is a 404) and are written to this
company's SAP. SAP's refusal → 400 with SAP's words, SAP unreachable → 503,
SAP broken → 502, a new BOM SAP already has → 409.
"""

import logging

from django.db.models import Count, Prefetch, Q
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import services, workflow
from .constants import ApprovalAction
from .models import BOMChangeApproval, BOMChangeRequest
from .permissions import (
    CanDecideBomChanges,
    CanPushBomDirectly,
    CanRequestBomChanges,
    CanViewBomChanges,
)
from .serializers import (
    BOMChangeRequestInputSerializer,
    BOMChangeRequestSerializer,
    DecisionSerializer,
    ListFilterSerializer,
)

logger = logging.getLogger(__name__)


class _BomChangesView(APIView):
    """Company context and SAP error shaping shared by every endpoint."""

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            # The message matters here: a write that timed out says SAP may
            # still have saved it, and to check before trying again.
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {"detail": str(exc) or "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in %s: %s", type(self).__name__, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company

    def sap(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)

    def requests(self):
        """This company's requests, with what the serializer and row flags read."""
        return (
            BOMChangeRequest.objects.filter(company=self.company)
            .select_related("created_by", "sap_pushed_by", "cancelled_by")
            .prefetch_related(
                "lines",
                Prefetch(
                    "approvals",
                    queryset=BOMChangeApproval.objects.select_related("decided_by"),
                ),
            )
        )

    def serialize(self, row, detail=False):
        context = {
            "request": self.request,
            "levels": workflow.approval_levels(),
            "detail": detail,
        }
        return BOMChangeRequestSerializer(row, context=context).data

    def detail_of(self, pk):
        row = self.requests().filter(pk=pk).first()
        if row is None:
            raise NotFound("BOM change request not found.")
        return self.serialize(row, detail=True)


# ---------------------------------------------------------------------------
# Workflow and SAP BOM viewer
# ---------------------------------------------------------------------------


class WorkflowAPI(_BomChangesView):
    """GET — the approval ladder as configured: levels and who signs each one."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBomChanges]

    def get(self, request):
        levels = workflow.approval_levels()
        return Response({"levels": levels, "steps": workflow.steps(levels)})


class SapBomListAPI(_BomChangesView):
    """GET ?search=&limit= — trees in SAP by parent code, description or item name."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBomChanges]

    def get(self, request):
        search = (request.query_params.get("search") or request.query_params.get("q") or "").strip()
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return Response(self.sap().search_product_trees(search, limit=limit))


class SapBomDetailAPI(_BomChangesView):
    """GET — one tree with its header and every line (items and resources)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBomChanges]

    def get(self, request, tree_code):
        tree = self.sap().get_product_tree(tree_code.strip())
        if tree is None:
            raise NotFound(f"SAP has no BOM for {tree_code}.")
        return Response(tree)


# ---------------------------------------------------------------------------
# Change requests
# ---------------------------------------------------------------------------


class RequestListCreateAPI(_BomChangesView):
    """GET — this company's requests, newest first, with status counts.
    ?status=A,B ?kind= ?mine=true ?actionable=true (my turn) ?search= ?limit=

    POST — raise a request (CREATE a new BOM or UPDATE an existing one).
    """

    def get_permissions(self):
        right = CanViewBomChanges if self.request.method == "GET" else CanRequestBomChanges
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def get(self, request):
        filters = ListFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        user = request.user
        levels = workflow.approval_levels()

        base = BOMChangeRequest.objects.filter(company=self.company)
        if data.get("kind"):
            base = base.filter(kind=data["kind"])
        if data.get("mine"):
            base = base.filter(created_by=user)
        search = (data.get("search") or "").strip()
        if search:
            base = base.filter(Q(item_code__icontains=search) | Q(item_name__icontains=search))

        # "My turn": a status whose right the caller holds, on a request they
        # have not approved yet.
        mine_to_approve = BOMChangeApproval.objects.filter(
            action=ApprovalAction.APPROVE, decided_by=user
        ).values("request_id")
        actionable = base.filter(
            status__in=services.actionable_statuses(user, levels)
        ).exclude(pk__in=mine_to_approve)

        counts = {row["status"]: row["n"] for row in base.values("status").annotate(n=Count("id"))}
        counts["ACTIONABLE"] = actionable.count()

        rows = actionable if data.get("actionable") else base
        if data.get("status"):
            rows = rows.filter(status__in=data["status"])
        ids = list(rows.order_by("-submitted_at", "-id").values_list("id", flat=True)[: data["limit"]])
        by_id = {row.id: row for row in self.requests().filter(id__in=ids)}
        results = [self.serialize(by_id[pk]) for pk in ids if pk in by_id]
        return Response({"results": results, "count": len(results), "counts": counts, "levels": levels})

    def post(self, request):
        serializer = BOMChangeRequestInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.create_request(self.company, request.user, serializer.validated_data)
        return Response(self.detail_of(row.pk), status=status.HTTP_201_CREATED)


class DirectPushAPI(_BomChangesView):
    """POST — raise a request and write it to SAP now, skipping approval
    (the portal admin's ``direct-create`` / ``direct-update``)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPushBomDirectly]

    def post(self, request):
        serializer = BOMChangeRequestInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.direct_push(self.company, request.user, serializer.validated_data)
        return Response(self.detail_of(row.pk), status=status.HTTP_201_CREATED)


class RequestDetailAPI(_BomChangesView):
    """GET — one request with its lines, decisions, progress and SAP result."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBomChanges]

    def get(self, request, pk):
        return Response(self.detail_of(pk))


class ApproveAPI(_BomChangesView):
    """POST {remarks} — approve at the current level. The final approval writes SAP."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDecideBomChanges]

    def post(self, request, pk):
        body = DecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.approve(self.company, pk, request.user, body.validated_data["remarks"])
        return Response(self.detail_of(pk))


class RejectAPI(_BomChangesView):
    """POST {remarks} — reject at the current level. Nothing reaches SAP."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDecideBomChanges]

    def post(self, request, pk):
        body = DecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.reject(self.company, pk, request.user, body.validated_data["remarks"])
        return Response(self.detail_of(pk))


class CancelAPI(_BomChangesView):
    """POST — withdraw a pending request (its submitter, or a pusher)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBomChanges]

    def post(self, request, pk):
        services.cancel(self.company, pk, request.user)
        return Response(self.detail_of(pk))
