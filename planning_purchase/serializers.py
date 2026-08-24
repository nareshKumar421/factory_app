"""planning_purchase/serializers.py

Query validation and response shaping. Plan and requirement data comes straight
from SAP and is never written, so those are plain `Serializer` classes; purchase
orders are ours and use `ModelSerializer`.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import MaterialType, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from .services import calendar as cal


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


class PlanListQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, default=36, min_value=1, max_value=200)


class PlanDetailQuerySerializer(serializers.Serializer):
    bucket_type = serializers.ChoiceField(
        choices=cal.BUCKET_TYPES, required=False, default=cal.MONTH,
        help_text="DAY, WEEK or MONTH",
    )
    spread_policy = serializers.ChoiceField(
        choices=cal.SPREAD_POLICIES, required=False, default=cal.POLICY_EVEN_WORKING_DAYS,
        help_text="EVEN_WORKING_DAYS spreads the monthly lump across working days; "
                  "PERIOD_START leaves it on the date SAP recorded.",
    )
    include_actuals = serializers.BooleanField(required=False, default=True)


class RequirementQuerySerializer(serializers.Serializer):
    material_type = serializers.ChoiceField(
        choices=[MaterialType.PACKAGING, MaterialType.RAW, MaterialType.OTHER],
        required=False, allow_blank=True,
    )
    warehouse = serializers.CharField(required=False, allow_blank=True, default="")
    include_covered = serializers.BooleanField(required=False, default=True)

    def validate_warehouse(self, value):
        if not value:
            return []
        return [code.strip() for code in value.split(",") if code.strip()]

    def validate_material_type(self, value):
        return value or None


class VendorQuerySerializer(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(required=False, default=100, min_value=1, max_value=500)


# ---------------------------------------------------------------------------
# Plan responses
# ---------------------------------------------------------------------------


class PlanBucketSerializer(serializers.Serializer):
    bucket_type = serializers.CharField()
    bucket_start = serializers.DateField()
    label = serializers.CharField(required=False)
    planned_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    planned_litres = serializers.DecimalField(
        max_digits=24, decimal_places=3, required=False
    )
    planned_cases = serializers.DecimalField(
        max_digits=24, decimal_places=2, required=False
    )
    derived = serializers.BooleanField()
    spread_policy = serializers.CharField(required=False)


class PlanHeaderSerializer(serializers.Serializer):
    abs_id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    start_date = serializers.DateField(allow_null=True)
    end_date = serializers.DateField(allow_null=True)
    period_view = serializers.CharField()
    line_count = serializers.IntegerField()
    item_count = serializers.IntegerField(required=False)
    planned_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    planned_litres = serializers.DecimalField(
        max_digits=24, decimal_places=3, required=False
    )
    planned_cases = serializers.DecimalField(
        max_digits=24, decimal_places=2, required=False
    )
    produced_qty = serializers.DecimalField(
        max_digits=24, decimal_places=3, required=False
    )
    produced_litres = serializers.DecimalField(
        max_digits=24, decimal_places=3, required=False
    )
    produced_cases = serializers.DecimalField(
        max_digits=24, decimal_places=2, required=False
    )
    non_litre_item_count = serializers.IntegerField(required=False)
    non_litre_items = serializers.ListField(required=False)
    attainment_pct = serializers.DecimalField(
        max_digits=10, decimal_places=1, required=False
    )
    first_bucket_date = serializers.DateField(allow_null=True, required=False)
    last_bucket_date = serializers.DateField(allow_null=True, required=False)
    is_current = serializers.BooleanField()
    items_without_bom = serializers.ListField(required=False)


class PlanLineSerializer(serializers.Serializer):
    line_id = serializers.IntegerField(allow_null=True)
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    item_group = serializers.CharField()
    bucket_date = serializers.DateField(allow_null=True)
    warehouse_code = serializers.CharField()
    planned_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    planned_cases = serializers.DecimalField(max_digits=24, decimal_places=2)
    planned_litres = serializers.DecimalField(max_digits=24, decimal_places=3)
    uom = serializers.CharField()
    pieces_per_case = serializers.IntegerField()
    litres_per_unit = serializers.DecimalField(max_digits=18, decimal_places=6)
    is_litre_item = serializers.BooleanField()
    has_bom = serializers.BooleanField()
    produced_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    produced_cases = serializers.DecimalField(max_digits=24, decimal_places=2)
    produced_litres = serializers.DecimalField(max_digits=24, decimal_places=3)
    variance_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    variance_litres = serializers.DecimalField(max_digits=24, decimal_places=3)
    attainment_pct = serializers.DecimalField(max_digits=10, decimal_places=1)
    buckets = PlanBucketSerializer(many=True)


# ---------------------------------------------------------------------------
# Requirement responses
# ---------------------------------------------------------------------------


class RequirementUsageSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    plan_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    qty_per_unit = serializers.DecimalField(max_digits=24, decimal_places=6)
    required_qty = serializers.DecimalField(max_digits=24, decimal_places=3)


class RequirementWarehouseSerializer(serializers.Serializer):
    warehouse = serializers.CharField()
    on_hand = serializers.DecimalField(max_digits=24, decimal_places=3)
    committed = serializers.DecimalField(max_digits=24, decimal_places=3)
    min_stock = serializers.DecimalField(max_digits=24, decimal_places=3)


class RequirementRowSerializer(serializers.Serializer):
    component_code = serializers.CharField()
    component_name = serializers.CharField()
    item_group = serializers.CharField()
    material_type = serializers.CharField()
    uom = serializers.CharField()
    issue_warehouse = serializers.CharField()
    is_purchased = serializers.BooleanField()
    has_own_bom = serializers.BooleanField()

    required_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    on_hand_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    committed_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    net_available_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    benchmark_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    has_benchmark = serializers.BooleanField()
    on_order_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    open_po_lines = serializers.IntegerField()
    open_po_earliest_due = serializers.DateField(allow_null=True)
    shortage_before_po_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    shortage_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    suggested_order_qty = serializers.DecimalField(max_digits=24, decimal_places=3)

    moq = serializers.DecimalField(
        max_digits=24, decimal_places=3, allow_null=True, required=False
    )
    moq_applied = serializers.DecimalField(
        max_digits=24, decimal_places=3, allow_null=True, required=False
    )
    lead_time_days = serializers.IntegerField(allow_null=True)
    lead_time_source = serializers.CharField()
    need_by_date = serializers.DateField(allow_null=True)
    order_by_date = serializers.DateField(allow_null=True)
    urgency = serializers.CharField()
    days_since_last_use = serializers.IntegerField(allow_null=True)

    vendor_code = serializers.CharField()
    vendor_name = serializers.CharField()
    unit_price = serializers.DecimalField(max_digits=24, decimal_places=6)
    price_source = serializers.CharField()
    # Evidence only. In the purchase unit, which for bulk oil is a metric ton
    # against a litre BOM — never multiply a requirement by it.
    last_po_price = serializers.DecimalField(
        max_digits=24, decimal_places=4, allow_null=True, required=False
    )
    last_po_date = serializers.DateField(allow_null=True, required=False)
    currency = serializers.CharField()
    estimated_value = serializers.DecimalField(max_digits=24, decimal_places=2)
    is_over_committed = serializers.BooleanField()

    used_by = RequirementUsageSerializer(many=True)
    warehouses = RequirementWarehouseSerializer(many=True)


class RequirementResourceSerializer(serializers.Serializer):
    """A conversion cost the plan incurs — filling, blowing, job work.

    Listed so the plan's cost picture is complete, never offered for purchase.
    """

    resource_code = serializers.CharField()
    resource_name = serializers.CharField()
    required_qty = serializers.DecimalField(max_digits=24, decimal_places=3)
    used_by_count = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    line_value = serializers.DecimalField(
        max_digits=24, decimal_places=4, read_only=True
    )

    class Meta:
        model = PurchaseOrderLine
        fields = [
            "id", "item_code", "item_name", "material_type", "uom",
            "quantity", "unit_price", "line_value", "warehouse_code", "required_date",
            "required_qty", "available_qty", "on_order_qty", "shortage_qty",
            "moq_applied", "sap_line_num",
        ]
        read_only_fields = ["id", "sap_line_num"]


class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    line_count = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            "id", "company_code",
            "plan_abs_id", "plan_code", "plan_name",
            "vendor_code", "vendor_name",
            "doc_date", "doc_due_date", "warehouse_code", "remarks",
            "status", "status_display", "is_editable",
            "total_value", "currency", "line_count",
            "sap_doc_entry", "sap_doc_num", "sap_error_message", "posted_at", "simulated",
            "created_by", "created_by_name", "approved_by", "approved_by_name",
            "approved_at", "created_at", "updated_at",
            "lines",
        ]
        read_only_fields = [
            "id", "company_code", "status", "total_value",
            "sap_doc_entry", "sap_doc_num", "sap_error_message", "posted_at", "simulated",
            "created_by", "approved_by", "approved_at", "created_at", "updated_at",
        ]

    def get_created_by_name(self, obj):
        return getattr(obj.created_by, "full_name", "") if obj.created_by_id else ""

    def get_approved_by_name(self, obj):
        return getattr(obj.approved_by, "full_name", "") if obj.approved_by_id else ""

    def get_line_count(self, obj):
        return obj.lines.count()


class PurchaseOrderLineInputSerializer(serializers.Serializer):
    """One requirement row the buyer chose to order."""

    item_code = serializers.CharField(max_length=100)
    item_name = serializers.CharField(required=False, allow_blank=True, default="")
    item_group = serializers.CharField(required=False, allow_blank=True, default="")
    material_type = serializers.CharField(required=False, allow_blank=True, default="")
    uom = serializers.CharField(required=False, allow_blank=True, default="")

    vendor_code = serializers.CharField(max_length=50)
    quantity = serializers.DecimalField(
        max_digits=24, decimal_places=6, min_value=Decimal("0.000001")
    )
    unit_price = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, default=Decimal("0")
    )
    warehouse_code = serializers.CharField(required=False, allow_blank=True, default="")
    required_date = serializers.DateField(required=False, allow_null=True)

    # Snapshot of why, carried through so the approver can check the number.
    required_qty = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, default=Decimal("0")
    )
    available_qty = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, default=Decimal("0")
    )
    on_order_qty = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, default=Decimal("0")
    )
    shortage_qty = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, default=Decimal("0")
    )
    moq_applied = serializers.DecimalField(
        max_digits=24, decimal_places=6, required=False, allow_null=True
    )


class PurchaseOrderCreateSerializer(serializers.Serializer):
    plan_abs_id = serializers.IntegerField(required=False, allow_null=True)
    plan_code = serializers.CharField(required=False, allow_blank=True, default="")
    plan_name = serializers.CharField(required=False, allow_blank=True, default="")
    doc_due_date = serializers.DateField(required=False, allow_null=True)
    warehouse_code = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    currency = serializers.CharField(required=False, allow_blank=True, default="INR")
    lines = PurchaseOrderLineInputSerializer(many=True, allow_empty=False)


class PurchaseOrderUpdateSerializer(serializers.Serializer):
    vendor_code = serializers.CharField(required=False, max_length=50)
    doc_due_date = serializers.DateField(required=False)
    warehouse_code = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
    lines = PurchaseOrderLineInputSerializer(many=True, required=False)


class PurchaseOrderListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=PurchaseOrderStatus.choices, required=False, allow_blank=True
    )
    plan_abs_id = serializers.IntegerField(required=False, allow_null=True)
    search = serializers.CharField(required=False, allow_blank=True, default="")
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(
        required=False, default=25, min_value=1, max_value=200
    )


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")
