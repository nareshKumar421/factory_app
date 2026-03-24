from rest_framework import serializers
from .models import (
    ShipmentOrder, ShipmentOrderItem,
    PickTask, OutboundLoadRecord, GoodsIssuePosting,
)


# ---- Output Serializers ----

class ShipmentOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShipmentOrderItem
        fields = [
            "id", "sap_line_num", "item_code", "item_name",
            "ordered_qty", "picked_qty", "packed_qty", "loaded_qty",
            "uom", "warehouse_code", "batch_number", "weight", "pick_status",
        ]


class ShipmentOrderListSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(source="items.count", read_only=True)
    vehicle_entry_no = serializers.CharField(
        source="vehicle_entry.entry_no", read_only=True, default=None
    )

    class Meta:
        model = ShipmentOrder
        fields = [
            "id", "sap_doc_entry", "sap_doc_num",
            "customer_code", "customer_name",
            "carrier_name", "scheduled_date", "dock_bay",
            "status", "bill_of_lading_no", "seal_number",
            "total_weight", "item_count", "vehicle_entry_no",
            "created_at",
        ]


class ShipmentOrderDetailSerializer(serializers.ModelSerializer):
    items = ShipmentOrderItemSerializer(many=True, read_only=True)
    vehicle_entry_no = serializers.CharField(
        source="vehicle_entry.entry_no", read_only=True, default=None
    )

    class Meta:
        model = ShipmentOrder
        fields = [
            "id", "sap_doc_entry", "sap_doc_num",
            "customer_code", "customer_name", "ship_to_address",
            "carrier_code", "carrier_name",
            "scheduled_date", "dock_bay", "dock_slot_start", "dock_slot_end",
            "status", "vehicle_entry_no",
            "bill_of_lading_no", "seal_number", "total_weight",
            "notes", "items", "created_at", "updated_at",
        ]


class PickTaskSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source="shipment_item.item_code", read_only=True)
    item_name = serializers.CharField(source="shipment_item.item_name", read_only=True)
    assigned_to_name = serializers.SerializerMethodField()

    class Meta:
        model = PickTask
        fields = [
            "id", "item_code", "item_name",
            "assigned_to", "assigned_to_name",
            "pick_location", "pick_qty", "actual_qty",
            "status", "picked_at", "scanned_barcode",
        ]

    def get_assigned_to_name(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.full_name or obj.assigned_to.email
        return None


class OutboundLoadRecordSerializer(serializers.ModelSerializer):
    inspected_by_name = serializers.SerializerMethodField()
    supervisor_name = serializers.SerializerMethodField()

    class Meta:
        model = OutboundLoadRecord
        fields = [
            "id", "trailer_condition", "trailer_temp_ok", "trailer_temp_reading",
            "inspected_by", "inspected_by_name", "inspected_at",
            "loading_started_at", "loading_completed_at",
            "supervisor_confirmed", "supervisor", "supervisor_name", "confirmed_at",
        ]

    def get_inspected_by_name(self, obj):
        if obj.inspected_by:
            return obj.inspected_by.full_name or obj.inspected_by.email
        return None

    def get_supervisor_name(self, obj):
        if obj.supervisor:
            return obj.supervisor.full_name or obj.supervisor.email
        return None


class GoodsIssuePostingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodsIssuePosting
        fields = [
            "id", "sap_doc_entry", "sap_doc_num",
            "status", "posted_at", "posted_by",
            "error_message", "retry_count",
        ]


# ---- Input Serializers ----

class AssignBaySerializer(serializers.Serializer):
    dock_bay = serializers.CharField(max_length=10, required=True)
    dock_slot_start = serializers.DateTimeField(required=False, allow_null=True)
    dock_slot_end = serializers.DateTimeField(required=False, allow_null=True)


class PickTaskUpdateSerializer(serializers.Serializer):
    assigned_to = serializers.IntegerField(required=False)
    status = serializers.ChoiceField(
        choices=["PENDING", "IN_PROGRESS", "COMPLETED", "SHORT"],
        required=False
    )
    actual_qty = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, min_value=0
    )
    scanned_barcode = serializers.CharField(max_length=100, required=False)


class ScanSerializer(serializers.Serializer):
    barcode = serializers.CharField(max_length=100, required=True)


class LinkVehicleSerializer(serializers.Serializer):
    vehicle_entry_id = serializers.IntegerField(required=True)


class InspectTrailerSerializer(serializers.Serializer):
    trailer_condition = serializers.ChoiceField(
        choices=["CLEAN", "DAMAGED", "REJECTED"], required=True
    )
    trailer_temp_ok = serializers.BooleanField(required=False, allow_null=True)
    trailer_temp_reading = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True
    )


class LoadItemSerializer(serializers.Serializer):
    item_id = serializers.IntegerField(required=True)
    loaded_qty = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=True, min_value=0
    )


class LoadSerializer(serializers.Serializer):
    items = LoadItemSerializer(many=True, required=True)


class DispatchSerializer(serializers.Serializer):
    seal_number = serializers.CharField(max_length=50, required=True)
    branch_id = serializers.IntegerField(required=False, allow_null=True)


class SyncFiltersSerializer(serializers.Serializer):
    customer_code = serializers.CharField(max_length=50, required=False)
    from_date = serializers.DateField(required=False)
    to_date = serializers.DateField(required=False)
