"""Serializers for the A/R invoice module.

The create endpoint is multipart (a ``data`` JSON field plus optional
``attachments`` files) or plain JSON when there are no files — the same
convention as ``ap_invoice``. SO open-line reads come straight from SAP and
are passed through unserialized.
"""
from rest_framework import serializers

from .models import (
    ARInvoiceAttachment,
    ARInvoiceLine,
    ARInvoicePayment,
    ARInvoicePosting,
    ARPaymentStatus,
)


class ARInvoiceLineKeySerializer(serializers.Serializer):
    so_doc_entry = serializers.IntegerField(min_value=1)
    line_num = serializers.IntegerField(min_value=0)


class ARDirectLineSerializer(serializers.Serializer):
    """A free line of a direct (cash/counter) sale."""

    item_code = serializers.CharField(max_length=50)
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=255, default=""
    )
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3, min_value=0)
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=0)
    tax_code = serializers.CharField(max_length=50)
    warehouse_code = serializers.CharField(max_length=20)


class ARInvoiceCreateSerializer(serializers.Serializer):
    """Create body: either ``lines`` (open Sales Order references) or
    ``direct_lines`` (free cash-sale lines) — exactly one of the two."""

    customer_code = serializers.CharField(max_length=50)
    lines = ARInvoiceLineKeySerializer(many=True, required=False, default=list)
    direct_lines = ARDirectLineSerializer(many=True, required=False, default=list)
    customer_ref = serializers.CharField(
        required=False, allow_blank=True, max_length=100, default=""
    )
    doc_date = serializers.DateField(required=False, allow_null=True)
    doc_due_date = serializers.DateField(required=False, allow_null=True)
    tax_date = serializers.DateField(required=False, allow_null=True)
    comments = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, default=""
    )

    def validate(self, attrs):
        has_so = bool(attrs.get("lines"))
        has_direct = bool(attrs.get("direct_lines"))
        if has_so == has_direct:
            raise serializers.ValidationError(
                "Provide either Sales Order lines or direct-sale lines (not both)."
            )
        return attrs


class WarehouseItemsQuerySerializer(serializers.Serializer):
    warehouse = serializers.CharField(max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, default="")


class LineDefaultsQuerySerializer(serializers.Serializer):
    customer_code = serializers.CharField(max_length=50)
    item_code = serializers.CharField(max_length=50)


class OpenSOLinesQuerySerializer(serializers.Serializer):
    customer_code = serializers.CharField(max_length=50)
    search = serializers.CharField(required=False, allow_blank=True, default="")


class CustomerSearchQuerySerializer(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, default="")


class CustomerCreditQuerySerializer(serializers.Serializer):
    customer_code = serializers.CharField(max_length=50)


class SapCashSaleQuerySerializer(serializers.Serializer):
    """Filters for the SAP-side cash-sale history (all optional)."""

    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)
    search = serializers.CharField(required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=1000, default=500
    )

    def validate(self, attrs):
        date_from, date_to = attrs.get("date_from"), attrs.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise serializers.ValidationError(
                "The 'from' date cannot be after the 'to' date."
            )
        return attrs


class ARInvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = ARInvoiceLine
        fields = [
            "id", "base_entry", "base_line", "base_doc_num", "item_code",
            "description", "quantity", "price", "line_total", "tax_code",
            "warehouse_code", "cost_center",
        ]


class ARInvoiceAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = ARInvoiceAttachment
        fields = [
            "id", "original_filename", "sap_attachment_status",
            "sap_absolute_entry", "sap_error_message", "uploaded_at", "file_url",
        ]

    def get_file_url(self, obj):
        request = self.context.get("request")
        if not obj.file:
            return None
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


class ARInvoicePaymentSerializer(serializers.ModelSerializer):
    """One invoice's payment-received mark, as History shows it."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    mode_display = serializers.CharField(source="get_mode_display", read_only=True)
    marked_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ARInvoicePayment
        fields = [
            "id", "sap_doc_entry", "sap_doc_num", "ar_invoice",
            "status", "status_display", "received_on", "amount",
            "mode", "mode_display", "reference", "remarks",
            "marked_by_name", "updated_at",
        ]

    def get_marked_by_name(self, obj):
        user = obj.updated_by or obj.created_by
        if not user:
            return None
        return getattr(user, "full_name", "") or user.get_username()


class ARInvoicePaymentWriteSerializer(serializers.Serializer):
    """Body of the mark-payment call.

    ``amount`` is what actually came in, so a PARTIAL mark without one says
    nothing; and nothing is "received" without the day it was received.
    """

    status = serializers.ChoiceField(choices=ARPaymentStatus.choices)
    received_on = serializers.DateField(required=False, allow_null=True)
    amount = serializers.DecimalField(
        max_digits=18, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    mode = serializers.CharField(
        required=False, allow_blank=True, max_length=20, default=""
    )
    reference = serializers.CharField(
        required=False, allow_blank=True, max_length=100, default=""
    )
    remarks = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, default=""
    )

    def validate_mode(self, value):
        from .models import ARPaymentMode

        value = (value or "").strip().upper()
        if value and value not in ARPaymentMode.values:
            raise serializers.ValidationError(
                f"Unknown payment mode. Use one of: {', '.join(ARPaymentMode.values)}."
            )
        return value

    def validate(self, attrs):
        status = attrs["status"]
        if status == ARPaymentStatus.PENDING:
            return attrs
        if not attrs.get("received_on"):
            raise serializers.ValidationError(
                {"received_on": "Say which day the payment was received."}
            )
        if status == ARPaymentStatus.PARTIAL and not attrs.get("amount"):
            raise serializers.ValidationError(
                {"amount": "A part payment needs the amount that came in."}
            )
        return attrs


class ARInvoicePostingSerializer(serializers.ModelSerializer):
    lines = ARInvoiceLineSerializer(many=True, read_only=True)
    attachments = ARInvoiceAttachmentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    posted_by_name = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()

    class Meta:
        model = ARInvoicePosting
        fields = [
            "id", "customer_code", "customer_name", "customer_ref",
            "doc_date", "doc_due_date", "tax_date",
            "selected_total", "branch_id", "comments",
            "status", "status_display", "error_message",
            "sap_draft_entry", "sap_approval_code", "approval_remarks",
            "sap_doc_entry", "sap_doc_num", "sap_doc_total",
            "posted_at", "created_at", "created_by_name", "posted_by_name",
            "lines", "attachments", "payment",
        ]

    @staticmethod
    def _name(user):
        if not user:
            return None
        return getattr(user, "full_name", "") or user.get_username()

    def get_created_by_name(self, obj):
        return self._name(obj.created_by)

    def get_posted_by_name(self, obj):
        return self._name(obj.posted_by)

    def get_payment(self, obj):
        """The payment mark, or None while the bill is untracked.

        Read off the prefetched relation rather than queried per row — History
        renders every invoice this company ever raised.
        """
        payment = next(iter(obj.payments.all()), None)
        return ARInvoicePaymentSerializer(payment).data if payment else None
