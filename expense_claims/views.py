"""
The expense claims API. Two screens sit on it:

* **Expense Entry** -- the expenses you put in (``claims/?by_me=1``), a form
  to put in another, and the same form to change one (``claims/<id>/`` PUT)
  until it is approved -- the whole expense:
  branch (the company), budget (its SAP business place), G/L account,
  comment, amount, and who it goes to. ``companies/``, ``budgets/``,
  ``gl-accounts/`` and ``approvers/`` feed its pickers; ``claims/`` POST saves it.
* **Expense Approval** -- ``claims/`` GET lists the expenses and
  ``claims/<id>/decide/`` is the approve or reject. Anybody can be sent an
  expense, so anybody may read what was sent to them and decide it; the whole
  list is the cash book approvers'.

Claims are common to every company: nothing here is narrowed by the company on
the request header. The SAP pickers read the company the page names instead.
"""

from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from cash_book.hana_reader import GLAccountReader
from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .constants import (
    COMPANY_LABELS,
    DEFAULT_BUDGET_NAME,
    GL_ACCOUNT_SEARCH_LIMIT,
    MAX_LIST_ROWS,
)
from .hana_reader import BranchReader
from .models import ExpenseClaim, ExpenseClaimStatus
from .permissions import CanApproveExpenseClaims, CanSubmitExpenseClaim
from .serializers import (
    DecideClaimSerializer,
    ExpenseClaimSerializer,
    ApproverSerializer,
    GLAccountSerializer,
    SubmitClaimSerializer,
)

BASE_PERMISSIONS = [IsAuthenticated, HasCompanyContext]
SUBMITTER = BASE_PERMISSIONS + [CanSubmitExpenseClaim]


def _sap_unavailable(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class ExpenseClaimListCreateAPI(APIView):
    """GET the expenses; POST a new one (anybody who may submit).

    GET takes ``?status=``, and ``?by_me=1`` (what the caller put in) or
    ``?for_me=1`` (what was sent to them) -- both anybody's to read. Every
    expense, without either, is the cash book approvers'.
    """

    def get_permissions(self):
        return [p() for p in (SUBMITTER if self.request.method == "POST" else BASE_PERMISSIONS)]

    def get(self, request):
        claims = ExpenseClaim.objects.filter(is_active=True).select_related(
            "company", "created_by", "approver", "decided_by"
        )
        if request.query_params.get("by_me") in ("1", "true"):
            claims = claims.filter(created_by=request.user)
        elif request.query_params.get("for_me") in ("1", "true"):
            claims = claims.filter(approver=request.user)
        elif not CanApproveExpenseClaims().has_permission(request, self):
            raise PermissionDenied("Only a cash book approver can see every expense.")
        wanted = (request.query_params.get("status") or "").upper()
        shown = claims.filter(status=wanted) if wanted in ExpenseClaimStatus.values else claims
        return Response(
            {
                "results": ExpenseClaimSerializer(shown[:MAX_LIST_ROWS], many=True).data,
                "counts": services.counts(claims),
            }
        )

    def post(self, request):
        serializer = SubmitClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            claim = services.submit(
                user=request.user,
                company=services.company_by_code(data["company"]),
                budget_id=data["budget_id"],
                gl_account_code=data["gl_account_code"],
                comment=data["comment"],
                amount=data["amount"],
                approver=data["approver"],
            )
        except SAPConnectionError as exc:
            return _sap_unavailable(exc)
        return Response(ExpenseClaimSerializer(claim).data, status=status.HTTP_201_CREATED)


class ExpenseClaimDetailAPI(APIView):
    """PUT the whole expense again: its submitter's edit, until it is approved."""

    permission_classes = SUBMITTER

    def put(self, request, pk):
        serializer = SubmitClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            claim = services.edit(
                user=request.user,
                claim_id=pk,
                company=services.company_by_code(data["company"]),
                budget_id=data["budget_id"],
                gl_account_code=data["gl_account_code"],
                comment=data["comment"],
                amount=data["amount"],
                approver=data["approver"],
            )
        except SAPConnectionError as exc:
            return _sap_unavailable(exc)
        return Response(ExpenseClaimSerializer(claim).data)


class ExpenseClaimDecideAPI(APIView):
    """POST ``{approve, note}``: the verdict of whoever the expense was sent to.

    No right is needed beyond being that person, which the service checks.
    """

    permission_classes = BASE_PERMISSIONS

    def post(self, request, pk):
        serializer = DecideClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claim = services.decide(
            user=request.user,
            claim_id=pk,
            approve=serializer.validated_data["approve"],
            note=serializer.validated_data["note"],
        )
        return Response(ExpenseClaimSerializer(claim).data)


class CompanyListAPI(APIView):
    """GET the page's "Branch" choices: Oil, Mart and Beverages."""

    permission_classes = SUBMITTER

    def get(self, request):
        return Response(
            [{"code": c.code, "name": COMPANY_LABELS[c.code]} for c in services.companies()]
        )


class ApproverListAPI(APIView):
    """GET everyone an expense may go to: every active user, but the caller."""

    permission_classes = SUBMITTER

    def get(self, request):
        people = services.approvers().exclude(pk=request.user.pk)
        return Response(ApproverSerializer(people, many=True).data)


class BudgetListAPI(APIView):
    """GET ``?company=`` -- its SAP business places, the page's "Budget".

    FACTORY comes back flagged as the default. 503 when SAP is unreachable.
    """

    permission_classes = SUBMITTER

    def get(self, request):
        company = services.company_by_code(request.query_params.get("company"))
        try:
            rows = BranchReader(company.code).list()
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_unavailable(exc)
        return Response(
            [
                {
                    "budget_id": row["branch_id"],
                    "budget_name": row["branch_name"],
                    "is_default": row["branch_name"].strip().upper() == DEFAULT_BUDGET_NAME,
                }
                for row in rows
            ]
        )


class GLAccountSearchAPI(APIView):
    """GET ``?company=&search=`` -- that company's postable SAP G/L accounts."""

    permission_classes = SUBMITTER

    def get(self, request):
        company = services.company_by_code(request.query_params.get("company"))
        try:
            rows = GLAccountReader(company.code).search(
                request.query_params.get("search", ""), limit=GL_ACCOUNT_SEARCH_LIMIT
            )
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_unavailable(exc)
        return Response(GLAccountSerializer(rows, many=True).data)
