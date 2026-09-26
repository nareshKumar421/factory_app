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


class CreditNoteDecisionSerializer(SapApprovalDecisionSerializer):
    """The credit-note queue's decision body: the shared one, plus SAP's
    "Without Qty Posting" (ported from SAP Portal's credit-note screen).

    ``without_qty_posting`` is optional and only read on an approval: absent or
    null leaves the draft's lines exactly as SAP holds them (the queue's
    behaviour before this field existed); ``true`` credits the value only and
    moves no stock, ``false`` makes every item line move stock.
    """

    without_qty_posting = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["status"] != "APPROVED":
            # Nothing to write on a rejection: the draft is not going to post.
            attrs["without_qty_posting"] = None
        return attrs
