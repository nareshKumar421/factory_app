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
from django.db.models import BooleanField, Case, Count, Q, Value, When
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import bunch_export, services
from .constants import DEFAULT_PAGE_SIZE, GL_ACCOUNT_SEARCH_LIMIT, MAX_PAGE_SIZE
from .hana_reader import GLAccountReader
from .models import (
    CashEntryAttachment,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from .permissions import (
    CanApproveCashEntries,
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
    CreateBunchSerializer,
    CashEntrySerializer,
    EntryIdsSerializer,
    GLAccountSerializer,
    MarkSentSerializer,
    CashEntryAttachmentSerializer,
    MovementSerializer,
    NewPersonSerializer,
    SetApproverSerializer,
    PersonSerializer,
    RecordAdvanceSerializer,
    RecordAtmReceiptSerializer,
    RecordEntrySerializer,
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


#: The register's columns: what each is called on the wire, the field behind
#: it, and how it is sorted.
#:
#: One table drives three things -- the value list a column offers, the filter
#: it applies, and the order it sorts in -- so a column cannot end up
#: filterable but not sortable, or offering values it then cannot match.
ENTRY_COLUMNS = {
    "date": {"field": "entry_date", "sort": ["entry_date", "id"]},
    "bunch": {"field": "bunch__number", "sort": ["bunch__number", "id"]},
    "branch": {"field": "branch__name", "sort": ["branch__name", "id"]},
    "gl": {"field": "gl_account_name", "sort": ["gl_account_code", "id"]},
    "source": {"field": "atm_account__name", "sort": ["atm_account__name", "id"]},
    "advance": {
        "field": "advance_holder__full_name",
        "sort": ["advance_holder__full_name", "id"],
    },
    "item": {"field": "item", "sort": ["item", "id"]},
    "detail": {"field": "detail", "sort": ["detail", "id"]},
    "direction": {"field": "direction", "sort": ["direction", "id"]},
    # Amount and In are one field read two ways. The register writes a payment
    # in the Amount column and a receipt in the In column, so each is blank on
    # the other's rows -- which is how the paper does it, and it has to be how
    # the filters do it too. Without ``blank_when``, ticking 1,410 under Amount
    # would also bring back a RECEIPT of 1,410 whose Amount cell is empty.
    "amount": {
        "field": "amount",
        "sort": ["amount", "id"],
        "blank_when": Q(direction=CashDirection.IN),
    },
    "in": {
        "field": "amount",
        "sort": ["amount", "id"],
        "blank_when": Q(direction=CashDirection.OUT),
    },
    "balance": {"field": "balance_after", "sort": ["balance_after", "id"]},
    "approval": {"field": "approval_state", "sort": ["approval_state", "id"]},
}

#: Every sort ends in ``id`` so the order is total: two entries of the same
#: value would otherwise swap places between pages and drop a row off the end.
ENTRY_SORTS = {name: spec["sort"] for name, spec in ENTRY_COLUMNS.items()}
ENTRY_SORTS["recorded"] = ["id"]

DEFAULT_ENTRY_SORT = "-recorded"

#: How a column filter arrives: ``?f_branch=Oil|Common``. A pipe, because a
#: comma appears inside G/L names and a detail line is full of them.
COLUMN_FILTER_PREFIX = "f_"
COLUMN_FILTER_SEPARATOR = "|"

#: Stands for a row whose column is empty, so "no branch" can be ticked like
#: any other value rather than being unfilterable.
BLANK_VALUE = "\u2014"

#: A value list is a picker, not a report. Past this many distinct values the
#: list is cut and the client is told, so a column of 400 different amounts
#: does not arrive as 400 checkboxes nobody will scroll.
MAX_COLUMN_VALUES = 300


def _ordering(sort, allowed, default):
    """Turn ``-date`` into the column list it means, or fall back.

    An unknown name is ignored rather than refused: a stale bookmark should
    show the register, not an error.
    """
    raw = (sort or "").strip() or default
    descending = raw.startswith("-")
    key = raw.lstrip("-")
    if key not in allowed:
        raw = default
        descending = raw.startswith("-")
        key = raw.lstrip("-")
    return [f"-{column}" if descending else column for column in allowed[key]]


def _apply_column_filters(queryset, params, *, skip=None):
    """Narrow by whatever each column's filter has ticked.

    ``skip`` leaves one column out, which is what lets that column's own value
    list still show the options it is hiding -- exactly as a spreadsheet does,
    where opening a filter you have already used still offers everything.
    """
    for name, spec in ENTRY_COLUMNS.items():
        if name == skip:
            continue
        raw = params.get(f"{COLUMN_FILTER_PREFIX}{name}")
        if not raw:
            continue
        chosen = [v for v in raw.split(COLUMN_FILTER_SEPARATOR) if v != ""]
        if not chosen:
            continue

        field = spec["field"]
        blank_when = spec.get("blank_when")
        wanted = [v for v in chosen if v != BLANK_VALUE]

        condition = Q()
        if wanted:
            match = Q(**{f"{field}__in": wanted})
            if blank_when is not None:
                # The column is blank on these rows, so its value cannot be
                # ticked there however much the underlying field matches.
                match &= ~blank_when
            condition = match
        if BLANK_VALUE in chosen:
            if blank_when is not None:
                condition |= blank_when
            else:
                # A blank is a null or an empty string depending on the
                # column; both read as "nothing there" and both should tick.
                condition |= Q(**{f"{field}__isnull": True}) | Q(**{field: ""})
        queryset = queryset.filter(condition)
    return queryset


def _entry_queryset(request, *, skip_column=None):
    """The register, narrowed and ordered by whatever the columns are set to.

    ``include_cancelled`` is off by default: a cancelled line is out of the
    book, and somebody reading the balance should not have to subtract it back
    out by eye.

    Sorting is the server's because the register is paged -- ordering one page
    of fifty would only shuffle the rows that happened to be on it.
    """
    params = request.query_params
    queryset = (
        CashEntry.objects.filter(company=_company(request))
        .select_related(
            "branch", "bunch", "created_by", "atm_account", "advance_holder"
        )
        .order_by(*_ordering(params.get("sort"), ENTRY_SORTS, DEFAULT_ENTRY_SORT))
    )

    if params.get("include_cancelled") != "true":
        queryset = queryset.filter(is_active=True)

    return _apply_column_filters(queryset, params, skip=skip_column)


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
                "approval_statuses": [
                    {"value": value, "label": label}
                    for value, label in EntryApprovalStatus.choices
                ],
                "balance": services.current_balance(company),
                "gl_account_search_limit": GL_ACCOUNT_SEARCH_LIMIT,
                "entry_columns": sorted(ENTRY_COLUMNS),
                "can_manage": CanManageCashBook().has_permission(request, self),
                "can_approve": CanApproveCashEntries().has_permission(request, self),
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
                # What the next voucher will be called, so the form can fill
                # it in without a second request.
                "next_serial": services.next_serial(_company(request)),
                "totals": services.totals(queryset),
                # The six figures the page heads itself with. Whole book, never
                # the filter -- a reconciliation of part of a book proves
                # nothing.
                "reconciliation": services.reconciliation(_company(request)),
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
            approver=data.get("approver"),
            serial_number=data.get("serial_number"),
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
                "reconciliation": services.reconciliation(company),
                "awaiting_approval": CashEntry.objects.filter(
                    company=company,
                    is_active=True,
                    approval_state=EntryApprovalStatus.PENDING,
                ).count(),
                # Sent back to be put right. Nothing is ever "not sent":
                # a payment is in the queue from the moment it is recorded.
                "rejected_entries": CashEntry.objects.filter(
                    company=company,
                    is_active=True,
                    approval_state=EntryApprovalStatus.REJECTED,
                ).count(),
            }
        )


