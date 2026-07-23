"""Serializers for the OMS invoice-approval proxy.

Reads are external OMS JSON passed through unchanged, so there are no output
serializers for invoices/history. We only validate input (the PATCH body and the
``?status=`` query param) and serialize our own local audit rows.
"""
from rest_framework import serializers

from .client import VALID_STATUSES
from .models import InvoiceApprovalAudit

DECISION_CHOICES = ("APPROVED", "REJECTED")


class InvoiceStatusUpdateSerializer(serializers.Serializer):
    """Validates the approve/reject body sent to PATCH .../status/.

    ``so_number``/``party_name``/``total_amount`` are optional display context the
    frontend already has; they are stored on the local audit row so we don't need an
    extra OMS round-trip just to label it.
    """

    status = serializers.ChoiceField(choices=DECISION_CHOICES)
    rejection_reason = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True
    )
    so_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    party_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    total_amount = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=2
    )

    def validate(self, attrs):
        if attrs["status"] == "REJECTED" and not (attrs.get("rejection_reason") or "").strip():
            raise serializers.ValidationError(
                {"rejection_reason": "This field is required when status is REJECTED."}
            )
        if attrs["status"] == "APPROVED":
            attrs.pop("rejection_reason", None)
        return attrs


class InvoiceStatusQuerySerializer(serializers.Serializer):
    """Validates the optional ``?status=`` filter on the list endpoint."""

    status = serializers.ChoiceField(choices=sorted(VALID_STATUSES), required=False)


class InvoiceApprovalAuditSerializer(serializers.ModelSerializer):
    acted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceApprovalAudit
        fields = [
            "id",
            "invoice_log_id",
            "so_number",
            "party_name",
            "total_amount",
            "decision",
            "rejection_reason",
            "oms_message",
            "company",
            "acted_by_name",
            "created_at",
        ]

    def get_acted_by_name(self, obj):
        user = obj.created_by
        if not user:
            return None
        return getattr(user, "full_name", "") or user.get_username()
