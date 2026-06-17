from rest_framework import serializers

from .models import (
    DispatchPlan,
    DispatchPlanStatus,
    TransporterAPInvoiceAttachment,
    TransporterAPInvoiceLine,
    TransporterAPInvoicePosting,
)

MONTH_INPUT_FORMATS = ["%Y-%m", "%Y-%m-%d"]


class DispatchBillFilterSerializer(serializers.Serializer):
    STATUS_CHOICES = [("all", "All")] + list(DispatchPlanStatus.choices)

    date_from = serializers.DateField(required=True, input_formats=["%Y-%m-%d"])
    date_to = serializers.DateField(required=True, input_formats=["%Y-%m-%d"])
    booking_status = serializers.ChoiceField(
        choices=STATUS_CHOICES,
        default="all",
        required=False,
    )
    search = serializers.CharField(required=False, max_length=120, allow_blank=True)
    branch = serializers.CharField(required=False, max_length=80, allow_blank=True)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=2000)
    exclude_jivo_mart_transfer = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError("date_from must be before or equal to date_to.")
        return attrs


class DispatchScheduleFilterSerializer(serializers.Serializer):
    """Optional filters for the read-only warehouse Dispatch Schedule."""

    STATUS_CHOICES = [("all", "All")] + list(DispatchPlanStatus.choices)

    date_from = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])
    date_to = serializers.DateField(required=False, input_formats=["%Y-%m-%d"])
    booking_status = serializers.ChoiceField(
        choices=STATUS_CHOICES,
        default="all",
        required=False,
    )
    search = serializers.CharField(required=False, max_length=120, allow_blank=True)

    def validate(self, attrs):
        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError(
                "date_from must be before or equal to date_to."
            )
        return attrs


class DispatchScheduleSerializer(serializers.ModelSerializer):
    """Lean, read-only view of a scheduled dispatch plan for warehouse staff."""

    class Meta:
        model = DispatchPlan
        fields = [
            "id",
            "dispatch_date",
            "booking_status",
            "priority",
            "sap_invoice_doc_num",
            "invoice_number",
            "place_of_supply",
            "budget_delivery_point",
            "product_variety",
            "vehicle_no",
            "transporter_name",
            "driver_name",
            "driver_mobile_no",
            "total_litres",
            "invoice_weight",
            "kanta_weight",
            "remarks",
        ]
        read_only_fields = fields


