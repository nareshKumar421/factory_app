"""SAP Finance API.

Ledger reads (journal entries, general ledger, chart of accounts) and the
budget screen, ported from SAP Portal's ``/api/sap/journal-entries``,
``/gl-ledger``, ``/chart-of-accounts``, ``/lookup/gl-search`` and ``/budget*``.

Every view: login, the ``Company-Code`` context, then its own right. SAP errors
map the way every JI SAP endpoint maps them — SAP's refusal → 400, SAP
unreachable → 503, SAP broken → 502.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import outstanding, services
from .models import SapBudgetChange
from .permissions import CanManageSapBudgets, CanViewOutstanding, CanViewSapBudgets, CanViewSapLedgers
from .serializers import (
    BudgetWriteSerializer,
    JournalEntryFilterSerializer,
    LedgerFilterSerializer,
    SapBudgetChangeSerializer,
    budget_from_sap,
)

logger = logging.getLogger(__name__)


class _SapFinanceView(APIView):
    """Company context and SAP error shaping shared by every endpoint."""

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
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


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------


class JournalEntryListAPI(_SapFinanceView):
    """GET — newest journal entries, each with its lines. Filters: trans_id,
    number, reference, trans_type, date_from, date_to, limit (≤ 100)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapLedgers]

    def get(self, request):
        filters = JournalEntryFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        entries = self.sap().journal_entries(**filters.validated_data)
        return Response({"results": entries, "count": len(entries)})


class ChartOfAccountsAPI(_SapFinanceView):
    """GET — the OACT tree with title accounts rolled up. ?search= ?drawer=1–10"""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapLedgers]

    def get(self, request):
        return Response(
            self.sap().chart_of_accounts(
                search=request.query_params.get("search", ""),
                drawer=request.query_params.get("drawer"),
            )
        )


class GeneralLedgerAPI(_SapFinanceView):
    """GET ?account=… — postings to one G/L account or partner, newest first,
    with the balance after each. ?date_from ?date_to ?limit (≤ 1000)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapLedgers]

    def get(self, request):
        filters = LedgerFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        return Response(
            self.sap().general_ledger(
                data["account"],
                date_from=data.get("date_from"),
                date_to=data.get("date_to"),
                limit=data.get("limit", 200),
            )
        )


class LedgerAccountSearchAPI(_SapFinanceView):
    """GET ?search=… — G/L accounts and partners, for the ledger's picker."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapLedgers]

    def get(self, request):
        search = (request.query_params.get("search") or "").strip()
        if len(search) < 2:
            return Response([])
        return Response(self.sap().ledger_account_search(search))


# ---------------------------------------------------------------------------
# Budgets (SAP BUDGET UDO)
# ---------------------------------------------------------------------------


class BudgetListCreateAPI(_SapFinanceView):
    """GET — every budget document in SAP. POST — create one."""

    def get_permissions(self):
        right = CanViewSapBudgets if self.request.method == "GET" else CanManageSapBudgets
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def get(self, request):
        return Response([budget_from_sap(doc) for doc in self.sap().list_budgets()])

    def post(self, request):
        serializer = BudgetWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = services.create_budget(self.company, request.user, serializer.to_sap())
        return Response(budget_from_sap(document), status=status.HTTP_201_CREATED)


