"""
The cash book's API.

Two screens sit on it. The register (``entries/``) is the book itself -- every
line, with its running balance, filtered by date, direction, department, G/L
head or free text. The approvals screen (``bunches/``) is the other half of the
sheet's Bunch column: sets of vouchers walked to an approver together.

Everything is scoped to the company on the request header. Each company keeps
its own cash box, so a balance only means anything read against one.
"""

import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Department
from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .constants import DEFAULT_PAGE_SIZE, GL_ACCOUNT_SEARCH_LIMIT, MAX_PAGE_SIZE
from .hana_reader import GLAccountReader
from .models import (
    BunchStatus,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from .permissions import (
    CanApproveCashBunch,
    CanManageCashBook,
    CanViewCashBook,
    CashBookPermission,
)
from .serializers import (
    CashBunchDetailSerializer,
    CashBunchSerializer,
    CashEntrySerializer,
    DecisionSerializer,
    DepartmentOptionSerializer,
    GLAccountSerializer,
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
        .select_related("department", "bunch", "created_by")
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

    department = _parse_positive_int(params.get("department"), None)
    if department:
        queryset = queryset.filter(department_id=department)

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


class CashBookOptionsAPI(APIView):
    """GET what the entry form and the filters need to offer.

    Sent rather than hardcoded in the client so a new department or a changed
    right shows up without a release. The two ``can_*`` flags are what the page
    hides its buttons behind -- the endpoints enforce the same rights anyway.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCashBook]

    def get(self, request):
        company = _company(request)
        return Response(
            {
                "departments": DepartmentOptionSerializer(
                    Department.objects.order_by("name"), many=True
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
            department=data.get("department"),
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
            CashEntry.objects.select_related("department", "bunch", "created_by"),
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
            "entries__department", "entries__bunch", "entries__created_by"
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