class DispatchPlanSerializer(serializers.ModelSerializer):
    vehicle_id = serializers.IntegerField(read_only=True, allow_null=True)
    transporter_id = serializers.IntegerField(read_only=True, allow_null=True)
    driver_id = serializers.IntegerField(read_only=True, allow_null=True)
    linked_vehicle_entry_id = serializers.IntegerField(read_only=True, allow_null=True)
    effective_month = serializers.DateField(
        allow_null=True,
        format="%Y-%m",
        read_only=True,
    )

    class Meta:
        model = DispatchPlan
        fields = [
            "id",
            "sap_invoice_doc_entry",
            "sap_invoice_doc_num",
            "invoice_number",
            "eway_bill",
            "invoice_weight",
            "invoice_amount",
            "place_of_supply",
            "product_variety",
            "total_litres",
            "effective_month",
            "budget_delivery_point",
            "service_location_code",
            "service_location_name",
            "sac_entry",
            "sac_code",
            "vehicle_id",
            "transporter_id",
            "driver_id",
            "linked_vehicle_entry_id",
            "booking_status",
            "dispatch_date",
            "priority",
            "transporter_name",
            "transporter_gstin",
            "contact_person",
            "mobile_no",
            "vehicle_no",
            "driver_name",
            "driver_mobile_no",
            "driver_license_no",
            "driver_id_proof_type",
            "driver_id_proof_number",
            "bilty_no",
            "bilty_date",
            "bilty_attachment",
            "bilty_attachment_name",
            "freight",
            "total_freight",
            "kanta_weight",
            "remarks",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CommaSeparatedIntegerListField(serializers.ListField):
    """Accept JSON arrays and multipart comma-separated/repeated values."""

    def to_internal_value(self, data):
        if data == "":
            data = []
        elif isinstance(data, str):
            data = self._split_value(data)
        elif isinstance(data, (list, tuple)):
            values = []
            for item in data:
                if item == "":
                    continue
                if isinstance(item, str):
                    values.extend(self._split_value(item))
                else:
                    values.append(item)
            data = values

        return super().to_internal_value(data)

    @staticmethod
    def _split_value(value: str):
        return [item.strip() for item in value.split(",") if item.strip()]


class DispatchPlanUpdateSerializer(serializers.Serializer):
    sap_invoice_doc_num = serializers.CharField(
        required=False, max_length=30, allow_blank=True
    )
    linked_invoice_doc_entries = CommaSeparatedIntegerListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
        write_only=True,
    )
    invoice_number = serializers.CharField(required=False, max_length=50, allow_blank=True)
    eway_bill = serializers.CharField(required=False, max_length=80, allow_blank=True)
    invoice_weight = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=18,
        decimal_places=3,
    )
    invoice_amount = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=18,
        decimal_places=2,
    )
    place_of_supply = serializers.CharField(
        required=False,
        max_length=150,
        allow_blank=True,
    )
    product_variety = serializers.CharField(required=False, max_length=50, allow_blank=True)
    total_litres = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=3
    )
    effective_month = serializers.DateField(
        required=False, allow_null=True, input_formats=MONTH_INPUT_FORMATS
    )
    budget_delivery_point = serializers.CharField(
        required=False, max_length=100, allow_blank=True
    )
    service_location_code = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    service_location_name = serializers.CharField(
        required=False, max_length=100, allow_blank=True
    )
    sac_entry = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    sac_code = serializers.CharField(required=False, max_length=30, allow_blank=True)
    vehicle_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    transporter_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    driver_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    linked_vehicle_entry_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    booking_status = serializers.ChoiceField(
        choices=DispatchPlanStatus.choices,
        required=False,
    )
    dispatch_date = serializers.DateField(
        required=False, allow_null=True, input_formats=["%Y-%m-%d"]
    )
    priority = serializers.CharField(required=False, max_length=50, allow_blank=True)
    transporter_name = serializers.CharField(
        required=False, max_length=150, allow_blank=True
    )
    transporter_gstin = serializers.CharField(
        required=False, max_length=20, allow_blank=True
    )
    contact_person = serializers.CharField(
        required=False, max_length=100, allow_blank=True
    )
    mobile_no = serializers.CharField(required=False, max_length=50, allow_blank=True)
    vehicle_no = serializers.CharField(required=False, max_length=30, allow_blank=True)
    driver_name = serializers.CharField(required=False, max_length=100, allow_blank=True)
    driver_mobile_no = serializers.CharField(required=False, max_length=50, allow_blank=True)
    driver_license_no = serializers.CharField(required=False, max_length=50, allow_blank=True)
    driver_id_proof_type = serializers.CharField(
        required=False, max_length=50, allow_blank=True
    )
    driver_id_proof_number = serializers.CharField(
        required=False, max_length=50, allow_blank=True
    )
    bilty_no = serializers.CharField(required=False, max_length=50, allow_blank=True)
    bilty_date = serializers.DateField(
        required=False, allow_null=True, input_formats=["%Y-%m-%d"]
    )
    bilty_attachment = serializers.FileField(required=False, allow_null=True)
    freight = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=2
    )
    total_freight = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=2
    )
    kanta_weight = serializers.DecimalField(
        required=False, allow_null=True, max_digits=18, decimal_places=3
    )
    remarks = serializers.CharField(required=False, allow_blank=True)


