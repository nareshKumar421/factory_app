from rest_framework import serializers

from .models import APInvoiceDraft, APInvoiceDraftCheck, CheckStatus, ReviewDecision


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


class APInvoiceDraftCheckSerializer(serializers.ModelSerializer):
    effective_status = serializers.CharField(read_only=True)
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = APInvoiceDraftCheck
        fields = [
            "key", "position", "label", "status", "detail", "facts",
            "review_decision", "review_remark", "reviewed_by_name", "reviewed_at",
            "effective_status",
        ]

    def get_reviewed_by_name(self, obj):
        return _person(obj.reviewed_by)


def _check_counts(entry) -> dict:
    counts = {status: 0 for status in CheckStatus.values}
    for check in entry.checks.all():
        counts[check.effective_status] += 1
    return counts


class APInvoiceDraftListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    check_counts = serializers.SerializerMethodField()

    class Meta:
        model = APInvoiceDraft
        fields = [
            "id", "entry_no", "company_code",
            "grpo_doc_entry", "grpo_doc_num", "grpo_date", "grpo_reference",
            "vendor_code", "vendor_name", "grpo_total",
            "sap_status", "sap_draft_entry", "sap_draft_adopted",
            "invoice_read_status", "check_counts",
            "created_by_name", "created_at",
        ]

    def get_created_by_name(self, obj):
        return _person(obj.created_by)

    def get_check_counts(self, obj):
        return _check_counts(obj)


class APInvoiceDraftDetailSerializer(APInvoiceDraftListSerializer):
    invoice_file_url = serializers.SerializerMethodField()
    checks = APInvoiceDraftCheckSerializer(many=True, read_only=True)
    gate_entry_no = serializers.SerializerMethodField()

    class Meta(APInvoiceDraftListSerializer.Meta):
        fields = APInvoiceDraftListSerializer.Meta.fields + [
            "invoice_file_url", "invoice_filename",
            "invoice_data", "invoice_read_error", "invoice_read_model", "invoice_read_at",
            "sap_error", "sap_attachment_entry", "sap_attachment_error", "sap_created_at",
            "grpo_posting", "gate_entry_no", "checks", "checks_run_at",
        ]

    def get_invoice_file_url(self, obj):
        if not obj.invoice_file:
            return ""
        request = self.context.get("request")
        url = obj.invoice_file.url
        return request.build_absolute_uri(url) if request else url

    def get_gate_entry_no(self, obj):
        posting = obj.grpo_posting
        return posting.vehicle_entry.entry_no if posting and posting.vehicle_entry_id else ""


class APInvoiceDraftCreateSerializer(serializers.Serializer):
    grpo_doc_entry = serializers.IntegerField(min_value=1)
    invoice_file = serializers.FileField()


class CheckReviewSerializer(serializers.Serializer):
    # Blank clears a decision and hands the check back to the app's finding.
    decision = serializers.ChoiceField(
        choices=[("", "Clear")] + list(ReviewDecision.choices), allow_blank=True,
    )
    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)
