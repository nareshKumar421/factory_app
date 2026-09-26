"""Request bodies of the SAP production-order screens (see views_sap_orders)."""

from rest_framework import serializers

from .models_sap_orders import SapProductionOrderAction


class OrderLineSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    planned_quantity = serializers.DecimalField(max_digits=19, decimal_places=6, min_value=0)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)


class CreateOrderSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    planned_quantity = serializers.DecimalField(max_digits=19, decimal_places=6)
    due_date = serializers.DateField()
    start_date = serializers.DateField(required=False)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)
    remarks = serializers.CharField(max_length=254, required=False, allow_blank=True)
    release = serializers.BooleanField(default=False)
    lines = OrderLineSerializer(many=True, required=False)
    confirm_repeat = serializers.BooleanField(default=False)

    def validate_planned_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("The planned quantity must be more than zero.")
        return value

    def validate(self, attrs):
        if attrs.get("start_date") and attrs["start_date"] > attrs["due_date"]:
            raise serializers.ValidationError({"due_date": "The due date is before the start date."})
        return attrs


class BatchSerializer(serializers.Serializer):
    batch_number = serializers.CharField(max_length=36)
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6, min_value=0)


class IssueLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField(min_value=0)
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)
    batches = BatchSerializer(many=True, required=False)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Issue a quantity of more than zero.")
        return value


class IssueSerializer(serializers.Serializer):
    lines = IssueLineSerializer(many=True)
    posting_date = serializers.DateField(required=False)
    remarks = serializers.CharField(max_length=254, required=False, allow_blank=True)
    confirm_repeat = serializers.BooleanField(default=False)

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("Issue at least one line.")
        numbers = [line["line_num"] for line in lines]
        if len(numbers) != len(set(numbers)):
            raise serializers.ValidationError("Each order line may appear only once.")
        return lines


class ReceiptSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6)
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)
    batch_number = serializers.CharField(max_length=36, required=False, allow_blank=True)
    posting_date = serializers.DateField(required=False)
    remarks = serializers.CharField(max_length=254, required=False, allow_blank=True)
    confirm_repeat = serializers.BooleanField(default=False)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Receive a quantity of more than zero.")
        return value


class SapProductionOrderActionSerializer(serializers.ModelSerializer):
    action_label = serializers.CharField(source="get_action_display", read_only=True)
    taken_by = serializers.SerializerMethodField()

    class Meta:
        model = SapProductionOrderAction
        fields = [
            "id", "action", "action_label", "order_doc_entry", "item_code", "quantity",
            "sap_doc_entry", "sap_doc_num", "pending_approval_draft", "taken_by", "created_at",
        ]
        read_only_fields = fields

    def get_taken_by(self, row):
        user = row.created_by
        return (getattr(user, "full_name", "") or getattr(user, "email", "")) if user else ""
