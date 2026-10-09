"""
The expense claims API. Two screens sit on it:

* **Expense Entry** -- the expenses you put in (``claims/?by_me=1``), a form
  to put in another (``claims/`` POST), and the same form to change one
  (``claims/<id>/`` PUT) until it is approved. Its bills go up after it is
  saved (``claims/<id>/attachments/`` POST) and come off with
  ``attachments/<id>/`` DELETE. ``companies/``, ``budgets/`` and
  ``gl-accounts/`` feed its pickers.
* **Expense Approval** -- every expense (``claims/``), approved or rejected
  by an expense approver (``claims/<id>/decide/``).

Claims are common to every company: nothing here is narrowed by the company on
the request header. The SAP pickers read the company the form names instead.
"""

from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .constants import (
    COMPANY_LABELS,
    DEFAULT_BUDGET_CODE,
    GL_ACCOUNT_SEARCH_LIMIT,
    MAX_LIST_ROWS,
)
from .hana_reader import ExpenseSapReader
from .models import ExpenseClaim, ExpenseClaimStatus
from .permissions import CanApproveExpenseClaims, CanSubmitExpenseClaim
from .serializers import (
    BudgetSerializer,
    DecideClaimSerializer,
    ExpenseClaimAttachmentSerializer,
    ExpenseClaimSerializer,
    GLAccountSerializer,
    SubmitClaimSerializer,
)

BASE_PERMISSIONS = [IsAuthenticated, HasCompanyContext]
SUBMITTER = BASE_PERMISSIONS + [CanSubmitExpenseClaim]
APPROVER = BASE_PERMISSIONS + [CanApproveExpenseClaims]


def _sap_unavailable(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _form(request):
    """The expense form, validated, with its company looked up."""
    serializer = SubmitClaimSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    data["company"] = services.company_by_code(data["company"])
    return data


class ExpenseClaimListCreateAPI(APIView):
    """GET the expenses; POST a new one.

    ``?by_me=1`` is the caller's own, anybody's to read. Without it, every
    expense -- the approvers' list. ``?status=`` narrows either.
    """

    def get_permissions(self):
        return [p() for p in (SUBMITTER if self.request.method == "POST" else BASE_PERMISSIONS)]

    def get(self, request):
        claims = (
            ExpenseClaim.objects.filter(is_active=True)
            .select_related("company", "created_by", "decided_by")
            .prefetch_related("attachments")
        )
        if request.query_params.get("by_me") in ("1", "true"):
            claims = claims.filter(created_by=request.user)
        elif not CanApproveExpenseClaims().has_permission(request, self):
            raise PermissionDenied("Only an expense approver can see every expense.")
        wanted = (request.query_params.get("status") or "").upper()
        shown = claims.filter(status=wanted) if wanted in ExpenseClaimStatus.values else claims
        return Response(
            {
                "results": ExpenseClaimSerializer(
                    shown[:MAX_LIST_ROWS], many=True, context={"request": request}
                ).data,
                "counts": services.counts(claims),
            }
        )

    def post(self, request):
        try:
            claim = services.submit(user=request.user, **_form(request))
        except SAPConnectionError as exc:
            return _sap_unavailable(exc)
        return Response(
            ExpenseClaimSerializer(claim, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ExpenseClaimDetailAPI(APIView):
    """PUT the whole expense again: its submitter's edit, until it is approved."""

    permission_classes = SUBMITTER

    def put(self, request, pk):
        try:
            claim = services.edit(user=request.user, claim_id=pk, **_form(request))
        except SAPConnectionError as exc:
            return _sap_unavailable(exc)
        return Response(ExpenseClaimSerializer(claim, context={"request": request}).data)


class ExpenseClaimAttachmentAPI(APIView):
    """POST ``files`` (multipart, several at once): bills on an expense you put in.

    Each file is checked on its own and one bad file does not throw the others
    away; the answer says what was attached and what was refused.
    """

    permission_classes = SUBMITTER
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not uploads:
            raise ValidationError({"files": "Pick a file to attach."})

        attached, refused = [], []
        for upload in uploads:
            try:
                attached.append(services.attach(user=request.user, claim_id=pk, upload=upload))
            except ValidationError as exc:
                refused.append({"filename": getattr(upload, "name", ""), "reason": exc.detail})

        return Response(
            {
                "attached": ExpenseClaimAttachmentSerializer(
                    attached, many=True, context={"request": request}
                ).data,
                "refused": refused,
            },
            status=status.HTTP_201_CREATED if attached else status.HTTP_400_BAD_REQUEST,
        )


class ExpenseClaimAttachmentDetailAPI(APIView):
    """DELETE a file off an expense you put in, until it is approved."""

    permission_classes = SUBMITTER

    def delete(self, request, pk):
        services.remove_attachment(user=request.user, attachment_id=pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ExpenseClaimDecideAPI(APIView):
    """POST ``{approve, note}``: an expense approver's verdict."""

    permission_classes = APPROVER

    def post(self, request, pk):
        serializer = DecideClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claim = services.decide(
            user=request.user,
            claim_id=pk,
            approve=serializer.validated_data["approve"],
            note=serializer.validated_data["note"],
        )
        return Response(ExpenseClaimSerializer(claim, context={"request": request}).data)


class CompanyListAPI(APIView):
    """GET the form's "Branch" choices: Oil, Mart and Beverages."""

    permission_classes = SUBMITTER

    def get(self, request):
        return Response(
            [{"code": c.code, "name": COMPANY_LABELS[c.code]} for c in services.companies()]
        )


class BudgetListAPI(APIView):
    """GET ``?company=`` -- its SAP budgets (dimension 3), Factory flagged as the default.

    503 when SAP is unreachable.
    """

    permission_classes = SUBMITTER

    def get(self, request):
        company = services.company_by_code(request.query_params.get("company"))
        try:
            rows = ExpenseSapReader(company.code).budgets()
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_unavailable(exc)
        for row in rows:
            row["is_default"] = row["budget_code"].strip().upper() == DEFAULT_BUDGET_CODE
        return Response(BudgetSerializer(rows, many=True).data)


class GLAccountSearchAPI(APIView):
    """GET ``?company=&search=`` -- that company's postable expense G/L accounts."""

    permission_classes = SUBMITTER

    def get(self, request):
        company = services.company_by_code(request.query_params.get("company"))
        try:
            rows = ExpenseSapReader(company.code).expense_accounts(
                request.query_params.get("search", ""), limit=GL_ACCOUNT_SEARCH_LIMIT
            )
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_unavailable(exc)
        return Response(GLAccountSerializer(rows, many=True).data)
