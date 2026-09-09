"""Serializers for the SAP transfer-approval queue.

Reads are passed straight through from HANA (see
``sap_client.hana.transfer_approval_reader``), so only the decision body is
validated here.
"""
from rest_framework import serializers

DECISION_CHOICES = ("APPROVED", "REJECTED")


class SapApprovalDecisionSerializer(serializers.Serializer):
    """Validates the approve/reject body sent to PATCH .../status/."""

    status = serializers.ChoiceField(choices=DECISION_CHOICES)
    rejection_reason = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True
    )

    def validate(self, attrs):
        if attrs["status"] == "REJECTED" and not (
            attrs.get("rejection_reason") or ""
        ).strip():
            raise serializers.ValidationError(
                {"rejection_reason": "This field is required when status is REJECTED."}
            )
        if attrs["status"] == "APPROVED":
            attrs.pop("rejection_reason", None)
        return attrs