class DispatchBillSerializer(serializers.Serializer):
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    doc_date = serializers.CharField(allow_null=True)
    create_date = serializers.CharField(allow_null=True)
    create_time = serializers.CharField(allow_blank=True)
    card_code = serializers.CharField()
    card_name = serializers.CharField()
    doc_total = serializers.FloatField()
    branch_id = serializers.IntegerField(allow_null=True)
    branch_name = serializers.CharField()
    ship_to_code = serializers.CharField()
    ship_to_address = serializers.CharField()
    state = serializers.CharField()
    city = serializers.CharField()
    bp_gstin = serializers.CharField()
    sap_dispatch_date = serializers.CharField(allow_null=True)
    sap_bilty_no = serializers.CharField()
    sap_bilty_date = serializers.CharField(allow_null=True)
    sap_transporter_name = serializers.CharField()
    sap_vehicle_no = serializers.CharField()
    sap_transporter_invoice = serializers.CharField()
    sap_lr_number = serializers.CharField()
    sap_eway_bill = serializers.CharField()
    gst_vehicle_no = serializers.CharField()
    gst_transport_date = serializers.CharField(allow_null=True)
    gst_transport_reason = serializers.CharField()
    line_count = serializers.IntegerField()
    total_quantity = serializers.FloatField()
    total_litres = serializers.FloatField()
    total_boxes = serializers.FloatField()
    total_weight = serializers.FloatField()
    total_line_amount = serializers.FloatField()
    total_gross_amount = serializers.FloatField()
    warehouses = serializers.CharField()
    item_summary = serializers.CharField()
    base_refs = serializers.CharField()
    plan = DispatchPlanSerializer(allow_null=True)


class DispatchBillLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField()
    item_code = serializers.CharField(allow_blank=True)
    item_name = serializers.CharField(allow_blank=True)
    quantity = serializers.FloatField()
    uom = serializers.CharField(allow_blank=True)
    rate = serializers.FloatField()
    line_total = serializers.FloatField()
    gross_total = serializers.FloatField()
    warehouse_code = serializers.CharField(allow_blank=True)
    base_ref = serializers.CharField(allow_blank=True)
    base_entry = serializers.IntegerField(allow_null=True)
    base_type = serializers.IntegerField(allow_null=True)
    tax_code = serializers.CharField(allow_blank=True)
    total_litres = serializers.FloatField()
    total_boxes = serializers.FloatField()
    total_weight = serializers.FloatField()


class DispatchBillDetailSerializer(DispatchBillSerializer):
    items = DispatchBillLineSerializer(many=True)


class DispatchPlansMetaSerializer(serializers.Serializer):
    total_bills = serializers.IntegerField()
    pending_count = serializers.IntegerField()
    booked_count = serializers.IntegerField()
    dispatched_count = serializers.IntegerField()
    cancelled_count = serializers.IntegerField()
    total_doc_value = serializers.FloatField()
    total_litres = serializers.FloatField()
    total_boxes = serializers.FloatField()
    fetched_at = serializers.CharField()


class DispatchBillListResponseSerializer(serializers.Serializer):
    data = DispatchBillSerializer(many=True)
    meta = DispatchPlansMetaSerializer()


class OpenBiltySerializer(serializers.Serializer):
    service_grpo_posting_id = serializers.IntegerField()
    dispatch_plan_id = serializers.IntegerField()
    sap_invoice_doc_entry = serializers.IntegerField()
    sap_invoice_doc_num = serializers.CharField(allow_blank=True)
    dispatch_date = serializers.DateField(allow_null=True)
    vehicle_no = serializers.CharField(allow_blank=True)
    driver_name = serializers.CharField(allow_blank=True)
    transporter_id = serializers.IntegerField(allow_null=True)
    transporter_name = serializers.CharField(allow_blank=True)
    vendor_code = serializers.CharField()
    vendor_name = serializers.CharField(allow_blank=True)
    branch_id = serializers.IntegerField(allow_null=True)
    bilty_no = serializers.CharField(allow_blank=True)
    bilty_date = serializers.DateField(allow_null=True)
    grpo_doc_entry = serializers.IntegerField()
    grpo_doc_num = serializers.IntegerField(allow_null=True)
    grpo_doc_total = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        allow_null=True,
    )
    posted_at = serializers.DateTimeField(allow_null=True)
    line_count = serializers.IntegerField()


class TransporterAPInvoicePreviewRequestSerializer(serializers.Serializer):
    service_grpo_posting_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        min_length=1,
    )
    vendor_code = serializers.CharField(required=False, max_length=50, allow_blank=True)
    branch_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_service_grpo_posting_ids(self, value):
        deduped = list(dict.fromkeys(value))
        if len(deduped) != len(value):
            raise serializers.ValidationError("Duplicate bilty GRPO selections are not allowed.")
        return value


