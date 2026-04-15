from rest_framework import serializers


class POItemReceiveSerializer(serializers.Serializer):
    """Serializer for individual PO item in receive request"""
    line_num = serializers.IntegerField(min_value=0)
    po_item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200)
    ordered_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0)
    received_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0)
    uom = serializers.CharField(max_length=20)

    def validate_received_qty(self, value):
        if value < 0:
            raise serializers.ValidationError("Received quantity cannot be negative")
        return value


class POReceiveRequestSerializer(serializers.Serializer):
    """Serializer for PO receive request - validates header + items"""
    po_number = serializers.CharField(
        max_length=50,
        required=True,
        error_messages={
            'required': 'PO number is required',
            'blank': 'PO number cannot be blank'
        }
    )
    supplier_code = serializers.CharField(
        max_length=50,
        required=True,
        error_messages={
            'required': 'Supplier code is required',
            'blank': 'Supplier code cannot be blank'
        }
    )
    supplier_name = serializers.CharField(
        max_length=200,
        required=True,
        error_messages={
            'required': 'Supplier name is required',
            'blank': 'Supplier name cannot be blank'
        }
    )
    items = POItemReceiveSerializer(many=True, required=True)

    def validate_po_number(self, value):
        if value:
            return value.strip()
        return value

    def validate_supplier_code(self, value):
        if value:
            return value.strip()
        return value

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one PO item is required")
        return value


class POItemReceiptSerializer(serializers.Serializer):
    """Serializer for PO item receipt output"""
    sap_line_num = serializers.IntegerField()
    po_item_code = serializers.CharField()
    item_name = serializers.CharField()
    ordered_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    received_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    short_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    uom = serializers.CharField()


class POReceiptSerializer(serializers.Serializer):
    """Serializer for PO receipt output"""
    po_number = serializers.CharField()
    supplier_code = serializers.CharField()
    supplier_name = serializers.CharField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    items = POItemReceiptSerializer(many=True)
