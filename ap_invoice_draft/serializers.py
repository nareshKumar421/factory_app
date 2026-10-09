from rest_framework import serializers

from .models import APInvoiceDraft


def _person(user):
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class OpenGRPOSerializer(serializers.Serializer):
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    doc_date = serializers.DateField(allow_null=True)
    reference = serializers.CharField(allow_blank=True)
    vendor_code = serializers.CharField(allow_blank=True)
    vendor_name = serializers.CharField(allow_blank=True)
    total = serializers.DecimalField(max_digits=18, decimal_places=2)
    comments = serializers.CharField(allow_blank=True)
    warehouses = serializers.ListField(child=serializers.CharField())
    # An open A/P draft SAP already holds for it (made by hand, usually).
    sap_draft_entries = serializers.ListField(child=serializers.IntegerField())
    # This app's entry for it, if any; such a GRPO cannot be picked again.
    entry_no = serializers.CharField(allow_blank=True)


class APInvoiceDraftListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = APInvoiceDraft
        fields = [
            "id", "entry_no", "company_code",
            "grpo_doc_entry", "grpo_doc_num", "grpo_date", "grpo_reference",
            "vendor_code", "vendor_name", "grpo_total",
            "sap_status", "sap_draft_entry", "sap_draft_adopted",
            "created_by_name", "created_at",
        ]

    def get_created_by_name(self, obj):
        return _person(obj.created_by)


class APInvoiceDraftDetailSerializer(APInvoiceDraftListSerializer):
    invoice_file_url = serializers.SerializerMethodField()

    class Meta(APInvoiceDraftListSerializer.Meta):
        fields = APInvoiceDraftListSerializer.Meta.fields + [
            "invoice_file_url", "invoice_filename",
            "sap_error", "sap_attachment_entry", "sap_attachment_error", "sap_created_at",
        ]

    def get_invoice_file_url(self, obj):
        if not obj.invoice_file:
            return ""
        request = self.context.get("request")
        url = obj.invoice_file.url
        return request.build_absolute_uri(url) if request else url

class APInvoiceDraftCreateSerializer(serializers.Serializer):
    grpo_doc_entry = serializers.IntegerField(min_value=1)
    invoice_file = serializers.FileField()