class BudgetDetailAPI(_SapFinanceView):
    """GET / PUT (replace every line) / DELETE one budget document."""

    def get_permissions(self):
        right = CanViewSapBudgets if self.request.method == "GET" else CanManageSapBudgets
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def _document(self, doc_entry):
        document = self.sap().get_budget(doc_entry)
        if document is None:
            raise NotFound(f"Budget {doc_entry} was not found in SAP.")
        return document

    def get(self, request, doc_entry):
        return Response(budget_from_sap(self._document(doc_entry)))

    def put(self, request, doc_entry):
        serializer = BudgetWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self._document(doc_entry)
        services.update_budget(self.company, request.user, doc_entry, serializer.to_sap())
        return Response(budget_from_sap(self._document(doc_entry)))

    def delete(self, request, doc_entry):
        current = self._document(doc_entry)
        services.delete_budget(self.company, request.user, doc_entry, current)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BudgetChangeListAPI(_SapFinanceView):
    """GET — who changed which budget from this app, newest first. ?doc_entry="""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapBudgets]

    def get(self, request):
        rows = SapBudgetChange.objects.filter(company=self.company).select_related("created_by")
        doc_entry = request.query_params.get("doc_entry")
        if doc_entry:
            try:
                rows = rows.filter(doc_entry=int(doc_entry))
            except ValueError:
                return Response({"detail": "doc_entry must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SapBudgetChangeSerializer(rows[:200], many=True).data)


# ---------------------------------------------------------------------------
# Outstanding (from EXIM): party balances, open bills, open GRPOs, aging
# ---------------------------------------------------------------------------


def _flag(params, name) -> bool:
    return params.get(name) in ("1", "true", "yes")


def _int(params, name, default) -> int:
    try:
        return int(params.get(name, default))
    except (TypeError, ValueError):
        return default


class PartyOutstandingAPI(_SapFinanceView):
    """GET ?side=vendor|customer [&oil_suppliers=1] [&refresh=1] — every party
    SAP holds a balance against, with its last bill and last payment."""

    def get_permissions(self):
        side = "customer" if self.request.query_params.get("side") == "customer" else "vendor"
        return [IsAuthenticated(), HasCompanyContext(), CanViewOutstanding(f"{side}_outstanding")()]

    def get(self, request):
        params = request.query_params
        side = "customer" if params.get("side") == "customer" else "vendor"
        return Response(outstanding.party_outstanding(
            self.company, side, oil_suppliers=_flag(params, "oil_suppliers") and side == "vendor",
            refresh=_flag(params, "refresh"),
        ))


class OpenBillsAPI(_SapFinanceView):
    """GET ?side=vendor|customer — open A/P or A/R invoices: totals by overdue
    bucket and party, and a page of bills. Filters: q, card_code, group, bucket,
    oil_suppliers (vendors); sort (due_date, doc_date, due, overdue_days,
    party) with desc=1; page, page_size (≤ 200); refresh=1."""

    def get_permissions(self):
        report = "open_ar" if self.request.query_params.get("side") == "customer" else "open_ap"
        return [IsAuthenticated(), HasCompanyContext(), CanViewOutstanding(report)()]

    def get(self, request):
        params = request.query_params
        side = "customer" if params.get("side") == "customer" else "vendor"
        return Response(outstanding.open_bills(
            self.company, side,
            q=params.get("q", "").strip(), card_code=params.get("card_code", "").strip(),
            group=params.get("group", "").strip(), bucket=params.get("bucket", "").strip(),
            oil_suppliers=_flag(params, "oil_suppliers") and side == "vendor",
            sort=params.get("sort", "due_date"), descending=_flag(params, "desc"),
            page=_int(params, "page", 1), page_size=_int(params, "page_size", 50),
            refresh=_flag(params, "refresh"),
        ))


class OpenGrpoAPI(_SapFinanceView):
    """GET [?raw_material=1] [&refresh=1] — goods received and not yet billed."""

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanViewOutstanding("open_grpos")()]

    def get(self, request):
        params = request.query_params
        return Response(outstanding.open_grpos(
            self.company, raw_material_only=_flag(params, "raw_material"), refresh=_flag(params, "refresh"),
        ))


class CustomerAgingAPI(_SapFinanceView):
    """GET [?basis=due|bill] [&q &group &sales_employee] [&card_code=<one
    customer's documents>] [&refresh=1] — what each customer owes, by age."""

    def get_permissions(self):
        return [IsAuthenticated(), HasCompanyContext(), CanViewOutstanding("customer_aging")()]

    def get(self, request):
        params = request.query_params
        return Response(outstanding.customer_aging(
            self.company, basis="bill" if params.get("basis") == "bill" else "due",
            q=params.get("q", "").strip(), group=params.get("group", "").strip(),
            sales_employee=params.get("sales_employee", "").strip(),
            card_code=params.get("card_code", "").strip(), refresh=_flag(params, "refresh"),
        ))
