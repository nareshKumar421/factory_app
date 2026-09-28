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


# SAP truncates a decision's remarks at 200 characters and the app appends who
# decided; keep the typed part short enough that the name always survives
# (the same limit as sap_approvals.serializers.REMARKS_MAX).
APPROVAL_COMMENT_MAX = 150


class _TypedPasswordMixin(serializers.Serializer):
    """An optional SAP password typed for this one call (SAP Portal's way).

    Blank or absent falls back to the stored ``SAP_APPROVER_CREDENTIALS``
    entry. Never trimmed, never rendered back: a password is exactly what was
    typed, and SAP is what checks it.
    """

    sap_password = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        max_length=128,
        write_only=True,
        style={"input_type": "password"},
    )

    def typed_password(self) -> str | None:
        return self.validated_data.get("sap_password") or None


class CreditNoteDecisionSerializer(_TypedPasswordMixin, SapApprovalDecisionSerializer):
    """The credit-note queue's decision body: the shared one, plus what SAP
    Portal's credit-note screen offered.

    * ``without_qty_posting`` is optional and only read on an approval: absent
      or null leaves the draft's lines exactly as SAP holds them; ``true``
      credits the value only and moves no stock, ``false`` makes every item
      line move stock.
    * ``sap_password``: the approver's own SAP password for this decision.
    * ``approval_comment``: an optional note carried into SAP's remarks.
    * ``confirm_duplicate``: approve although SAP already holds a posted credit
      note for the same party and amount (the portal's explicit override).
    """

    without_qty_posting = serializers.BooleanField(required=False, allow_null=True, default=None)
    approval_comment = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=APPROVAL_COMMENT_MAX
    )
    confirm_duplicate = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["status"] != "APPROVED":
            # Nothing to write on a rejection: the draft is not going to post.
            attrs["without_qty_posting"] = None
            attrs["approval_comment"] = ""
            attrs["confirm_duplicate"] = False
        return attrs


class CreditNoteWithdrawSerializer(_TypedPasswordMixin):
    """``POST credit-note-approvals/<wdd_code>/withdraw/`` — the originator's
    own SAP password, optional when one is stored."""


class CreditNoteListFilterSerializer(serializers.Serializer):
    """The credit-note list's query string (SAP Portal's filters, all optional).

    ``party`` matches part of the card code or name, ``doc_num`` part of the
    draft's number, ``code`` the approval request exactly; ``date_from`` /
    ``date_to`` bound the day the request was raised. ``limit`` is clamped to
    500 (each row costs a read of the draft's lines) and ``offset`` pages on.
    """

    status = serializers.ChoiceField(choices=("PENDING", "APPROVED", "REJECTED", "ALL"), default="PENDING")
    party = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    doc_num = serializers.RegexField(r"^\d*$", required=False, allow_blank=True, default="", max_length=20)
    code = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1)
    date_from = serializers.DateField(required=False, allow_null=True, default=None)
    date_to = serializers.DateField(required=False, allow_null=True, default=None)
    limit = serializers.IntegerField(required=False, default=100, min_value=1, max_value=500)
    offset = serializers.IntegerField(required=False, default=0, min_value=0)

    def validate(self, attrs):
        if attrs["date_from"] and attrs["date_to"] and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "The To date is before the From date."})
        return attrs

    def to_reader_filters(self) -> dict:
        data = self.validated_data
        return {
            "party": data["party"],
            "doc_num": data["doc_num"],
            "code": data["code"],
            "date_from": data["date_from"],
            "date_to": data["date_to"],
            "offset": data["offset"],
        }