class TransporterAPInvoicePostRequestSerializer(
    TransporterAPInvoicePreviewRequestSerializer
):
    invoice_number = serializers.CharField(max_length=100)
    invoice_date = serializers.DateField(required=False, allow_null=True)
    invoice_amount = serializers.DecimalField(max_digits=18, decimal_places=2)
    doc_date = serializers.DateField(required=False, allow_null=True)
    doc_due_date = serializers.DateField(required=False, allow_null=True)
    tax_date = serializers.DateField(required=False, allow_null=True)
    comments = serializers.CharField(required=False, allow_blank=True)

    def validate_invoice_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Invoice amount must be greater than zero.")
        return value


class TransporterAPInvoiceSubmitRequestSerializer(TransporterAPInvoicePostRequestSerializer):
    pass


class TransporterAPInvoiceSAPPostRequestSerializer(serializers.Serializer):
    doc_date = serializers.DateField(required=False, allow_null=True)
    doc_due_date = serializers.DateField(required=False, allow_null=True)
    tax_date = serializers.DateField(required=False, allow_null=True)
    comments = serializers.CharField(required=False, allow_blank=True)


class TransporterAPInvoicePreviewLineSerializer(serializers.Serializer):
    service_grpo_posting_id = serializers.IntegerField()
    service_grpo_line_id = serializers.IntegerField(allow_null=True)
    dispatch_plan_id = serializers.IntegerField()
    bilty_no = serializers.CharField(allow_blank=True)
    grpo_doc_entry = serializers.IntegerField()
    grpo_doc_num = serializers.IntegerField(allow_null=True)
    grpo_line_num = serializers.IntegerField()
    service_description = serializers.CharField(allow_blank=True)
    line_total = serializers.DecimalField(max_digits=18, decimal_places=2)
    tax_code = serializers.CharField(allow_blank=True)
    gl_account = serializers.CharField(allow_blank=True)


class TransporterAPInvoicePreviewSerializer(serializers.Serializer):
    vendor_code = serializers.CharField()
    vendor_name = serializers.CharField(allow_blank=True)
    branch_id = serializers.IntegerField()
    selected_grpo_total = serializers.DecimalField(max_digits=18, decimal_places=2)
    tolerance = serializers.DecimalField(max_digits=18, decimal_places=2)
    lines = TransporterAPInvoicePreviewLineSerializer(many=True)


class TransporterAPInvoiceLineSerializer(serializers.ModelSerializer):
    service_grpo_posting_id = serializers.IntegerField(read_only=True)
    dispatch_plan_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = TransporterAPInvoiceLine
        fields = [
            "id",
            "service_grpo_posting_id",
            "dispatch_plan_id",
            "base_entry",
            "base_line",
            "base_doc_num",
            "bilty_no",
            "service_description",
            "line_total",
            "tax_code",
            "gl_account",
        ]


class TransporterAPInvoiceAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransporterAPInvoiceAttachment
        fields = [
            "id",
            "file",
            "original_filename",
            "sap_attachment_status",
            "sap_absolute_entry",
            "sap_error_message",
            "uploaded_at",
            "uploaded_by",
        ]
        read_only_fields = [
            "id",
            "original_filename",
            "sap_attachment_status",
            "sap_absolute_entry",
            "sap_error_message",
            "uploaded_at",
            "uploaded_by",
        ]


class TransporterAPInvoicePostingSerializer(serializers.ModelSerializer):
    lines = TransporterAPInvoiceLineSerializer(many=True, read_only=True)
    attachments = TransporterAPInvoiceAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = TransporterAPInvoicePosting
        fields = [
            "id",
            "company",
            "vendor_code",
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "invoice_amount",
            "selected_grpo_total",
            "amount_difference",
            "branch_id",
            "sap_doc_entry",
            "sap_doc_num",
            "sap_doc_total",
            "status",
            "error_message",
            "posted_at",
            "posted_by",
            "created_at",
            "updated_at",
            "comments",
            "lines",
            "attachments",
        ]
        read_only_fields = fields


class TransporterAPInvoicePostResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    transporter_ap_invoice_posting_id = serializers.IntegerField()
    sap_doc_entry = serializers.IntegerField(allow_null=True)
    sap_doc_num = serializers.IntegerField(allow_null=True)
    sap_doc_total = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        allow_null=True,
    )
    message = serializers.CharField()
    posting = TransporterAPInvoicePostingSerializer()
