"""Reusable DRF mixin exposing a record's controlled-document identity.

Add :class:`ControlledDocumentSerializerMixin` to any attachment serializer and
include ``*ControlledDocumentSerializerMixin.DOCUMENT_FIELDS`` in ``Meta.fields``
so GATE / QC / GRPO all surface the code / revision / issue-date identically.
All three fields are null-safe on legacy rows (they read model properties that
return "" when no code is present).
"""

from rest_framework import serializers


class ControlledDocumentSerializerMixin(serializers.Serializer):
    document_code = serializers.CharField(source="document_code_str", read_only=True)
    document_revision = serializers.CharField(source="revision_label", read_only=True)
    document_issue_date = serializers.CharField(
        source="issue_date_display", read_only=True
    )

    #: Spread into a serializer's ``Meta.fields`` list.
    DOCUMENT_FIELDS = ("document_code", "document_revision", "document_issue_date")
