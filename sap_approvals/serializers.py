"""Request bodies and query strings for the SAP approvals inbox.

Reads are passed through from HANA (``sap_client.hana.approval_inbox_reader``),
so only what the caller sends is validated here. ``sap_password`` is
write-only by construction: nothing here ever serializes it back, and it is
never trimmed — a password is exactly what was typed.
"""

from rest_framework import serializers

from sap_client.hana.approval_inbox_reader import SCOPES, STATUSES

from .constants import RejectionCategory

# SAP truncates a decision's remarks at 200 characters and the app appends who
# decided; keep the typed part short enough that the name always survives.
REMARKS_MAX = 150


class RequestFilterSerializer(serializers.Serializer):
    """``GET requests/`` query string."""

    scope = serializers.ChoiceField(choices=SCOPES, required=False, default="all")
    status = serializers.ChoiceField(
        choices=(*STATUSES, "ALL"), required=False, default="PENDING"
    )
    object_type = serializers.RegexField(
        r"^\d{1,20}$", required=False, allow_blank=True, default=""
    )
    date_from = serializers.DateField(required=False, allow_null=True, default=None)
    date_to = serializers.DateField(required=False, allow_null=True, default=None)
    search = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=100
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=200)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)

    def validate(self, attrs):
        if attrs["date_from"] and attrs["date_to"] and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "Must be on or after date_from."})
        return attrs

    def to_reader_filters(self) -> dict:
        data = self.validated_data
        return {
            "scope": data["scope"],
            "status": None if data["status"] == "ALL" else data["status"],
            "object_type": data["object_type"] or None,
            "date_from": data["date_from"],
            "date_to": data["date_to"],
            "search": data["search"],
            "limit": data["limit"],
            "offset": data["offset"],
        }


class _SignedActionSerializer(serializers.Serializer):
    # Optional: blank or absent falls back to the stored SAP_APPROVER_CREDENTIALS.
    sap_password = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        max_length=128,
        write_only=True,
        style={"input_type": "password"},
    )

    def typed_password(self) -> str | None:
        """The typed password, or None to use the stored one."""
        return self.validated_data.get("sap_password") or None


class DecisionSerializer(_SignedActionSerializer):
    """``POST requests/<wdd_code>/decision/``."""

    approve = serializers.BooleanField()
    remarks = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=REMARKS_MAX
    )
    confirm_duplicate = serializers.BooleanField(required=False, default=False)
    # Rejects only; kept here, not sent to SAP. Ignored on an approval.
    category = serializers.ChoiceField(
        choices=RejectionCategory.choices, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if not attrs["approve"] and not (attrs.get("remarks") or "").strip():
            raise serializers.ValidationError(
                {"remarks": "Say why this is being rejected; SAP records it."}
            )
        if not attrs["approve"] and not attrs.get("category"):
            raise serializers.ValidationError(
                {"category": "Pick what kind of entry this is; the rejection history counts by it."}
            )
        attrs["remarks"] = (attrs.get("remarks") or "").strip()
        if attrs["approve"]:
            attrs["category"] = ""
        return attrs


class WithdrawSerializer(_SignedActionSerializer):
    """``POST requests/<wdd_code>/withdraw/``."""


class RejectionFilterSerializer(serializers.Serializer):
    """``GET rejections/`` query string. Both dates default in the view."""

    date_from = serializers.DateField(required=False, allow_null=True, default=None)
    date_to = serializers.DateField(required=False, allow_null=True, default=None)
    originator = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=50
    )
    # Every SAP company the caller belongs to, instead of the one in the header.
    all_companies = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["date_from"] and attrs["date_to"] and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "Must be on or after date_from."})
        attrs["originator"] = attrs["originator"].strip().upper()
        return attrs
