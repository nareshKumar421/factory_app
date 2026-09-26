"""
The internal Partner Onboarding API: the approvals queue for each kind.

Every view: login, the ``Company-Code`` context, then its kind's right, and
only the header company's registrations (another company's is a 404). The same
classes serve customers and vendors; ``urls.py`` sets ``family`` on each.

SAP errors map the way every JI SAP endpoint maps them — SAP's refusal → 400
with SAP's own words, SAP unreachable → 503, SAP broken → 502 — and workflow
refusals carry a ``code`` the screen can act on (``possible_duplicate``, 409,
comes with the ``matches``).
"""

import logging

from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .constants import SAFE_CONTENT_TYPES, RegistrationStatus
from .families import FAMILIES, VENDOR_FAMILY
from .models import RegistrationAttachment
from .permissions import (
    CanApproveRegistrations,
    CanRejectRegistrations,
    CanVerifyRegistrations,
    CanViewRegistrations,
)
from .serializers import (
    ApproveSerializer,
    CustomerEditSerializer,
    RegistrationDetailSerializer,
    RegistrationListSerializer,
    RejectSerializer,
    VendorEditSerializer,
    VerifySerializer,
)
from .services import workflow

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class _RegistrationView(APIView):
    family = ""

    @property
    def kind(self):
        return FAMILIES[self.family]

    def handle_exception(self, exc):
        if isinstance(exc, workflow.WorkflowError):
            return Response(exc.body(), status=exc.status_code)
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc), "code": "sap_refused"}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {
                    "detail": "SAP is not reachable right now. Nothing was created; try again shortly.",
                    "code": "sap_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in %s: %s", type(self).__name__, exc)
            return Response({"detail": str(exc), "code": "sap_error"}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company

    def rows(self):
        return self.kind.model.objects.filter(company=self.company).select_related("company")

    def detail(self, pk, **extra):
        prefetch = ["addresses", "attachments", "events"]
        if self.kind is VENDOR_FAMILY:
            prefetch.append("bank_accounts")
        registration = get_object_or_404(
            self.rows().select_related("verified_by", "approved_by", "rejected_by").prefetch_related(*prefetch),
            pk=pk,
        )
        data = RegistrationDetailSerializer(registration, context={"request": self.request}).data
        data.update(extra)
        return Response(data)


class RegistrationListAPI(_RegistrationView):
    """GET — this company's registrations of one kind, newest first.

    ``?status=PENDING`` (or several, comma-separated; blank or ``ALL`` for every
    one), ``?search=`` (name, GSTIN, PAN, email, mobile, card code, contact, or a
    reference like REG-00012), ``?limit`` (≤ 500), ``?offset``. ``counts`` are
    per status for the same search, for the tabs.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewRegistrations]

    def get(self, request):
        rows = self.rows()
        search = (request.query_params.get("search") or "").strip()
        if search:
            match = (
                Q(card_name__icontains=search)
                | Q(foreign_name__icontains=search)
                | Q(gstin__icontains=search)
                | Q(pan__icontains=search)
                | Q(email__icontains=search)
                | Q(mobile__icontains=search)
                | Q(card_code__icontains=search)
                | Q(sap_card_code__icontains=search)
                | Q(contact_first_name__icontains=search)
                | Q(contact_last_name__icontains=search)
            )
            number = search.upper().removeprefix(f"{self.kind.model.reference_prefix}-").lstrip("0")
            if number.isdigit() and len(number) <= 9:  # a reference, not a phone number
                match |= Q(pk=int(number)) | Q(legacy_portal_id=int(number))
            rows = rows.filter(match)

        counts = {value: 0 for value, _ in RegistrationStatus.choices}
        for row in rows.values("status").annotate(n=Count("id")):
            counts[row["status"]] = row["n"]

        wanted = [
            value.strip().upper()
            for value in (request.query_params.get("status") or "").split(",")
            if value.strip() and value.strip().upper() != "ALL"
        ]
        unknown = set(wanted) - set(counts)
        if unknown:
            return Response(
                {"detail": f"Unknown status: {', '.join(sorted(unknown))}."}, status=status.HTTP_400_BAD_REQUEST
            )
        if wanted:
            rows = rows.filter(status__in=wanted)

        try:
            limit = min(max(int(request.query_params.get("limit", DEFAULT_LIMIT)), 1), MAX_LIMIT)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except ValueError:
            return Response({"detail": "limit and offset must be numbers."}, status=status.HTTP_400_BAD_REQUEST)

        total = rows.count()
        page = (
            rows.annotate(attachment_count=Count("attachments", distinct=True))
            .prefetch_related("addresses")
            .order_by("-submitted_at", "-id")[offset: offset + limit]
        )
        return Response(
            {
                "count": total,
                "counts": counts,
                "results": RegistrationListSerializer(page, many=True).data,
            }
        )


class RegistrationDetailAPI(_RegistrationView):
    """GET — one registration with its addresses, documents and history.
    PATCH — correct an open one (verify right); lists sent replace what is there."""

    edit_serializers = {"customer": CustomerEditSerializer, "vendor": VendorEditSerializer}

    def get_permissions(self):
        right = CanViewRegistrations if self.request.method == "GET" else CanVerifyRegistrations
        return [IsAuthenticated(), HasCompanyContext(), right()]

    def get(self, request, pk):
        return self.detail(pk)

    def patch(self, request, pk):
        current = get_object_or_404(self.rows(), pk=pk)
        serializer = self.edit_serializers[self.family](
            data=request.data, partial=True, context={"instance": current, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        workflow.edit(self.kind, pk, self.company, request.user, serializer.validated_data)
        return self.detail(pk)


class RegistrationVerifyAPI(_RegistrationView):
    """POST {note?} — a pending registration is checked and correct."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyRegistrations]

    def post(self, request, pk):
        serializer = VerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow.verify(self.kind, pk, self.company, request.user, serializer.validated_data["note"])
        return self.detail(pk)


class RegistrationRejectAPI(_RegistrationView):
    """POST {reason} — turn down a pending or verified registration."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanRejectRegistrations]

    def post(self, request, pk):
        serializer = RejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow.reject(self.kind, pk, self.company, request.user, serializer.validated_data["reason"])
        return self.detail(pk)


class RegistrationApproveAPI(_RegistrationView):
    """POST {SAP fields…, bank_accounts: [{id, sap_bank_code}], confirm_duplicate?}
    — create the partner in the registration's company's SAP.

    409 ``possible_duplicate`` lists SAP's partners with the same GSTIN or PAN;
    send again with ``confirm_duplicate: true`` to create anyway.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveRegistrations]

    def post(self, request, pk):
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        bank_codes = data.pop("bank_accounts", [])
        confirm = data.pop("confirm_duplicate", False)
        registration, warnings = workflow.approve(
            self.kind, pk, self.company, request.user, data, bank_codes, confirm_duplicate=confirm
        )
        return self.detail(
            pk,
            warnings=warnings,
            message=f"{registration.card_name} is in SAP as {registration.sap_card_code}.",
        )


class RegistrationAttachmentAPI(_RegistrationView):
    """GET — one document, streamed through this permission check.

    Never a ``/media/`` link: that path is not access-controlled (see
    ``company_vehicle.views.FleetAttachmentAPI``). PDFs and images open inline;
    anything else (only an import can bring one) downloads as bytes.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewRegistrations]

    def get(self, request, pk, attachment_id):
        registration = get_object_or_404(self.rows(), pk=pk)
        attachment = get_object_or_404(RegistrationAttachment, pk=attachment_id, **{self.family: registration})
        if not attachment.file:
            raise Http404("Nothing filed here.")
        try:
            handle = attachment.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The file is missing from storage.") from exc
        safe = attachment.content_type in SAFE_CONTENT_TYPES
        response = FileResponse(
            handle,
            as_attachment=not safe,
            filename=attachment.original_name,
            content_type=attachment.content_type if safe else "application/octet-stream",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response
