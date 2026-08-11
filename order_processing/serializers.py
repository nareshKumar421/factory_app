from rest_framework import serializers

from .models import (
    MaterialRequirement,
    OmsOrder,
    OmsOrderLine,
    OmsSyncRun,
    ProcessingEvent,
    ProcurementRequirement,
    ProductionRequirement,
    RequirementSource,
    StockCheck,
    StockCheckLine,
)


class OmsOrderLineSerializer(serializers.ModelSerializer):
    is_trustworthy = serializers.BooleanField(read_only=True)

    class Meta:
        model = OmsOrderLine
        fields = ["id", "oms_line_id", "item_code", "item_name", "category", "brand",
                  "sub_group", "quantity", "pack_size", "cases", "litres",
                  "scheme_quantity", "unit_price", "line_total", "warehouse_code",
                  "issues", "is_trustworthy"]


class OmsOrderListSerializer(serializers.ModelSerializer):
    line_count = serializers.IntegerField(read_only=True)
    issue_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = OmsOrder
        fields = ["id", "oms_order_id", "order_number", "customer_code", "customer_name",
                  "company_code", "branch_name", "oms_status", "state", "delivery_date",
                  "sap_created", "sap_doc_number", "total_amount", "oms_created_at",
                  "line_count", "issue_count"]


class StockCheckLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockCheckLine
        fields = ["item_code", "warehouse_code", "required", "on_hand",
                  "committed_in_sap", "local_demand", "available",
                  "available_in_group", "elsewhere", "allocatable", "short",
                  "verdict", "notes"]


class StockCheckSerializer(serializers.ModelSerializer):
    lines = StockCheckLineSerializer(many=True, read_only=True)

    class Meta:
        model = StockCheck
        fields = ["id", "checked_at", "checked_by", "sap_company", "verdict",
                  "total_short", "errors", "lines"]


class MaterialRequirementSerializer(serializers.ModelSerializer):
    is_short = serializers.BooleanField(read_only=True)

    class Meta:
        model = MaterialRequirement
        fields = ["id", "item_code", "item_name", "warehouse_code",
                  "quantity_per_unit", "gross_required", "on_hand", "committed",
                  "incoming_po", "net_required", "stock_known", "is_short",
                  "computed_at"]


class RequirementSourceSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.order_number", read_only=True)
    customer_name = serializers.CharField(source="order.customer_name", read_only=True)
    delivery_date = serializers.DateField(source="order.delivery_date", read_only=True)

    class Meta:
        model = RequirementSource
        fields = ["order_number", "customer_name", "delivery_date", "shortfall"]


class ProductionRequirementSerializer(serializers.ModelSerializer):
    sources = RequirementSourceSerializer(many=True, read_only=True)
    materials = MaterialRequirementSerializer(many=True, read_only=True)

    class Meta:
        model = ProductionRequirement
        fields = ["id", "item_code", "item_name", "warehouse_code", "sap_company",
                  "quantity", "needed_by", "status", "production_run", "notes",
                  "created_at", "updated_at", "sources", "materials"]


class ProcurementRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcurementRequirement
        fields = ["id", "item_code", "item_name", "warehouse_code", "sap_company",
                  "quantity", "incoming_po", "needed_by", "status", "notes",
                  "created_at", "updated_at"]


class ProcessingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingEvent
        fields = ["id", "created_at", "event", "entity_type", "entity_id", "source",
                  "actor", "old_state", "new_state", "result", "detail", "error"]


class OmsSyncRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = OmsSyncRun
        fields = ["id", "started_at", "finished_at", "status", "watermark_from",
                  "watermark_to", "orders_seen", "orders_created", "orders_updated",
                  "lines_written", "issues_found", "error", "triggered_by"]


class OmsOrderDetailSerializer(serializers.ModelSerializer):
    lines = OmsOrderLineSerializer(many=True, read_only=True)
    latest_check = serializers.SerializerMethodField()

    class Meta:
        model = OmsOrder
        fields = ["id", "oms_order_id", "order_number", "customer_code", "customer_name",
                  "company_code", "branch_bpl_id", "branch_name", "oms_status", "state",
                  "order_type", "po_number", "ship_to_address", "total_amount", "is_foc",
                  "remarks", "delivery_date", "delivery_date_raw", "sap_created",
                  "sap_doc_number", "quotation_cancelled", "oms_created_at",
                  "oms_updated_at", "last_synced_at", "lines", "latest_check"]

    def get_latest_check(self, obj):
        check = obj.stock_checks.prefetch_related("lines").first()
        return StockCheckSerializer(check).data if check else None
