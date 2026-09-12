from rest_framework import serializers

from gate_core.serializers_sales_dispatch import user_display_name
from gate_core.services.sales_dispatch_gatepass import resolved_expected_box_count

from .models import DockingPartialScanRequest, DockingScanSkipRequest


class DockingScanSkipRequestSerializer(serializers.ModelSerializer):
    """Read serializer enriched with docking-entry context for the admin queue."""

    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    entry_no = serializers.SerializerMethodField()
    vehicle_no = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    sap_doc_num = serializers.SerializerMethodField()
    document_type = serializers.SerializerMethodField()
    dispatch_status = serializers.SerializerMethodField()

    class Meta:
        model = DockingScanSkipRequest
        fields = [
            "id",
            "sales_dispatch",
            "entry_no",
            "vehicle_no",
            "customer_name",
            "sap_doc_num",
            "document_type",
            "dispatch_status",
            "reason",
            "status",
            "requested_by",
            "requested_by_name",
            "requested_at",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_requested_by_name(self, obj):
        return user_display_name(obj.requested_by)

    def get_reviewed_by_name(self, obj):
        return user_display_name(obj.reviewed_by)

    def get_entry_no(self, obj):
        return getattr(obj.sales_dispatch, "entry_no", "")

    def get_vehicle_no(self, obj):
        return getattr(obj.sales_dispatch, "vehicle_no", "")

    def get_customer_name(self, obj):
        return getattr(obj.sales_dispatch, "customer_name", "")

    def get_sap_doc_num(self, obj):
        return getattr(obj.sales_dispatch, "sap_doc_num", "")

    def get_document_type(self, obj):
        return getattr(obj.sales_dispatch, "document_type", "")

    def get_dispatch_status(self, obj):
        return getattr(obj.sales_dispatch, "status", "")


class DockingScanSkipRequestCreateSerializer(serializers.Serializer):
    """Operator-side create payload. `sales_dispatch` is the SalesDispatchGateOut id."""

    sales_dispatch = serializers.IntegerField()
    reason = serializers.CharField(trim_whitespace=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A reason is required to request a scanning skip.")
        return value.strip()


class DockingScanSkipReviewSerializer(serializers.Serializer):
    """Approve/reject payload. Notes are optional on approve, required on reject."""

    notes = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True, default="")


class DockingPartialScanRequestSerializer(serializers.ModelSerializer):
    """Read serializer enriched with docking-entry context for the admin queue."""

    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    entry_no = serializers.SerializerMethodField()
    vehicle_no = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    sap_doc_num = serializers.SerializerMethodField()
    document_type = serializers.SerializerMethodField()
    dispatch_status = serializers.SerializerMethodField()
    expected_boxes = serializers.SerializerMethodField()
    company_code = serializers.SerializerMethodField()
    company_name = serializers.SerializerMethodField()

    class Meta:
        model = DockingPartialScanRequest
        fields = [
            "id",
            "sales_dispatch",
            # The BILL this approval covers (null on legacy load-wide rows).
            "document",
            "entry_no",
            "vehicle_no",
            "company_code",
            "company_name",
            "customer_name",
            "sap_doc_num",
            "document_type",
            "dispatch_status",
            "scanned_boxes",
            "expected_boxes",
            "scanned_pieces",
            "expected_pieces",
            "reason",
            "status",
            "requested_by",
            "requested_by_name",
            "requested_at",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_requested_by_name(self, obj):
        return user_display_name(obj.requested_by)

    def get_reviewed_by_name(self, obj):
        return user_display_name(obj.reviewed_by)

    def get_entry_no(self, obj):
        return getattr(obj.sales_dispatch, "entry_no", "")

    def get_vehicle_no(self, obj):
        return getattr(obj.sales_dispatch, "vehicle_no", "")

    def get_customer_name(self, obj):
        # The BILL's customer when the request names one -- on a docking carrying several
        # bills the header customer is just the first of them.
        if obj.document_id:
            return getattr(obj.document, "customer_name", "") or getattr(
                obj.sales_dispatch, "customer_name", ""
            )
        return getattr(obj.sales_dispatch, "customer_name", "")

    def get_sap_doc_num(self, obj):
        """The bill the admin is being asked to approve.

        A per-bill request names its own document; the docking header carries every bill on
        the load ("626090324, 626090325"), which is what left an approver reading a number
        that had nothing to do with the goods that were short.
        """
        if obj.document_id:
            return getattr(obj.document, "sap_doc_num", "") or str(
                getattr(obj.document, "sap_doc_entry", "") or ""
            )
        return getattr(obj.sales_dispatch, "sap_doc_num", "")

    def get_company_code(self, obj):
        return getattr(obj.company, "code", "")

    def get_company_name(self, obj):
        # A cross-company truck raises each bill's request in that bill's own company, so
        # the queue has to say whose bill it is.
        return getattr(obj.company, "name", "")

    def get_document_type(self, obj):
        return getattr(obj.sales_dispatch, "document_type", "")

    def get_dispatch_status(self, obj):
        return getattr(obj.sales_dispatch, "status", "")

    def get_expected_boxes(self, obj):
        # The stored figure is what the operator's screen showed when the request was
        # raised: this BILL's expected boxes for a per-bill request, the whole truck's for a
        # legacy load-wide one. Recomputing would contradict it (and read 0 for a bill that
        # ships entirely loose). Older rows saved 0, before the item quantity/pack-size
        # fallback existed; those still resolve live, against the docking they cover.
        if obj.expected_boxes:
            return obj.expected_boxes
        if obj.document_id is None and obj.sales_dispatch_id:
            resolved = resolved_expected_box_count(obj.sales_dispatch)
            if resolved:
                return resolved
        return obj.expected_boxes


class DockingPartialScanRequestCreateSerializer(serializers.Serializer):
    """Operator-side create payload. `sales_dispatch` is the SalesDispatchGateOut id;
    the scanned/expected box counts are resolved server-side from the actual scans."""

    sales_dispatch = serializers.IntegerField()
    reason = serializers.CharField(trim_whitespace=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A reason is required to dispatch with a partial box scan."
            )
        return value.strip()
