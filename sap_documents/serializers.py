from rest_framework import serializers

from .constants import STATUS_LABELS


class DocumentFilterSerializer(serializers.Serializer):
    """Query parameters of a document list, as SAP Portal's documents page sent
    them (``q``, ``bp``, ``dateFrom``, ``dateTo``, ``status``, ``top``, ``skip``),
    under this app's names. All optional."""

    number = serializers.IntegerField(required=False, min_value=0)
    partner = serializers.CharField(required=False, allow_blank=True, max_length=100, trim_whitespace=True)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    status = serializers.ChoiceField(required=False, allow_blank=True, choices=sorted(STATUS_LABELS))
    top = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)
    skip = serializers.IntegerField(required=False, min_value=0, max_value=100000, default=0)

    def __init__(self, *args, document_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.document_type = document_type

    def validate(self, attrs):
        if attrs.get("date_from") and attrs.get("date_to") and attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "The end date is before the start date."})
        doc_type = self.document_type
        if doc_type is not None:
            status = attrs.get("status") or ""
            if status and status not in doc_type.statuses:
                raise serializers.ValidationError(
                    {"status": f"{doc_type.label} cannot be filtered by status {STATUS_LABELS[status]}."}
                )
            if (attrs.get("partner") or "") and not doc_type.has_partner:
                raise serializers.ValidationError({"partner": f"{doc_type.label} has no business partner."})
        return attrs
