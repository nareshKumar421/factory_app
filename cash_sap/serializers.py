"""Request and response shapes for the cash book -> SAP API.

Read serializers come in pairs with plain write serializers (``required=False``
everywhere a PATCH may leave a field out), as elsewhere in the repo. The plan
itself is not a serializer: it is ``BunchPlan.as_dict()``.
"""

from rest_framework import serializers

from .models import GLItemMap, GrpoGrouping, HeadTreatment, ImprestVoucherBill, PostingMode
from .payloads import person_label


class BranchRoutingSerializer(serializers.Serializer):
    """One ``(branch, routing or None)`` pair from ``services.routing_rows``."""

    FIELDS = (
        "enabled", "sap_company_code", "mode", "vendor_code", "bpl_id", "warehouse_code",
        "ocr_code", "ocr_code3", "ocr_code5", "item_tax_code", "service_tax_code",
        "ap_drafts_enabled", "grpo_needs_attachment", "grpo_grouping",
        "po_series_hint", "grpo_series_hint", "ap_series_hint", "cutover_date",
    )
    DEFAULTS = {
        "enabled": False, "sap_company_code": "", "mode": PostingMode.PO_GRPO, "vendor_code": "",
        "bpl_id": None, "warehouse_code": "", "ocr_code": "", "ocr_code3": "", "ocr_code5": "",
        "item_tax_code": "IGST@0", "service_tax_code": "Exampt", "ap_drafts_enabled": False,
        "grpo_needs_attachment": True, "grpo_grouping": GrpoGrouping.NONE, "po_series_hint": "",
        "grpo_series_hint": "", "ap_series_hint": "", "cutover_date": None,
    }

    def to_representation(self, pair):
        branch, routing = pair
        out = {
            "branch_id": branch.id,
            "branch_name": branch.name,
            "branch_is_active": branch.is_active,
            "configured": routing is not None,
            "id": routing.id if routing else None,
            "updated_by_name": person_label(routing.updated_by) if routing else "",
            "updated_at": routing.updated_at.isoformat() if routing and routing.updated_at else None,
        }
        for name in self.FIELDS:
            value = getattr(routing, name) if routing is not None else self.DEFAULTS[name]
            if name == "cutover_date" and value is not None:
                value = value.isoformat()
            out[name] = value
        return out


class BranchRoutingWriteSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    sap_company_code = serializers.CharField(required=False, allow_blank=True, max_length=30)
    mode = serializers.ChoiceField(required=False, choices=PostingMode.choices)
    vendor_code = serializers.CharField(required=False, allow_blank=True, max_length=30)
    bpl_id = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    warehouse_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    ocr_code = serializers.CharField(required=False, allow_blank=True, max_length=30)
    ocr_code3 = serializers.CharField(required=False, allow_blank=True, max_length=30)
    ocr_code5 = serializers.CharField(required=False, allow_blank=True, max_length=30)
    item_tax_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    service_tax_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    ap_drafts_enabled = serializers.BooleanField(required=False)
    grpo_needs_attachment = serializers.BooleanField(required=False)
    grpo_grouping = serializers.ChoiceField(required=False, choices=GrpoGrouping.choices)
    po_series_hint = serializers.CharField(required=False, allow_blank=True, max_length=40)
    grpo_series_hint = serializers.CharField(required=False, allow_blank=True, max_length=40)
    ap_series_hint = serializers.CharField(required=False, allow_blank=True, max_length=40)
    cutover_date = serializers.DateField(required=False, allow_null=True)


class GLItemMapSerializer(serializers.ModelSerializer):
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = GLItemMap
        fields = [
            "id", "sap_company_code", "gl_account_code", "gl_account_name", "target_account_code",
            "treatment", "item_code", "item_name", "ocr_code3", "needs_review", "is_active",
            "updated_by_name", "updated_at",
        ]

    def get_updated_by_name(self, obj):
        return person_label(obj.updated_by)


class GLItemMapWriteSerializer(serializers.Serializer):
    sap_company_code = serializers.CharField(required=False, max_length=30)
    gl_account_code = serializers.CharField(required=False, max_length=32)
    gl_account_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    target_account_code = serializers.CharField(required=False, allow_blank=True, max_length=32)
    treatment = serializers.ChoiceField(required=False, choices=HeadTreatment.choices)
    item_code = serializers.CharField(required=False, allow_blank=True, max_length=50)
    item_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    ocr_code3 = serializers.CharField(required=False, allow_blank=True, max_length=30)
    needs_review = serializers.BooleanField(required=False)


class VoucherInfoSerializer(serializers.Serializer):
    """The bill's own facts (``VoucherSapInfo``)."""

    bill_date = serializers.DateField(required=False, allow_null=True)
    gate_entry_no = serializers.CharField(required=False, allow_blank=True, max_length=30)
    supplier_bill_no = serializers.CharField(required=False, allow_blank=True, max_length=60)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Change at least one field.")
        return attrs


class VoucherOverrideSerializer(VoucherInfoSerializer):
    """How one voucher posts. An empty string clears an override."""

    sap_company_code = serializers.CharField(required=False, allow_blank=True, max_length=30)
    treatment = serializers.ChoiceField(
        required=False, allow_blank=True, choices=[("ITEM", "ITEM"), ("SERVICE", "SERVICE"), ("SKIP", "SKIP")]
    )
    item_code = serializers.CharField(required=False, allow_blank=True, max_length=50)
    ocr_code = serializers.CharField(required=False, allow_blank=True, max_length=30)
    ocr_code3 = serializers.CharField(required=False, allow_blank=True, max_length=30)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class LeaveOutSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class GrpoRedateSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)
    gate_entry_no = serializers.CharField(required=False, allow_blank=True, max_length=60)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Give a posting date or a G.no.")
        return attrs


class SendNextSerializer(serializers.Serializer):
    include_failed = serializers.BooleanField(required=False, default=False)
    skip_posting_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list, max_length=500
    )


class StopSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class AcceptChangesSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=500)


class PostBunchSerializer(serializers.Serializer):
    sap_company_code = serializers.CharField(required=False, allow_blank=True, max_length=30)


class BillSerializer(serializers.ModelSerializer):
    entry_id = serializers.IntegerField(source="voucher.entry_id", read_only=True)
    url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ImprestVoucherBill
        fields = ["id", "entry_id", "original_filename", "size_bytes", "url", "uploaded_by_name", "created_at"]

    def get_url(self, obj):
        request = self.context.get("request")
        try:
            url = obj.file.url
        except ValueError:
            return ""
        return request.build_absolute_uri(url) if request else url

    def get_uploaded_by_name(self, obj):
        return person_label(obj.created_by)
