"""Serializers for the SAP-identity admin page."""
from rest_framework import serializers

from .models import SapApproverIdentity


class SapApproverIdentitySerializer(serializers.ModelSerializer):
    """One app user ↔ SAP account link, with the labels the page renders."""

    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source="user.email", read_only=True)
    user_code = serializers.CharField(source="user.employee_code", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    # Whether the app can actually authenticate as this SAP account. The link
    # says who may decide; this says whether the decision can be signed.
    password_configured = serializers.BooleanField(read_only=True)

    class Meta:
        model = SapApproverIdentity
        fields = [
            "id",
            "user",
            "user_name",
            "user_email",
            "user_code",
            "company_code",
            "sap_user_code",
            "sap_user_name",
            "password_configured",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "company_code", "created_at"]

    def get_user_name(self, obj):
        return getattr(obj.user, "full_name", "") or obj.user.get_username()


class SapApproverIdentityWriteSerializer(serializers.Serializer):
    """Validates a create/update from the admin page.

    ``company`` is never accepted from the body — it comes from the request's
    company context, so an administrator cannot map an identity into a company
    they are not acting in.
    """

    user = serializers.IntegerField()
    sap_user_code = serializers.CharField(max_length=50)
    sap_user_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_sap_user_code(self, value):
        code = (value or "").strip().upper()
        if not code:
            raise serializers.ValidationError("A SAP user code is required.")
        return code
