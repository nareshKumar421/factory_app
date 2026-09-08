from rest_framework import serializers


class WarehouseSerializer(serializers.Serializer):
    warehouse_code = serializers.CharField()
    warehouse_name = serializers.CharField()


class VendorSerializer(serializers.Serializer):
    vendor_code = serializers.CharField()
    vendor_name = serializers.CharField()


class POItemSerializer(serializers.Serializer):
    po_item_code = serializers.CharField()
    item_name = serializers.CharField()
    ordered_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    received_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    remaining_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    uom = serializers.CharField()
    rate = serializers.DecimalField(max_digits=18, decimal_places=6)
    line_num = serializers.IntegerField()


class POSerializer(serializers.Serializer):
    po_number = serializers.CharField()
    supplier_code = serializers.CharField()
    supplier_name = serializers.CharField()
    doc_entry = serializers.IntegerField()
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    vendor_ref = serializers.CharField(required=False, allow_blank=True)
    doc_date = serializers.DateField(required=False, allow_null=True)
    items = POItemSerializer(many=True)
    # Whether the gate should hold receipts on this PO to 110% of a line's open
    # quantity. SAP only enforces that in some companies, so the screen must be told
    # rather than hard-coding the list. Follows the `box_scan_optional` precedent.
    over_receipt_enforced = serializers.SerializerMethodField()

    def get_over_receipt_enforced(self, _po) -> bool:
        return bool(self.context.get("over_receipt_enforced", False))


# ---- GRPO Serializers ----

class GRPOLineRequestSerializer(serializers.Serializer):
    """Serializer for GRPO document line items"""
    ItemCode = serializers.CharField(required=True, help_text="Item code")
    Quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=True, help_text="Quantity received"
    )
    TaxCode = serializers.CharField(required=False, allow_blank=True, help_text="Tax code")
    UnitPrice = serializers.DecimalField(
        max_digits=18, decimal_places=6, required=False, help_text="Unit price"
    )
    WarehouseCode = serializers.CharField(required=False, allow_blank=True, help_text="Warehouse code")
    BaseEntry = serializers.IntegerField(required=False, help_text="Base PO DocEntry")
    BaseLine = serializers.IntegerField(required=False, help_text="Base PO line number")
    BaseType = serializers.IntegerField(required=False, help_text="Base document type (22 for PO)")


class GRPORequestSerializer(serializers.Serializer):
    """Serializer for GRPO creation request"""
    CardCode = serializers.CharField(required=True, help_text="Supplier/Vendor code")
    BPL_IDAssignedToInvoice = serializers.IntegerField(required=False, help_text="Branch/Business Place ID")
    DocDate = serializers.DateField(required=False, help_text="Document date")
    DocDueDate = serializers.DateField(required=False, help_text="Document due date")
    Comments = serializers.CharField(required=False, allow_blank=True, help_text="Comments")
    DocumentLines = GRPOLineRequestSerializer(many=True, required=True, help_text="Document lines")

    def validate_DocumentLines(self, value):
        if not value:
            raise serializers.ValidationError("At least one document line is required")
        return value


class GRPOResponseSerializer(serializers.Serializer):
    """Serializer for GRPO creation response"""
    DocEntry = serializers.IntegerField(help_text="Document entry (internal ID)")
    DocNum = serializers.IntegerField(help_text="Document number")
    CardCode = serializers.CharField(help_text="Supplier code")
    CardName = serializers.CharField(required=False, help_text="Supplier name")
    DocDate = serializers.CharField(required=False, help_text="Document date")
    DocTotal = serializers.DecimalField(
        max_digits=18, decimal_places=6, required=False, help_text="Document total"
    )
