"""Serializers for the bill summary."""

from rest_framework import serializers

from .models_bill_summary import BillSummary, BillSummaryLine


class BillSummaryLineSerializer(serializers.ModelSerializer):
    is_short = serializers.BooleanField(read_only=True)

    class Meta:
        model = BillSummaryLine
        fields = [
            "id",
            "sap_line_num",
            "item_code",
            "item_name",
            "uom",
            "warehouse_code",
            "invoice_qty",
            "pcs_per_box",
            "boxes",
            "loose_qty",
            "litres",
            "gross_weight",
            "dispatch_qty",
            "is_short",
        ]


def _person(user) -> str:
    """`accounts.User` has `full_name`, not `get_full_name()`."""
    if not user:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


class BillSummaryListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    issued_by_name = serializers.SerializerMethodField()
    picked_by_name = serializers.SerializerMethodField()
    totals = serializers.SerializerMethodField()

    class Meta:
        model = BillSummary
        fields = [
            "id",
            "entry_no",
            "company",
            "company_code",
            "sap_invoice_doc_entry",
            "sap_invoice_doc_num",
            "customer_code",
            "customer_name",
            "delivery_address",
            "invoice_date",
            "bill_amount",
            "branch_name",
            "branch_gstin",
            "warehouse_codes",
            "dispatch_date",
            "bilty_no",
            "bilty_date",
            "transporter_name",
            "vehicle_no",
            "driver_name",
            "driver_mobile",
            "status",
            "sap_status",
            "sap_error",
            "sap_posted_at",
            "issued_by_name",
            "picked_by_name",
            "issued_at",
            "picked_at",
            "remarks",
            "cancel_reason",
            "totals",
        ]

    def get_issued_by_name(self, obj) -> str:
        return _person(obj.issued_by)

    def get_picked_by_name(self, obj) -> str:
        return _person(obj.picked_by)

    def get_totals(self, obj) -> dict:
        return obj.totals()


class BillSummaryDetailSerializer(BillSummaryListSerializer):
    lines = BillSummaryLineSerializer(source="active_lines", many=True, read_only=True)

    class Meta(BillSummaryListSerializer.Meta):
        fields = BillSummaryListSerializer.Meta.fields + ["lines"]


class BillSummaryGenerateLineSerializer(serializers.Serializer):
    sap_line_num = serializers.IntegerField()
    dispatch_qty = serializers.DecimalField(max_digits=18, decimal_places=3)


class BillSummaryGenerateSerializer(serializers.Serializer):
    """The form: what the app found, with the user's corrections and additions.

    `bilty_no` is required here rather than optional-with-a-later-nag because SAP
    will not take the posting without it — see `bill_summary_service`.
    """

    sap_invoice_doc_entry = serializers.IntegerField()
    sap_invoice_doc_num = serializers.CharField(max_length=30, required=False, allow_blank=True)
    dispatch_date = serializers.DateField()
    bilty_no = serializers.CharField(max_length=50)
    bilty_date = serializers.DateField(required=False, allow_null=True)
    transporter_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    vehicle_no = serializers.CharField(max_length=30, required=False, allow_blank=True)
    driver_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    driver_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    # Only the lines being dispatched short need sending; the rest default to the
    # full billed quantity.
    lines = BillSummaryGenerateLineSerializer(many=True, required=False)


class BillSummaryCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)
