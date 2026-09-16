"""
The cash book's API.

Two screens sit on it. The register (``entries/``) is the book itself -- every
line, with its running balance, filtered by date, direction, branch, G/L
head or free text. The approvals screen (``bunches/``) is the other half of the
sheet's Bunch column: sets of vouchers walked to an approver together.

Everything is scoped to the company on the request header. Each company keeps
its own cash box, so a balance only means anything read against one.
"""

import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .constants import DEFAULT_PAGE_SIZE, GL_ACCOUNT_SEARCH_LIMIT, MAX_PAGE_SIZE
from .hana_reader import GLAccountReader
from .models import (
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    BunchStatus,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from .permissions import (
    CanApproveCashBunch,
    CashBranchPermission,
    CanManageCashBranches,
    CanManageCashBook,
    CanViewCashBook,
    CashBookPermission,
)
from .serializers import (
    AdvanceEntrySerializer,
    AdvanceHolderSerializer,
    AtmAccountSerializer,
    AtmAccountWriteSerializer,
    AtmReceiptSerializer,
    CashBranchSerializer,
    CashBranchWriteSerializer,
    CashBunchDetailSerializer,
    CashBunchSerializer,
    CashEntrySerializer,
    DecisionSerializer,
    GLAccountSerializer,
    MovementSerializer,
    PersonSerializer,
    RecordAdvanceSerializer,
    RecordAtmReceiptSerializer,
    RecordEntrySerializer,
    ResendSerializer,
    SendForApprovalSerializer,
    UpdateEntrySerializer,
)

logger = logging.getLogger(__name__)


def _company(request):
    return request.company.company


def _parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _entry_queryset(request):
    """The register, filtered by whatever the screen controls are set to.

    ``include_cancelled`` is off by default: a cancelled line is out of the
    book, and somebody reading the balance should not have to subtract it back
    out by eye.
    """
    params = request.query_params
    queryset = (
        CashEntry.objects.filter(company=_company(request))
        .select_related("branch", "bunch", "created_by")
        .order_by("-id")
    )

    if params.get("include_cancelled") != "true":
        queryset = queryset.filter(is_active=True)

    date_from = params.get("date_from")
    if date_from:
        queryset = queryset.filter(entry_date__gte=date_from)
    date_to = params.get("date_to")
    if date_to:
        queryset = queryset.filter(entry_date__lte=date_to)

    direction = (params.get("direction") or "").upper()
    if direction in CashDirection.values:
        queryset = queryset.filter(direction=direction)

    branch = _parse_positive_int(params.get("branch"), None)
    if branch:
        queryset = queryset.filter(branch_id=branch)

    gl_account = (params.get("gl_account_code") or "").strip()
    if gl_account:
        queryset = queryset.filter(gl_account_code=gl_account)

    bunch = _parse_positive_int(params.get("bunch"), None)
    if bunch:
        queryset = queryset.filter(bunch_id=bunch)

    approval = (params.get("approval_status") or "").upper()
    if approval == EntryApprovalStatus.UNSENT:
        queryset = queryset.filter(bunch__isnull=True)
    elif approval in {
        EntryApprovalStatus.PENDING,
        EntryApprovalStatus.APPROVED,
        EntryApprovalStatus.REJECTED,
    }:
        queryset = queryset.filter(bunch__status=approval)

    search = (params.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(detail__icontains=search)
            | Q(item__icontains=search)
            | Q(gl_account_name__icontains=search)
            | Q(gl_account_code__icontains=search)
        )

    return queryset


def _branch_queryset(request):
    """Branches of the active company, retired ones only when asked for.

    Each carries how many entries are filed under it, so the settings page can
    say what retiring one would hide.
    """
    queryset = CashBranch.objects.filter(company=_company(request)).annotate(
        entry_count=Count("cash_entries")
    )
    if request.query_params.get("include_retired") != "true":
        queryset = queryset.filter(is_active=True)
    return queryset


class CashBookOptionsAPI(APIView):
    """GET what the entry form and the filters need to offer.

    Sent rather than hardcoded in the client so a new branch or a changed
    right shows up without a release. The two ``can_*`` flags are what the page
    hides its buttons behind -- the endpoints enforce the same rights anyway.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        company = _company(request)
        return Response(
            {
                "branches": CashBranchSerializer(
                    CashBranch.objects.filter(company=company, is_active=True),
                    many=True,
                ).data,
                "directions": [
                    {"value": value, "label": label}
                    for value, label in CashDirection.choices
                ],
                "bunch_statuses": [
                    {"value": value, "label": label}
                    for value, label in BunchStatus.choices
                ],
                "approval_statuses": [
                    {"value": value, "label": label}
                    for value, label in EntryApprovalStatus.choices
                ],
                "balance": services.current_balance(company),
                "gl_account_search_limit": GL_ACCOUNT_SEARCH_LIMIT,
                "can_manage": CanManageCashBook().has_permission(request, self),
                "can_approve": CanApproveCashBunch().has_permission(request, self),
                "can_manage_branches": CanManageCashBranches().has_permission(
                    request, self
                ),
            }
        )


class GLAccountSearchAPI(APIView):
    """GET the SAP G/L heads matching ``?search=``.

    A type-ahead against the live chart of accounts. When SAP is unreachable
    this answers 503 and says so: the book still reads, but a new payment
    cannot be filed against a head nobody can confirm exists.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        reader = GLAccountReader(_company(request).code)
        try:
            rows = reader.search(request.query_params.get("search", ""))
        except (SAPConnectionError, SAPDataError) as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response(GLAccountSerializer(rows, many=True).data)


def _snapshot_gl_account(company, code, fallback_name):
    """Confirm the account against SAP and take its name, if SAP is reachable.

    A blank code is a receipt and needs nothing. A code SAP does not have, or
    will not take a posting on, is refused -- that is the whole reason the head
    is picked rather than typed. But SAP being *down* must not stop cash being
    recorded, so an unreachable SAP falls back to the name the picker sent.
    """
    if not code:
        return ""
    try:
        return GLAccountReader(company.code).resolve(code)["account_name"]
    except SAPConnectionError:
        logger.warning(
            "[Cash book] SAP unreachable while recording against %s; "
            "keeping the name the client sent.",
            code,
        )
        return fallback_name
    except SAPDataError as exc:
        raise ValidationError({"gl_account_code": str(exc)}) from exc


class CashEntryListCreateAPI(APIView):
    """GET the register · POST to write a line into it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request):
        queryset = _entry_queryset(request)

        page = _parse_positive_int(request.query_params.get("page"), 1)
        page_size = min(
            _parse_positive_int(request.query_params.get("page_size"), DEFAULT_PAGE_SIZE),
            MAX_PAGE_SIZE,
        )
        total = queryset.count()
        total_pages = max((total + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        start = (page - 1) * page_size

        return Response(
            {
                "results": CashEntrySerializer(
                    queryset[start : start + page_size], many=True
                ).data,
                "count": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "next": page < total_pages,
                "previous": page > 1,
                # The book's own balance, not the filtered set's. Sent on every
                # page so the header never has to guess.
                "balance": services.current_balance(_company(request)),
                "totals": services.totals(queryset),
            }
        )

    def post(self, request):
        serializer = RecordEntrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company = _company(request)

        code = (data.get("gl_account_code") or "").strip()
        name = _snapshot_gl_account(company, code, data.get("gl_account_name", ""))

        entry = services.record_entry(
            user=request.user,
            company=company,
            entry_date=data["entry_date"],
            direction=data["direction"],
            amount=data["amount"],
            detail=data["detail"],
            branch=data.get("branch"),
            atm_account=data.get("atm_account"),
            advance_holder=data.get("advance_holder"),
            gl_account_code=code,
            gl_account_name=name,
            item=data.get("item", ""),
        )
        return Response(
            CashEntrySerializer(entry).data, status=status.HTTP_201_CREATED
        )


class CashEntryDetailAPI(APIView):
    """GET one line · PATCH to correct it · DELETE to cancel it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def _entry(self, request, pk) -> CashEntry:
        return get_object_or_404(
            CashEntry.objects.select_related("branch", "bunch", "created_by"),
            pk=pk,
            company=_company(request),
        )

    def get(self, request, pk):
        return Response(CashEntrySerializer(self._entry(request, pk)).data)

    def patch(self, request, pk):
        entry = self._entry(request, pk)
        if not entry.is_active:
            return Response(
                {"detail": "This entry has been cancelled and cannot be corrected."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = UpdateEntrySerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = dict(serializer.validated_data)

        if "gl_account_code" in changes:
            code = (changes["gl_account_code"] or "").strip()
            changes["gl_account_code"] = code
            changes["gl_account_name"] = _snapshot_gl_account(
                entry.company, code, changes.get("gl_account_name", "")
            )
        else:
            changes.pop("gl_account_name", None)

        entry = services.update_entry(user=request.user, entry=entry, **changes)
        return Response(CashEntrySerializer(entry).data)

    # PUT behaves as PATCH: the form sends only what changed.
    put = patch

    def delete(self, request, pk):
        entry = self._entry(request, pk)
        services.cancel_entry(user=request.user, entry=entry)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CashBookSummaryAPI(APIView):
    """GET the book's balance, and the in/out of whatever is filtered."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        company = _company(request)
        return Response(
            {
                "balance": services.current_balance(company),
                "filtered": services.totals(_entry_queryset(request)),
                "pending_bunches": CashBunch.objects.filter(
                    company=company, status=BunchStatus.PENDING
                ).count(),
                "unsent_entries": CashEntry.objects.filter(
                    company=company, is_active=True, bunch__isnull=True
                ).count(),
            }
        )


def _bunch_queryset(request):
    queryset = (
        CashBunch.objects.filter(company=_company(request))
        .select_related("sent_by", "decided_by")
        .prefetch_related("entries")
    )
    bunch_status = (request.query_params.get("status") or "").upper()
    if bunch_status in BunchStatus.values:
        queryset = queryset.filter(status=bunch_status)
    return queryset


class CashBunchListCreateAPI(APIView):
    """GET the bunches · POST to bundle loose entries and send them up."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request):
        return Response(
            CashBunchSerializer(_bunch_queryset(request), many=True).data
        )

    def post(self, request):
        serializer = SendForApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.send_for_approval(
            user=request.user,
            company=_company(request),
            entry_ids=serializer.validated_data["entry_ids"],
            remarks=serializer.validated_data.get("remarks", ""),
        )
        return Response(
            CashBunchDetailSerializer(bunch).data, status=status.HTTP_201_CREATED
        )


class CashBunchDetailAPI(APIView):
    """GET one bunch and every line in it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request, pk):
        return Response(CashBunchDetailSerializer(_bunch(request, pk)).data)


def _bunch(request, pk) -> CashBunch:
    return get_object_or_404(
        CashBunch.objects.select_related("sent_by", "decided_by").prefetch_related(
            "entries__branch", "entries__bunch", "entries__created_by"
        ),
        pk=pk,
        company=_company(request),
    )


class CashBunchApproveAPI(APIView):
    """POST to approve. The decision time is the sheet's 'Sign Date'."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveCashBunch]

    def post(self, request, pk):
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.approve_bunch(
            user=request.user,
            bunch=_bunch(request, pk),
            note=serializer.validated_data.get("note", ""),
        )
        return Response(CashBunchDetailSerializer(bunch).data)


class CashBunchRejectAPI(APIView):
    """POST to send a bunch back. Its entries unfreeze so they can be fixed."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveCashBunch]

    def post(self, request, pk):
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.reject_bunch(
            user=request.user,
            bunch=_bunch(request, pk),
            note=serializer.validated_data.get("note", ""),
        )
        return Response(CashBunchDetailSerializer(bunch).data)


class CashBunchResendAPI(APIView):
    """POST to send a corrected bunch back up, under its own number."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def post(self, request, pk):
        serializer = ResendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.resend_bunch(
            user=request.user,
            bunch=_bunch(request, pk),
            remarks=serializer.validated_data.get("remarks"),
        )
        return Response(CashBunchDetailSerializer(bunch).data)


class CashBranchListCreateAPI(APIView):
    """GET the branches - POST to add one.

    The settings screen behind the entry form's Branch picker. Reading is open
    to anyone who can read the book, because the picker needs it; changing the
    list is its own right.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBranchPermission]

    def get(self, request):
        return Response(
            CashBranchSerializer(_branch_queryset(request), many=True).data
        )

    def post(self, request):
        serializer = CashBranchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company = _company(request)

        clash = CashBranch.objects.filter(
            company=company, name__iexact=data["name"]
        ).first()
        if clash is not None:
            # A retired branch of the same name is revived rather than
            # duplicated -- two branches called "Water" would split the
            # reports in half and nobody would know which to pick.
            if clash.is_active:
                return Response(
                    {"name": f"{clash.name} is already a branch."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            clash.is_active = True
            clash.updated_by = request.user
            clash.save(update_fields=["is_active", "updated_by", "updated_at"])
            return Response(CashBranchSerializer(clash).data)

        branch = CashBranch.objects.create(
            company=company,
            name=data["name"],
            sort_order=data.get("sort_order", 0),
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(
            CashBranchSerializer(branch).data, status=status.HTTP_201_CREATED
        )


class CashBranchDetailAPI(APIView):
    """PATCH to rename or reorder a branch - DELETE to retire it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBranchPermission]

    def _branch(self, request, pk) -> CashBranch:
        return get_object_or_404(CashBranch, pk=pk, company=_company(request))

    def patch(self, request, pk):
        branch = self._branch(request, pk)
        serializer = CashBranchWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "name" in data:
            clash = (
                CashBranch.objects.filter(
                    company=branch.company, name__iexact=data["name"]
                )
                .exclude(pk=branch.pk)
                .exists()
            )
            if clash:
                return Response(
                    {"name": f"{data['name']} is already a branch."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            branch.name = data["name"]
        if "sort_order" in data:
            branch.sort_order = data["sort_order"]
        if "is_active" in data:
            branch.is_active = data["is_active"]

        branch.updated_by = request.user
        branch.save()
        return Response(CashBranchSerializer(branch).data)

    put = patch

    def delete(self, request, pk):
        """Retire rather than delete: entries already filed under it keep it.

        The FK is PROTECT, so a real delete would be refused the moment the
        branch had ever been used. Retiring takes it out of the picker and
        leaves the register readable.
        """
        branch = self._branch(request, pk)
        if not branch.is_active:
            return Response(status=status.HTTP_204_NO_CONTENT)
        branch.is_active = False
        branch.updated_by = request.user
        branch.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# The card
# ----------------------------------------------------------------------


def _atm_account(request, pk) -> AtmAccount:
    return get_object_or_404(AtmAccount, pk=pk, company=_company(request))


def _with_balance(accounts):
    """Cards carry what is left on them, which is read rather than stored."""
    for account in accounts:
        account.balance = services.atm_balance(account)
    return accounts


class AtmAccountListCreateAPI(APIView):
    """GET the cards and what is on them - POST to add one."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request):
        queryset = AtmAccount.objects.filter(company=_company(request))
        if request.query_params.get("include_closed") != "true":
            queryset = queryset.filter(is_active=True)
        return Response(
            AtmAccountSerializer(_with_balance(list(queryset)), many=True).data
        )

    def post(self, request):
        serializer = AtmAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company = _company(request)

        if AtmAccount.objects.filter(
            company=company, name__iexact=data["name"]
        ).exists():
            return Response(
                {"name": f"{data['name']} is already a card."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        account = AtmAccount.objects.create(
            company=company,
            name=data["name"],
            opening_balance=data.get("opening_balance") or 0,
            created_by=request.user,
            updated_by=request.user,
        )
        account.balance = services.atm_balance(account)
        return Response(
            AtmAccountSerializer(account).data, status=status.HTTP_201_CREATED
        )


class AtmAccountDetailAPI(APIView):
    """GET one card's statement - PATCH to change it - DELETE to close it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request, pk):
        account = _atm_account(request, pk)
        account.balance = services.atm_balance(account)
        return Response(
            {
                "account": AtmAccountSerializer(account).data,
                "movements": MovementSerializer(
                    services.atm_statement(account), many=True
                ).data,
            }
        )

    def patch(self, request, pk):
        account = _atm_account(request, pk)
        serializer = AtmAccountWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "name" in data:
            clash = (
                AtmAccount.objects.filter(
                    company=account.company, name__iexact=data["name"]
                )
                .exclude(pk=account.pk)
                .exists()
            )
            if clash:
                return Response(
                    {"name": f"{data['name']} is already a card."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            account.name = data["name"]
        if "opening_balance" in data:
            account.opening_balance = data["opening_balance"]
        if "is_active" in data:
            account.is_active = data["is_active"]

        account.updated_by = request.user
        account.save()
        account.balance = services.atm_balance(account)
        return Response(AtmAccountSerializer(account).data)

    put = patch

    def delete(self, request, pk):
        """Close rather than delete: cash drawn off it still points at it."""
        account = _atm_account(request, pk)
        if account.is_active:
            account.is_active = False
            account.updated_by = request.user
            account.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class AtmReceiptCreateAPI(APIView):
    """POST money onto a card. The ATM screen's one write."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def post(self, request, pk):
        account = _atm_account(request, pk)
        serializer = RecordAtmReceiptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        receipt = services.record_atm_receipt(
            user=request.user,
            account=account,
            received_on=data["received_on"],
            amount=data["amount"],
            detail=data.get("detail", ""),
        )
        return Response(
            AtmReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED
        )


class AtmReceiptDetailAPI(APIView):
    """DELETE to take a payment back off a card, keeping the row."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def delete(self, request, pk):
        receipt = get_object_or_404(
            AtmReceipt, pk=pk, account__company=_company(request)
        )
        services.cancel_atm_receipt(user=request.user, receipt=receipt)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# Advances
# ----------------------------------------------------------------------


class AdvanceHolderListAPI(APIView):
    """GET everyone holding a float, and what they are still holding."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        rows = services.advance_holders(_company(request))
        return Response(
            {
                "holders": AdvanceHolderSerializer(rows, many=True).data,
                "total_outstanding": sum(
                    (row["balance"] for row in rows), Decimal("0.00")
                ),
            }
        )


class AdvanceEntryListCreateAPI(APIView):
    """GET the handouts and returns - POST to hand cash over or take it back."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request):
        queryset = AdvanceEntry.objects.filter(
            company=_company(request)
        ).select_related("person")
        person = _parse_positive_int(request.query_params.get("person"), None)
        if person:
            queryset = queryset.filter(person_id=person)
        if request.query_params.get("include_cancelled") != "true":
            queryset = queryset.filter(is_active=True)
        return Response(AdvanceEntrySerializer(queryset, many=True).data)

    def post(self, request):
        serializer = RecordAdvanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        entry = services.record_advance(
            user=request.user,
            company=_company(request),
            person=data["person"],
            entry_date=data["entry_date"],
            direction=data["direction"],
            amount=data["amount"],
            detail=data.get("detail", ""),
        )
        return Response(
            AdvanceEntrySerializer(entry).data, status=status.HTTP_201_CREATED
        )


class AdvanceEntryDetailAPI(APIView):
    """DELETE to take a handout or a return back out of the ledger."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def delete(self, request, pk):
        entry = get_object_or_404(AdvanceEntry, pk=pk, company=_company(request))
        services.cancel_advance(user=request.user, entry=entry)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdvanceStatementAPI(APIView):
    """GET one person's ledger: what they took, returned and explained."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request, pk):
        person = get_object_or_404(get_user_model(), pk=pk)
        company = _company(request)
        return Response(
            {
                "person": PersonSerializer(person).data,
                "balance": services.advance_balance(company, person),
                "movements": MovementSerializer(
                    services.advance_statement(company, person), many=True
                ).data,
            }
        )


class CashPeopleAPI(APIView):
    """GET who an advance may be given to.

    Everybody with a login to this company, because that is what an advance
    holder is here. Searchable, since the list is the whole staff directory
    rather than a short master.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        User = get_user_model()
        people = User.objects.filter(
            usercompany__company=_company(request), usercompany__is_active=True
        ).distinct()
        search = (request.query_params.get("search") or "").strip()
        if search:
            people = people.filter(
                Q(full_name__icontains=search) | Q(email__icontains=search)
            )
        return Response(PersonSerializer(people.order_by("full_name")[:100], many=True).data)
