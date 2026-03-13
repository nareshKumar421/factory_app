from rest_framework import serializers
from .models import GRPOPosting, GRPOLinePosting, GRPOAttachment


class GRPOLineDetailSerializer(serializers.Serializer):
    """Serializer for GRPO line item details (for preview/preparation)"""
    po_item_receipt_id = serializers.IntegerField()
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    ordered_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    received_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    accepted_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    rejected_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    uom = serializers.CharField()
    qc_status = serializers.CharField()

    # Pre-filled from SAP PO — use these values during GRPO posting
    unit_price = serializers.DecimalField(
        max_digits=18, decimal_places=6, allow_null=True
    )
    tax_code = serializers.CharField(allow_blank=True)
    warehouse_code = serializers.CharField(allow_blank=True)
    gl_account = serializers.CharField(allow_blank=True)
    sap_line_num = serializers.IntegerField(allow_null=True)


class GRPOPreviewSerializer(serializers.Serializer):
    """
    Serializer for GRPO preview data.
    Returns all data needed to post GRPO after gate entry completion.
    """
    vehicle_entry_id = serializers.IntegerField()
    entry_no = serializers.CharField()
    entry_status = serializers.CharField()
    is_ready_for_grpo = serializers.BooleanField()

    po_receipt_id = serializers.IntegerField()
    po_number = serializers.CharField()
    supplier_code = serializers.CharField()
    supplier_name = serializers.CharField()

    # SAP PO reference for PO linking
    sap_doc_entry = serializers.IntegerField(allow_null=True)

    # Pre-filled from SAP PO — use these values during GRPO posting
    branch_id = serializers.IntegerField(allow_null=True)
    vendor_ref = serializers.CharField(allow_blank=True)

    invoice_no = serializers.CharField(allow_blank=True)
    invoice_date = serializers.DateField(allow_null=True)
    challan_no = serializers.CharField(allow_blank=True)

    items = GRPOLineDetailSerializer(many=True)

    # GRPO posting status if already attempted
    grpo_status = serializers.CharField(allow_null=True)
    sap_doc_num = serializers.IntegerField(allow_null=True)


class GRPOItemInputSerializer(serializers.Serializer):
    """Serializer for individual item accepted quantity input"""
    po_item_receipt_id = serializers.IntegerField(required=True)
    accepted_qty = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=True, min_value=0
    )
    unit_price = serializers.DecimalField(
        max_digits=18, decimal_places=6, required=False, allow_null=True,
        help_text="Unit price per item (from PO)"
    )
    tax_code = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="SAP Tax Code (e.g. GST18, IGST18)"
    )
    gl_account = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="G/L Account code for the line item"
    )
    variety = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="Item variety - maps to SAP UDF U_Variety"
    )


class ExtraChargeInputSerializer(serializers.Serializer):
    """Serializer for additional expense charges on GRPO"""
    expense_code = serializers.IntegerField(
        required=True,
        help_text="SAP Expense Code (from Additional Expenses setup)"
    )
    amount = serializers.DecimalField(
        max_digits=18, decimal_places=2, required=True, min_value=0,
        help_text="Total amount for this charge"
    )
    remarks = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="Description of the charge (e.g. Freight, Handling)"
    )
    tax_code = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="Tax code for this charge"
    )


class GRPOPostRequestSerializer(serializers.Serializer):
    """Serializer for GRPO posting request (supports multipart/form-data with attachments)"""
    vehicle_entry_id = serializers.IntegerField(required=True)
    po_receipt_id = serializers.IntegerField(required=True)
    items = GRPOItemInputSerializer(many=True, required=True)
    branch_id = serializers.IntegerField(
        required=True,
        help_text="SAP Branch/Business Place ID (BPLId)"
    )
    warehouse_code = serializers.CharField(required=False, allow_blank=True)
    comments = serializers.CharField(
        required=False, allow_blank=True,
        help_text="User remarks - will be appended to structured comment"
    )
    vendor_ref = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        help_text="Vendor reference / invoice number (NumAtCard in SAP)"
    )
    extra_charges = ExtraChargeInputSerializer(
        many=True, required=False,
        help_text="Additional expenses (freight, handling, etc.)"
    )
    doc_date = serializers.DateField(
        required=False, allow_null=True,
        help_text="Posting Date (DocDate) in SAP — defaults to today if not provided"
    )
    doc_due_date = serializers.DateField(
        required=False, allow_null=True,
        help_text="Due Date (DocDueDate) in SAP"
    )
    tax_date = serializers.DateField(
        required=False, allow_null=True,
        help_text="Document Date (TaxDate) in SAP"
    )
    round_off = serializers.DecimalField(
        max_digits=18, decimal_places=6, required=False, allow_null=True,
        help_text="Total amount round-off adjustment (RoundDif in SAP)"
    )

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item with accepted quantity is required")
        return value


class GRPOLinePostingSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='po_item_receipt.po_item_code', read_only=True)
    item_name = serializers.CharField(source='po_item_receipt.item_name', read_only=True)

    class Meta:
        model = GRPOLinePosting
        fields = [
            'id',
            'item_code',
            'item_name',
            'quantity_posted',
            'base_entry',
            'base_line'
        ]


class GRPOAttachmentSerializer(serializers.ModelSerializer):
    """Serializer for listing GRPO attachments"""
    class Meta:
        model = GRPOAttachment
        fields = [
            'id',
            'file',
            'original_filename',
            'sap_attachment_status',
            'sap_absolute_entry',
            'sap_error_message',
            'uploaded_at',
            'uploaded_by',
        ]
        read_only_fields = [
            'id',
            'original_filename',
            'sap_attachment_status',
            'sap_absolute_entry',
            'sap_error_message',
            'uploaded_at',
            'uploaded_by',
        ]


class GRPOAttachmentUploadSerializer(serializers.Serializer):
    """Serializer for uploading GRPO attachments"""
    file = serializers.FileField(required=True)


class GRPOPostingSerializer(serializers.ModelSerializer):
    lines = GRPOLinePostingSerializer(many=True, read_only=True)
    attachments = GRPOAttachmentSerializer(many=True, read_only=True)
    po_number = serializers.CharField(source='po_receipt.po_number', read_only=True)
    entry_no = serializers.CharField(source='vehicle_entry.entry_no', read_only=True)

    class Meta:
        model = GRPOPosting
        fields = [
            'id',
            'vehicle_entry',
            'entry_no',
            'po_receipt',
            'po_number',
            'sap_doc_entry',
            'sap_doc_num',
            'sap_doc_total',
            'status',
            'error_message',
            'posted_at',
            'posted_by',
            'created_at',
            'lines',
            'attachments',
        ]


class GRPOPostResponseSerializer(serializers.Serializer):
    """Serializer for GRPO post response"""
    success = serializers.BooleanField()
    grpo_posting_id = serializers.IntegerField()
    sap_doc_entry = serializers.IntegerField(allow_null=True)
    sap_doc_num = serializers.IntegerField(allow_null=True)
    sap_doc_total = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    message = serializers.CharField()
    attachments = GRPOAttachmentSerializer(many=True, read_only=True)
