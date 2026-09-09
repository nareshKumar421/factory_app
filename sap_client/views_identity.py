"""API for the SAP-identity admin page — who each app user is inside SAP.

The mapping decides who may take a SAP approval decision in this app: SAP
accepts a decision only from the authorizer its template names, so the app only
offers Approve to the person whose own SAP account *is* that authorizer.

Passwords are not handled here. They live in the ``SAP_APPROVER_CREDENTIALS``
env map and this API only reports whether one is present, so the page can show
what is still missing without ever carrying a secret to the browser.
"""

import logging

from django.conf import settings
from django.db import IntegrityError
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .client import SAPClient
from .exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .models import SapApproverIdentity
from .serializers_identity import (
    SapApproverIdentitySerializer,
    SapApproverIdentityWriteSerializer,
)

logger = logging.getLogger(__name__)


class CanManageSapIdentities(BasePermission):
    message = "You do not have permission to manage SAP user mappings."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.has_perm("sap_client.can_manage_sap_identities")
        )


class _IdentityView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageSapIdentities]

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SAPConnectionError, SAPDataError)):
            logger.error("SAP error on the SAP-identity page: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company

    def queryset(self):
        return SapApproverIdentity.objects.filter(
            company=self.company
        ).select_related("user", "company")


class SapApproverIdentityListCreateView(_IdentityView):
    """GET/POST /api/v1/sap-identity/identities/ — the mappings for this company."""

    def get(self, request):
        return Response(SapApproverIdentitySerializer(self.queryset(), many=True).data)

    def post(self, request):
        serializer = SapApproverIdentityWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            identity = SapApproverIdentity.objects.create(
                user_id=data["user"],
                company=self.company,
                sap_user_code=data["sap_user_code"],
                sap_user_name=data.get("sap_user_name", ""),
                is_active=data.get("is_active", True),
                created_by=request.user,
            )
        except IntegrityError:
            # Both unique constraints are real rules, so name which one broke
            # rather than leaving the admin to guess.
            return Response(
                {"error": self._conflict(data)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            SapApproverIdentitySerializer(identity).data, status=status.HTTP_201_CREATED
        )

    def _conflict(self, data) -> str:
        existing = self.queryset().filter(user_id=data["user"]).first()
        if existing:
            return (
                f"That user is already mapped to SAP user {existing.sap_user_code} "
                f"in {self.company.code}. Edit that mapping instead."
            )
        taken = self.queryset().filter(sap_user_code=data["sap_user_code"]).first()
        if taken:
            name = getattr(taken.user, "full_name", "") or taken.user.get_username()
            return (
                f"SAP user {data['sap_user_code']} is already mapped to {name}. "
                "One SAP account belongs to one person, or the audit trail cannot "
                "say which of them decided."
            )
        return "That mapping conflicts with an existing one."


class SapApproverIdentityDetailView(_IdentityView):
    """PATCH/DELETE /api/v1/sap-identity/identities/<pk>/."""

    def patch(self, request, pk):
        identity = self.queryset().filter(pk=pk).first()
        if identity is None:
            return Response(
                {"error": "That mapping was not found."}, status=status.HTTP_404_NOT_FOUND
            )
        serializer = SapApproverIdentityWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "sap_user_code" in data:
            identity.sap_user_code = data["sap_user_code"]
        if "sap_user_name" in data:
            identity.sap_user_name = data["sap_user_name"]
        if "is_active" in data:
            identity.is_active = data["is_active"]
        identity.updated_by = request.user
        try:
            identity.save()
        except IntegrityError:
            return Response(
                {
                    "error": (
                        f"SAP user {identity.sap_user_code} is already mapped to "
                        "somebody else in this company."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(SapApproverIdentitySerializer(identity).data)

    def delete(self, request, pk):
        identity = self.queryset().filter(pk=pk).first()
        if identity is None:
            return Response(
                {"error": "That mapping was not found."}, status=status.HTTP_404_NOT_FOUND
            )
        identity.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SapUserListView(_IdentityView):
    """GET /api/v1/sap-identity/sap-users/ — the SAP accounts to pick from.

    Sourced from ``OUSR`` so a code cannot be mistyped into a mapping that
    silently never matches an authorizer. Each row also reports how many active
    approval templates name it, whether its password is configured, and who it
    is already mapped to — which together make this the worklist for collecting
    the remaining passwords.
    """

    def get(self, request):
        users = SAPClient(company_code=self.company.code).list_sap_users(
            include_locked=request.query_params.get("include_locked") == "1"
        )
        credentials = settings.SAP_APPROVER_CREDENTIALS.get(self.company.code) or {}
        mapped = {}
        for identity in self.queryset():
            name = (
                getattr(identity.user, "full_name", "") or identity.user.get_username()
            )
            mapped[identity.sap_user_code] = {
                "identity_id": identity.id,
                "user_id": identity.user_id,
                "user_name": name,
                "is_active": identity.is_active,
            }
        for row in users:
            code = row["user_code"].upper()
            row["password_configured"] = bool(credentials.get(code))
            row["mapped_to"] = mapped.get(code)
        return Response(users)


class MySapIdentityView(APIView):
    """GET /api/v1/sap-identity/me/ — the caller's own SAP account.

    Needs no admin permission: any approver screen may ask what the app will
    sign as on their behalf, and a screen cannot correctly disable an action it
    is not allowed to ask about.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        company = request.company.company
        identity = (
            SapApproverIdentity.objects.filter(
                user=request.user, company=company, is_active=True
            )
            .select_related("company")
            .first()
        )
        if identity is None:
            return Response({"sap_user_code": None, "password_configured": False})
        return Response(
            {
                "sap_user_code": identity.sap_user_code,
                "sap_user_name": identity.sap_user_name,
                "password_configured": identity.password_configured,
            }
        )
