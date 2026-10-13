"""Serializers for cash sale out (see ``gate_core.views_cash_sale_out``)."""
from rest_framework import serializers

from gate_core.models import CashSaleGateOut, CashSaleGateOutBill


class CashSaleGateOutBillSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashSaleGateOutBill
        fields = [
            "id",
            "sap_doc_entry",
            "sap_doc_num",
            "doc_date",
            "customer_code",
            "customer_name",
            "customer_ref",
            "doc_total",
            "item_summary",
            "total_quantity",
        ]


class CashSaleGateOutSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    bills = CashSaleGateOutBillSerializer(many=True, read_only=True)
    marked_out_by_name = serializers.SerializerMethodField()
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CashSaleGateOut
        fields = [
            "id",
            "entry_no",
            "company",
            "company_code",
            "company_name",
            "taken_by_name",
            "taken_by_mobile",
            "vehicle_no",
            "remarks",
            "out_at",
            "status",
            "marked_out_by_name",
            "cancel_reason",
            "cancelled_by_name",
            "cancelled_at",
            "bills",
        ]

    @staticmethod
    def _user_name(user):
        return (getattr(user, "full_name", "") or user.email) if user else ""

    def get_marked_out_by_name(self, obj):
        return self._user_name(obj.created_by)

    def get_cancelled_by_name(self, obj):
        return self._user_name(obj.cancelled_by)


class CashSaleGateOutCreateSerializer(serializers.Serializer):
    company_id = serializers.IntegerField()
    doc_entries = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=50
    )
    taken_by_name = serializers.CharField(max_length=100)
    taken_by_mobile = serializers.CharField(max_length=20, required=False, allow_blank=True)
    vehicle_no = serializers.CharField(max_length=30, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)

    def validate_taken_by_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Enter the name of the person taking it out.")
        return value


class CashSaleGateOutCancelSerializer(serializers.Serializer):
    reason = serializers.CharField()

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("Say why this entry is being cancelled.")
        return value