def _bunch_queryset(request):
    queryset = (
        CashBunch.objects.filter(company=_company(request))
        .select_related("created_by", "sent_by")
        .prefetch_related("entries")
    )
    state = (request.query_params.get("state") or "").upper()
    if state == "SENT":
        queryset = queryset.filter(sent_at__isnull=False)
    elif state == "UNSENT":
        queryset = queryset.filter(sent_at__isnull=True)
    return queryset


def _bunch(request, pk) -> CashBunch:
    return get_object_or_404(
        CashBunch.objects.select_related("company", "created_by", "sent_by")
        .prefetch_related("entries__branch", "entries__created_by"),
        pk=pk,
        company=_company(request),
    )


class CashBunchListCreateAPI(APIView):
    """GET the batches - POST to bundle approved vouchers into a new one.

    A bunch is made from the register: filter it down, tick the approved
    payments that belong together, and the batch is the record of what was put
    in one envelope.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CashBookPermission]

    def get(self, request):
        return Response(CashBunchSerializer(_bunch_queryset(request), many=True).data)

    def post(self, request):
        serializer = CreateBunchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.create_bunch(
            user=request.user,
            company=_company(request),
            entry_ids=serializer.validated_data["entry_ids"],
            remarks=serializer.validated_data.get("remarks", ""),
        )
        return Response(
            CashBunchDetailSerializer(bunch).data, status=status.HTTP_201_CREATED
        )


class CashBunchDetailAPI(APIView):
    """GET one batch and every voucher in it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request, pk):
        return Response(CashBunchDetailSerializer(_bunch(request, pk)).data)


class CashBunchSentAPI(APIView):
    """POST to record that the batch went to head office, or that it did not.

    The app does not send the mail, so it cannot know on its own.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def post(self, request, pk):
        serializer = MarkSentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = services.mark_bunch_sent(
            user=request.user,
            bunch=_bunch(request, pk),
            sent=serializer.validated_data.get("sent", True),
        )
        return Response(CashBunchSerializer(bunch).data)


class CashBunchExportAPI(APIView):
    """GET the batch as the spreadsheet that gets mailed to head office.

    Built on demand rather than stored: the vouchers in a batch can still be
    corrected up to the moment it is sent, and a file saved at bundling time
    would quietly disagree with the register it came from.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request, pk):
        bunch = _bunch(request, pk)
        entries = sorted(
            (entry for entry in bunch.entries.all() if entry.is_active),
            key=lambda entry: entry.id,
        )
        try:
            content = bunch_export.build_bunch_workbook(bunch, entries)
        except ImportError:  # pragma: no cover - environment problem
            return Response(
                {"detail": "openpyxl is needed to build the spreadsheet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        response = HttpResponse(
            content,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        filename = bunch_export.bunch_filename(bunch)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class CashEntryBunchRemoveAPI(APIView):
    """DELETE to take one voucher back out of a batch that has not gone yet."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def delete(self, request, pk):
        entry = get_object_or_404(
            CashEntry.objects.select_related("bunch"), pk=pk, company=_company(request)
        )
        services.remove_from_bunch(user=request.user, entry=entry)
        return Response(status=status.HTTP_204_NO_CONTENT)


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
    """GET one person's ledger: what they took, returned and explained.

    ``?include_cancelled=true`` brings back the rows somebody has taken out,
    the way the register's own "Show cancelled" does. Off by default: a
    cancelled row is out of the account, and somebody reading a balance should
    not have to subtract it back out by eye.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request, pk):
        person = get_object_or_404(get_user_model(), pk=pk)
        company = _company(request)
        include_cancelled = (
            request.query_params.get("include_cancelled") == "true"
        )
        return Response(
            {
                "person": PersonSerializer(person).data,
                # Always the live balance, whatever the ledger is showing --
                # ticking a box to see history must not restate what they hold.
                "balance": services.advance_balance(company, person),
                "movements": MovementSerializer(
                    services.advance_statement(
                        company, person, include_cancelled=include_cancelled
                    ),
                    many=True,
                ).data,
            }
        )


class CashApproversAPI(APIView):
    """GET the people a payment may be sent to for approval.

    Not everybody the permission system would let approve: see
    ``services.approvers``. An empty list here is the honest answer when
    nobody has been made an approver yet, and the form says so rather than
    showing an empty box with no explanation.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        """The approvers, or everybody who could be one.

        ``?candidates=true`` returns the appointable people with an
        ``approves`` flag on each. One list drives the settings screen, so it
        cannot offer somebody the write side would refuse, and cannot show
        "nobody approves" about people it has just appointed.
        """
        company = _company(request)
        if request.query_params.get("candidates") == "true":
            # A search, not a listing: blank finds nobody. See
            # services.approver_candidates for why.
            found = services.approver_candidates(
                company, request.query_params.get("search", "")
            )[: services.MAX_CANDIDATES]
            approving = set(
                services.approvers(company).values_list("pk", flat=True)
            )
            rows = []
            for person in found:
                row = PersonSerializer(person).data
                row["approves"] = person.pk in approving
                rows.append(row)
            return Response(rows)

        return Response(PersonSerializer(services.approvers(company), many=True).data)

    def post(self, request):
        """Make somebody an approver of this company's cash, or stop them.

        Gated on the settings right rather than the custodian's: choosing who
        agrees to spending is an administrative act, not part of keeping the
        book. Appointing yourself is refused in the service, where every
        caller meets it.
        """
        if not CanManageCashBranches().has_permission(request, self):
            raise PermissionDenied(
                "Only a cash book administrator can change who approves."
            )
        serializer = SetApproverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        person = services.set_approver(
            user=request.user,
            company=_company(request),
            person=serializer.validated_data["person"],
            approving=serializer.validated_data["approving"],
        )
        return Response(PersonSerializer(person).data)


class CashPersonCreateAPI(APIView):
    """POST a name to add somebody who can hold the factory's cash.

    For the drivers and tradesmen the book deals with who have no login. The
    custodian meets them at the moment of handing cash over, which is where
    this is offered, rather than having to leave the form and find an
    administrator.

    A name that is already somebody comes back as that person rather than a
    second one, and the response says which happened. That matters more here
    than anywhere else the rule appears: matching only on an exact full name
    once produced ten duplicate people on the live book, each holding a float
    while the real account sat empty, and an "add" button offered to anybody
    typing a name will produce more of them faster than an import ever could.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def post(self, request):
        serializer = NewPersonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        person, created = services.create_cash_person(
            user=request.user, name=serializer.validated_data["name"]
        )
        return Response(
            {**PersonSerializer(person).data, "created": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CashPeopleAPI(APIView):
    """GET who may be picked as a person, which is two different questions.

    ``?holding=true`` narrows to people who have been given an advance, with
    what each is still holding. That is who a *return* can come from and whose
    advance a payment can clear -- offering the whole staff directory there
    invites somebody to settle a float against a person who never took one,
    which is silent and wrong.

    Without it, everybody a new advance may be given to: the company's own
    directory, **plus anybody the cash book already deals with**. The second
    half is not a refinement -- the sheet's people are drivers, tradesmen and
    contractors who have no login and no company membership, so a directory
    query alone cannot see them. Manoj could be holding 804.00 and still not
    be offerable for the next advance, which is how this was found.

    Searchable, because the first half is the whole staff directory rather
    than a short master.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        company = _company(request)
        search = (request.query_params.get("search") or "").strip()

        if request.query_params.get("holding") == "true":
            rows = services.advance_holders(company)
            people = []
            for row in rows:
                person = row["person"]
                # Carried on the instance so one serializer serves both shapes.
                person.balance = row["balance"]
                people.append(person)
            if search:
                needle = search.lower()
                people = [
                    person
                    for person in people
                    if needle in (person.full_name or "").lower()
                    or needle in person.email.lower()
                ]
            return Response(PersonSerializer(people, many=True).data)

        # Two ways of belonging here, and somebody needs only one of them.
        known = {row["person"].pk for row in services.advance_holders(company)}
        people = (
            get_user_model()
            .objects.filter(
                Q(usercompany__company=company, usercompany__is_active=True)
                | Q(pk__in=known)
            )
            .distinct()
        )
        if search:
            people = people.filter(
                Q(full_name__icontains=search) | Q(email__icontains=search)
            )
        return Response(PersonSerializer(people.order_by("full_name")[:100], many=True).data)


class CashEntryAttachmentAPI(APIView):
    """POST a bill against a line of the book.

    Multipart, and several files at once: a bill is often more than one sheet
    of paper. Each is checked on its own, and one bad file does not throw away
    the others -- the response says what landed and what did not, because a
    silent partial upload is how somebody ends up believing a voucher is on
    record when half of it is.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        entry = get_object_or_404(CashEntry, pk=pk, company=_company(request))
        uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not uploads:
            raise ValidationError({"files": "Pick a bill to attach."})

        attached, refused = [], []
        for upload in uploads:
            try:
                attached.append(
                    services.attach_to_entry(
                        user=request.user, entry=entry, upload=upload
                    )
                )
            except ValidationError as exc:
                refused.append(
                    {"filename": getattr(upload, "name", ""), "reason": exc.detail}
                )

        return Response(
            {
                "attached": CashEntryAttachmentSerializer(
                    attached, many=True, context={"request": request}
                ).data,
                "refused": refused,
            },
            status=(
                status.HTTP_201_CREATED if attached else status.HTTP_400_BAD_REQUEST
            ),
        )


class CashEntryAttachmentDetailAPI(APIView):
    """DELETE to take a bill off a line, and off the disk with it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageCashBook]

    def delete(self, request, pk):
        attachment = get_object_or_404(
            CashEntryAttachment, pk=pk, entry__company=_company(request)
        )
        services.remove_attachment(user=request.user, attachment=attachment)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CashEntryApprovalDecideAPI(APIView):
    """POST to approve or reject entries. ``?reject=true`` sends them back."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanApproveCashEntries,
    ]

    def post(self, request):
        serializer = EntryIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approve = request.query_params.get("reject") != "true"
        entries = services.decide_entries(
            user=request.user,
            company=_company(request),
            entry_ids=serializer.validated_data["entry_ids"],
            approve=approve,
            note=serializer.validated_data.get("note", ""),
        )
        return Response(CashEntrySerializer(entries, many=True).data)


class CashApprovalQueueAPI(APIView):
    """GET the entries waiting on somebody, newest first.

    Entries, not bunches: a bunch is a bundle of paper, and bundling vouchers
    is not a decision about them.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        state = (request.query_params.get("state") or "PENDING").upper()
        queryset = services.approval_queue(
            _company(request),
            request.user,
            state=state if state in EntryApprovalStatus.values else None,
        )
        rows = list(queryset[:500])
        return Response(
            {
                "state": state,
                "results": CashEntrySerializer(rows, many=True).data,
                "total": sum((entry.amount for entry in rows), Decimal("0.00")),
                "counts": {
                    value: CashEntry.objects.filter(
                        company=_company(request), is_active=True, approval_state=value
                    ).count()
                    for value in EntryApprovalStatus.values
                },
            }
        )


class CashEntryColumnValuesAPI(APIView):
    """GET the values one column of the register holds, with a count each.

    What a spreadsheet's filter button drops down. Two things make it behave
    the way people expect from one:

    * the list is drawn from the **whole** book, not the page on screen -- a
      register is paged, and a filter offering only what page one happened to
      contain would hide most of its own options;
    * the other columns' filters DO narrow it, but this column's own does not.
      So ticking two branches still leaves all four showing when you reopen
      that filter, while the G/L list beside it has already narrowed to what
      those two branches actually spent on.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        column = (request.query_params.get("column") or "").strip()
        if column not in ENTRY_COLUMNS:
            return Response(
                {
                    "detail": f"No such column: {column!r}. Known: "
                    f"{', '.join(sorted(ENTRY_COLUMNS))}."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        spec = ENTRY_COLUMNS[column]
        field = spec["field"]
        blank_when = spec.get("blank_when")

        queryset = _entry_queryset(request, skip_column=column)
        if blank_when is not None:
            queryset = queryset.annotate(
                reads_blank=Case(
                    When(blank_when, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                )
            )
            rows = (
                queryset.values(field, "reads_blank")
                .annotate(count=Count("id"))
                .order_by(field)
            )
        else:
            rows = queryset.values(field).annotate(count=Count("id")).order_by(field)

        # Grouped by value, so every row the column reads blank on lands in one
        # "(blank)" entry however many different amounts sit behind it.
        counted = {}
        for row in rows:
            raw = row[field]
            blank = row.get("reads_blank") or raw is None or raw == ""
            key = BLANK_VALUE if blank else str(raw)
            if key not in counted:
                counted[key] = {
                    "value": key,
                    "label": "(blank)" if blank else key,
                    "count": 0,
                }
            counted[key]["count"] += row["count"]
        values = list(counted.values())

        truncated = len(values) > MAX_COLUMN_VALUES
        return Response(
            {
                "column": column,
                "values": values[:MAX_COLUMN_VALUES],
                "truncated": truncated,
                "total": len(values),
            }
        )
