from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from gate_core.models import (
    SalesDispatchAdditionalWeight,
    SalesDispatchAttachment,
    SalesDispatchAttachmentType,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGatepassPrintLog,
    SalesDispatchLock,
)
from gate_core.services.sales_dispatch_gatepass import get_gatepass_readiness


class SalesDispatchDocumentSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(choices=SalesDispatchDocumentType.choices)
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField(allow_blank=True)
    doc_date = serializers.DateField(allow_null=True, required=False)
    doc_total = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        allow_null=True,
        required=False,
    )
    branch_id = serializers.IntegerField(allow_null=True, required=False)
    branch_name = serializers.CharField(allow_blank=True, required=False)
    card_code = serializers.CharField(allow_blank=True, required=False)
    card_name = serializers.CharField(allow_blank=True, required=False)
    ship_to_code = serializers.CharField(allow_blank=True, required=False)
    ship_to_address = serializers.CharField(allow_blank=True, required=False)
    place_of_supply = serializers.CharField(allow_blank=True, required=False)
    bp_gstin = serializers.CharField(allow_blank=True, required=False)
    eway_bill = serializers.CharField(allow_blank=True, required=False)
    vehicle_no = serializers.CharField(allow_blank=True, required=False)
    transporter_name = serializers.CharField(allow_blank=True, required=False)
    bilty_no = serializers.CharField(allow_blank=True, required=False)
    bilty_date = serializers.DateField(allow_null=True, required=False)
    from_warehouse = serializers.CharField(allow_blank=True, required=False)
    to_warehouse = serializers.CharField(allow_blank=True, required=False)
    warehouses = serializers.CharField(allow_blank=True, required=False)
    item_summary = serializers.CharField(allow_blank=True, required=False)
    base_refs = serializers.CharField(allow_blank=True, required=False)
    total_quantity = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        allow_null=True,
        required=False,
    )
    total_litres = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        allow_null=True,
        required=False,
    )
    total_boxes = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        allow_null=True,
        required=False,
    )
    total_weight = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        allow_null=True,
        required=False,
    )
    line_count = serializers.IntegerField(required=False)
    items = serializers.ListField(required=False)
    plan = serializers.JSONField(allow_null=True, required=False)


class SalesDispatchGateOutItemSerializer(serializers.ModelSerializer):
    document_sap_doc_num = serializers.CharField(source="document.sap_doc_num", read_only=True)

    class Meta:
        model = SalesDispatchGateOutItem
        fields = [
            "id",
            "document",
            "document_sap_doc_num",
            "line_num",
            "item_code",
            "item_name",
            "quantity",
            "uom",
            "rate",
            "line_total",
            "gross_total",
            "warehouse_code",
            "from_warehouse",
            "to_warehouse",
            "base_ref",
            "base_entry",
            "base_type",
            "tax_code",
            "total_litres",
            "total_boxes",
            "total_weight",
        ]
        read_only_fields = fields


class SalesDispatchGateOutDocumentSerializer(serializers.ModelSerializer):
    items = SalesDispatchGateOutItemSerializer(many=True, read_only=True)

    class Meta:
        model = SalesDispatchGateOutDocument
        fields = [
            "id",
            "dispatch_plan",
            "document_type",
            "sap_doc_entry",
            "sap_doc_num",
            "sap_doc_date",
            "sap_doc_total",
            "sap_branch_id",
            "sap_branch_name",
            "sap_reference",
            "sap_comments",
            "customer_code",
            "customer_name",
            "ship_to_code",
            "ship_to_address",
            "place_of_supply",
            "bp_gstin",
            "eway_bill",
            "from_warehouse",
            "to_warehouse",
            "warehouses",
            "item_summary",
            "base_refs",
            "total_quantity",
            "total_litres",
            "total_boxes",
            "total_weight",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class SalesDispatchAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchAttachment
        fields = [
            "id",
            "attachment_type",
            "file",
            "original_filename",
            "latitude",
            "longitude",
            "notes",
            "uploaded_by",
            "uploaded_by_name",
            "uploaded_at",
        ]
        read_only_fields = [
            "id",
            "original_filename",
            "uploaded_by",
            "uploaded_by_name",
            "uploaded_at",
        ]

    def get_uploaded_by_name(self, obj):
        return user_display_name(obj.uploaded_by)


class SalesDispatchBoxScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.SerializerMethodField()
    document_sap_doc_num = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchBoxScan
        fields = [
            "id",
            "sales_dispatch",
            "document",
            "document_sap_doc_num",
            "box",
            "scan_log",
            "box_barcode",
            "barcode_raw",
            "item_code",
            "item_name",
            "batch_number",
            "quantity",
            "uom",
            "net_weight",
            "gross_weight",
            "box_status",
            "warehouse_code",
            "pallet_code",
            "scanned_by",
            "scanned_by_name",
            "scanned_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_scanned_by_name(self, obj):
        return user_display_name(obj.scanned_by)

    def get_document_sap_doc_num(self, obj):
        return obj.document.sap_doc_num if obj.document_id else ""


class SalesDispatchBoxScanDetailSerializer(SalesDispatchBoxScanSerializer):
    """Box-scan payload for the docking *detail* read.

    Drops ``scanned_by_name`` (the frontend never renders it). That field is the
    only reason the detail queryset would join ``accounts_user`` per scan, so
    omitting it lets the detail view prefetch box scans without that join — a
    load with hundreds of boxes no longer pulls a full user row per scan. The
    standalone ``/box-scans/`` endpoint keeps the full serializer.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("scanned_by_name", None)


class SalesDispatchLockSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchLock
        fields = [
            "id",
            "company",
            "is_locked",
            "reason",
            "changed_by",
            "changed_by_name",
            "changed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_changed_by_name(self, obj):
        return user_display_name(obj.changed_by)


def user_display_name(user):
    if not user:
        return ""
    get_full_name = getattr(user, "get_full_name", None)
    if callable(get_full_name):
        return get_full_name() or getattr(user, "username", "") or str(user)
    return getattr(user, "full_name", "") or getattr(user, "username", "") or str(user)


class SalesDispatchLockUpdateSerializer(serializers.Serializer):
    is_locked = serializers.BooleanField()
    reason = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)

    def validate(self, attrs):
        if attrs["is_locked"] and not attrs.get("reason", "").strip():
            raise serializers.ValidationError(
                {"reason": "Reason is required when locking Docking."}
            )
        return attrs


class SalesDispatchGatepassPrintLogSerializer(serializers.ModelSerializer):
    printed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchGatepassPrintLog
        fields = [
            "id",
            "sales_dispatch",
            "gatepass_no",
            "entry_status",
            "copy_number",
            "print_type",
            "reprint_reason",
            "printed_by",
            "printed_by_name",
            "printed_at",
            "printer_name",
            "ip_address",
            "user_agent",
        ]
        read_only_fields = fields

    def get_printed_by_name(self, obj):
        return user_display_name(obj.printed_by)


class SalesDispatchAdditionalWeightSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchAdditionalWeight
        fields = [
            "id",
            "sales_dispatch",
            "name",
            "weight",
            "recorded_by_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_recorded_by_name(self, obj):
        return user_display_name(obj.created_by)


class SalesDispatchAdditionalWeightItemSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    weight = serializers.DecimalField(
        max_digits=12,
        decimal_places=3,
        min_value=Decimal("0"),
    )


class SalesDispatchAdditionalWeightSetSerializer(serializers.Serializer):
    """Replaces the full list of additional-weight line items for an entry."""

    items = SalesDispatchAdditionalWeightItemSerializer(many=True)


class SalesDispatchGateOutSerializer(serializers.ModelSerializer):
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    items = SalesDispatchGateOutItemSerializer(many=True, read_only=True)
    documents = SalesDispatchGateOutDocumentSerializer(many=True, read_only=True)
    attachments = SalesDispatchAttachmentSerializer(many=True, read_only=True)
    box_scans = serializers.SerializerMethodField()
    additional_weights = serializers.SerializerMethodField()
    gatepass_print_logs = SalesDispatchGatepassPrintLogSerializer(many=True, read_only=True)
    gatepass_readiness = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    document_numbers = serializers.SerializerMethodField()
    primary_document = serializers.SerializerMethodField()
    dispatch_date = serializers.SerializerMethodField()
    gross_weight = serializers.SerializerMethodField()
    tare_weight = serializers.SerializerMethodField()
    net_weight = serializers.SerializerMethodField()
    weighbridge_slip_no = serializers.SerializerMethodField()
    first_weighment_time = serializers.SerializerMethodField()
    second_weighment_time = serializers.SerializerMethodField()
    challan_weight_by_name = serializers.SerializerMethodField()
    arrival = serializers.IntegerField(source="arrival_id", read_only=True)
    arrival_no = serializers.CharField(
        source="arrival.arrival_no", read_only=True, allow_null=True
    )
    arrival_status = serializers.SerializerMethodField()
    arrival_company_count = serializers.SerializerMethodField()
    arrival_can_depart = serializers.SerializerMethodField()
    gatepass_print_locked = serializers.SerializerMethodField()
    gatepass_lock_reason = serializers.SerializerMethodField()

    class Meta:
        model = SalesDispatchGateOut
        fields = [
            "id",
            "entry_no",
            "company",
            "company_code",
            "company_name",
            "arrival",
            "arrival_no",
            "arrival_status",
            "arrival_company_count",
            "arrival_can_depart",
            "gatepass_print_locked",
            "gatepass_lock_reason",
            "vehicle_entry",
            "vehicle_entry_no",
            "vehicle_entry_status",
            "dispatch_plan",
            "vehicle",
            "transporter",
            "driver",
            "dispatch_date",
            "documents",
            "document_count",
            "document_numbers",
            "primary_document",
            "document_type",
            "sap_doc_entry",
            "sap_doc_num",
            "sap_doc_date",
            "sap_doc_total",
            "sap_branch_id",
            "sap_branch_name",
            "sap_reference",
            "sap_comments",
            "customer_code",
            "customer_name",
            "ship_to_code",
            "ship_to_address",
            "place_of_supply",
            "bp_gstin",
            "eway_bill",
            "from_warehouse",
            "to_warehouse",
            "warehouses",
            "item_summary",
            "base_refs",
            "total_quantity",
            "total_litres",
            "total_boxes",
            "total_weight",
            "challan_weight",
            "challan_weight_at",
            "challan_weight_by",
            "challan_weight_by_name",
            "vehicle_no",
            "transporter_name",
            "transporter_gstin",
            "transporter_contact_person",
            "transporter_mobile_no",
            "driver_name",
            "driver_mobile_no",
            "driver_license_no",
            "driver_id_proof_type",
            "driver_id_proof_number",
            "bilty_no",
            "bilty_date",
            "freight",
            "total_freight",
            "dock_incharge",
            "docked_at",
            "gate_out_date",
            "out_time",
            "security_name",
            "truck_photo",
            "photo_latitude",
            "photo_longitude",
            "photo_uploaded_by",
            "photo_uploaded_at",
            "gatepass_no",
            "random_code",
            "qr_payload",
            "uom",
            "physical_quantity",
            "seal_number",
            "pgi_reference",
            "printed_by",
            "printed_at",
            "print_committed_by",
            "print_committed_at",
            "dispatched_by",
            "dispatched_at",
            "status",
            "remarks",
            "reject_reason",
            "rejected_by",
            "rejected_at",
            "cancel_reason",
            "cancelled_by",
            "cancelled_at",
            "gross_weight",
            "tare_weight",
            "net_weight",
            "weighbridge_slip_no",
            "first_weighment_time",
            "second_weighment_time",
            "gatepass_readiness",
            "items",
            "attachments",
            "box_scans",
            "additional_weights",
            "gatepass_print_logs",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_dispatch_date(self, obj):
        return getattr(obj.dispatch_plan, "dispatch_date", None)

    def get_arrival_status(self, obj):
        # The physical truck's real in/out state lives on the arrival, not the
        # per-company docking -- so a "dispatched" docking still shows its truck
        # as inside/loading until the whole arrival departs.
        return obj.arrival.status if obj.arrival_id else None

    def get_arrival_company_count(self, obj):
        # >1 means a multi-company truck: dispatching this docking dispatches the
        # whole truck (the FE shows readiness across all companies, not a redirect).
        if not obj.arrival_id:
            return 0
        return len({gi.company_id for gi in obj.arrival.gate_ins.all() if gi.is_active})

    def _lock_for(self, obj):
        # The gatepass-print lock that matters is the DOCKING's company's, not the
        # active Company-Code -- read-only + memoized so the cross-company board
        # never does a write or an N+1 (companies on a board are few).
        if not hasattr(self, "_lock_cache"):
            self._lock_cache = {}
        if obj.company_id not in self._lock_cache:
            from gate_core.models import SalesDispatchLock

            lock = SalesDispatchLock.objects.filter(company_id=obj.company_id).first()
            locked = bool(lock and lock.is_locked)
            self._lock_cache[obj.company_id] = (
                locked,
                (lock.reason if locked else "") or "",
            )
        return self._lock_cache[obj.company_id]

    def get_gatepass_print_locked(self, obj):
        return self._lock_for(obj)[0]

    def get_gatepass_lock_reason(self, obj):
        return self._lock_for(obj)[1]

    def get_arrival_can_depart(self, obj):
        # True once every company on the truck is dispatched (all gate-ins retired)
        # and it hasn't left yet -- the FE shows an inline Depart (single exit).
        from gate_core.models import VehicleArrivalStatus

        arrival = obj.arrival
        if not obj.arrival_id or arrival is None:
            return False
        if arrival.status == VehicleArrivalStatus.DEPARTED:
            return False
        return not any(
            gi.is_active and gi.retired_at is None for gi in arrival.gate_ins.all()
        )

    def get_gatepass_readiness(self, obj):
        return get_gatepass_readiness(obj)

    def get_document_count(self, obj):
        # Read from the prefetch cache (``.all()``); ``.count()`` would re-query per row.
        return len(obj.documents.all()) or 1

    def get_document_numbers(self, obj):
        documents = list(obj.documents.all())
        numbers = [document.sap_doc_num for document in documents if document.sap_doc_num]
        return numbers or ([obj.sap_doc_num] if obj.sap_doc_num else [])

    def get_primary_document(self, obj):
        # ``.first()`` re-queries even on a prefetched relation (it appends ordering);
        # index the cached list instead.
        documents = list(obj.documents.all())
        document = documents[0] if documents else None
        if not document:
            return {
                "document_type": obj.document_type,
                "sap_doc_entry": obj.sap_doc_entry,
                "sap_doc_num": obj.sap_doc_num,
            }
        return SalesDispatchGateOutDocumentSerializer(document).data

    def _weighment_value(self, obj, field):
        weighment = getattr(obj.vehicle_entry, "weighment", None)
        return getattr(weighment, field, None) if weighment else None

    def get_gross_weight(self, obj):
        return self._weighment_value(obj, "gross_weight")

    def get_tare_weight(self, obj):
        return self._weighment_value(obj, "tare_weight")

    def get_net_weight(self, obj):
        return self._weighment_value(obj, "net_weight")

    def get_weighbridge_slip_no(self, obj):
        return self._weighment_value(obj, "weighbridge_slip_no")

    def get_first_weighment_time(self, obj):
        return self._weighment_value(obj, "first_weighment_time")

    def get_second_weighment_time(self, obj):
        return self._weighment_value(obj, "second_weighment_time")

    def get_challan_weight_by_name(self, obj):
        return user_display_name(obj.challan_weight_by)

    def get_box_scans(self, obj):
        # ``.filter()`` re-queries per row; the base queryset already prefetches
        # active-only box scans (with their per-bill document), so read ``.all()``
        # from cache (Python guard keeps this correct for callers that prefetch
        # unfiltered or not at all).
        box_scans = [scan for scan in obj.box_scans.all() if scan.is_active]
        return SalesDispatchBoxScanDetailSerializer(box_scans, many=True).data

    def get_additional_weights(self, obj):
        weights = [weight for weight in obj.additional_weights.all() if weight.is_active]
        return SalesDispatchAdditionalWeightSerializer(weights, many=True).data


class SalesDispatchGateOutDocumentListSerializer(SalesDispatchGateOutDocumentSerializer):
    """Document fields for the dashboard list — drops the nested per-line ``items``,
    which the docking/dispatch-out table never renders."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("items", None)


class SalesDispatchGateOutListSerializer(SalesDispatchGateOutSerializer):
    """Slim serializer for the docking / dispatch-out dashboard *list*.

    The dashboard table renders only scalar fields, document numbers/count and an item
    summary. This drops the heavy nested collections the full (detail) serializer
    carries — box scans, attachments, gatepass print logs, additional weights, the
    readiness summary, the duplicated ``primary_document`` and per-line document
    ``items`` — none of which any list consumer reads. The entry-level ``items`` stays
    (used by the export sheet and the item-summary fallback). Keeping the list payload
    small is what lets the page load inside the client timeout; the detail endpoint
    keeps the full serializer. Pair with ``_sales_dispatch_list_queryset``.
    """

    documents = SalesDispatchGateOutDocumentListSerializer(many=True, read_only=True)

    # Always dropped from the list payload: heavy nested collections the board never
    # renders, plus ``qr_payload`` (only the gatepass/detail views read it -- it was
    # ~0.2 MB of dead weight on every board load).
    _LIST_OMITTED_FIELDS = (
        "box_scans",
        "attachments",
        "gatepass_print_logs",
        "additional_weights",
        "gatepass_readiness",
        "primary_document",
        "qr_payload",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self._LIST_OMITTED_FIELDS:
            self.fields.pop(field_name, None)
        # The per-line ``items`` (~1.3 MB) and full ``documents`` (~1 MB) arrays are
        # the two biggest slices of the board payload and only the export sheet reads
        # them -- the table shows the stored ``item_summary`` and the light
        # ``document_numbers`` / ``document_count`` (computed from the prefetch cache
        # whether or not the array is serialized). Omit both unless the caller opts in
        # (``?detail=1``), which also turns on the matching items prefetch.
        if not self.context.get("include_items"):
            self.fields.pop("items", None)
            self.fields.pop("documents", None)


class SalesDispatchGateOutCreateSerializer(serializers.Serializer):
    document_type = serializers.ChoiceField(
        choices=SalesDispatchDocumentType.choices,
        required=False,
    )
    sap_doc_entry = serializers.IntegerField(required=False)
    documents = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=False,
    )
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    dispatch_plan_id = serializers.IntegerField(required=False, allow_null=True)
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    eway_bill = serializers.CharField(required=False, allow_blank=True, default="")
    bilty_no = serializers.CharField(required=False, allow_blank=True, default="")
    bilty_date = serializers.DateField(required=False, allow_null=True)
    freight = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    total_freight = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    dock_incharge = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        documents = attrs.get("documents")
        if documents:
            normalized_documents = []
            seen = set()
            for index, raw_document in enumerate(documents):
                document_type = raw_document.get("document_type") or attrs.get("document_type")
                sap_doc_entry = raw_document.get("sap_doc_entry") or raw_document.get("doc_entry")
                if not document_type or sap_doc_entry in (None, ""):
                    raise serializers.ValidationError(
                        {
                            "documents": (
                                "Each document requires document_type and sap_doc_entry."
                            )
                        }
                    )
                try:
                    sap_doc_entry = int(sap_doc_entry)
                except (TypeError, ValueError) as exc:
                    raise serializers.ValidationError(
                        {"documents": f"Document {index + 1} has an invalid SAP DocEntry."}
                    ) from exc
                key = (str(document_type).upper(), sap_doc_entry)
                if key in seen:
                    raise serializers.ValidationError(
                        {"documents": "Duplicate SAP documents are not allowed."}
                    )
                seen.add(key)
                normalized_documents.append(
                    {
                        "document_type": document_type,
                        "sap_doc_entry": sap_doc_entry,
                        "dispatch_plan_id": raw_document.get("dispatch_plan_id"),
                    }
                )
            attrs["documents"] = normalized_documents
            attrs["document_type"] = normalized_documents[0]["document_type"]
            attrs["sap_doc_entry"] = normalized_documents[0]["sap_doc_entry"]
            if not attrs.get("dispatch_plan_id"):
                attrs["dispatch_plan_id"] = normalized_documents[0].get("dispatch_plan_id")
            return attrs

        if not attrs.get("document_type") or attrs.get("sap_doc_entry") in (None, ""):
            raise serializers.ValidationError(
                "document_type and sap_doc_entry are required."
            )
        attrs["documents"] = [
            {
                "document_type": attrs["document_type"],
                "sap_doc_entry": attrs["sap_doc_entry"],
                "dispatch_plan_id": attrs.get("dispatch_plan_id"),
            }
        ]
        return attrs


class SalesDispatchGateOutUpdateSerializer(serializers.Serializer):
    security_name = serializers.CharField(required=False, allow_blank=True)
    eway_bill = serializers.CharField(required=False, allow_blank=True)
    bilty_no = serializers.CharField(required=False, allow_blank=True)
    bilty_date = serializers.DateField(required=False, allow_null=True)
    freight = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    total_freight = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    dock_incharge = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class SalesDispatchChallanWeightSerializer(serializers.Serializer):
    """Operator-entered challan weight used as the net-weight comparison reference.

    Pass null to clear a previously entered value and fall back to the SAP weight.
    """

    challan_weight = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        required=True,
        allow_null=True,
        min_value=Decimal("0"),
    )


class SalesDispatchAttachmentUploadSerializer(serializers.Serializer):
    attachment_type = serializers.ChoiceField(
        choices=SalesDispatchAttachmentType.choices,
        default=SalesDispatchAttachmentType.OTHER,
    )
    file = serializers.FileField()
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["attachment_type"] == SalesDispatchAttachmentType.TRUCK_PHOTO:
            if attrs.get("latitude") is None or attrs.get("longitude") is None:
                raise serializers.ValidationError(
                    "Latitude and longitude are required for truck photo upload."
                )
        attrs["latitude"] = self._coordinate(attrs.get("latitude"), "latitude", -90, 90)
        attrs["longitude"] = self._coordinate(attrs.get("longitude"), "longitude", -180, 180)
        return attrs

    @staticmethod
    def _coordinate(value, field_name, minimum, maximum):
        if value is None:
            return None
        try:
            decimal_value = Decimal(str(value)).quantize(Decimal("0.000001"))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise serializers.ValidationError({field_name: "Enter a valid coordinate."}) from exc
        if decimal_value < Decimal(str(minimum)) or decimal_value > Decimal(str(maximum)):
            raise serializers.ValidationError(
                {field_name: f"Coordinate must be between {minimum} and {maximum}."}
            )
        return decimal_value


class SalesDispatchBoxScanCreateSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=500, trim_whitespace=True)
    # The bill (SalesDispatchGateOutDocument) the operator is scanning into. When
    # omitted the backend auto-resolves the bill (legacy / whole-load scanning).
    document = serializers.IntegerField(required=False, allow_null=True)


class SalesDispatchBoxScanBatchCreateSerializer(serializers.Serializer):
    """Accept a batch of scanned barcodes submitted together from the client.

    The whole list is scanned locally on the device and only sent on submit, so
    each entry is validated independently and the response reports which ones
    failed (see the batch view). ``allow_empty=False`` rejects an empty submit.
    """

    barcodes = serializers.ListField(
        child=serializers.CharField(max_length=500, trim_whitespace=True, allow_blank=True),
        allow_empty=False,
        max_length=2000,
    )


class SalesDispatchGatepassPrintSerializer(serializers.Serializer):
    uom = serializers.CharField(required=False, allow_blank=True, default="")
    physical_quantity = serializers.DecimalField(
        max_digits=18,
        decimal_places=3,
        required=False,
        allow_null=True,
    )
    seal_number = serializers.CharField(required=False, allow_blank=True, default="")
    pgi_reference = serializers.CharField(required=False, allow_blank=True, default="")
    eway_bill = serializers.CharField(required=False, allow_blank=True, default="")
    printer_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)


class SalesDispatchGatepassReprintSerializer(serializers.Serializer):
    reprint_reason = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
    printer_name = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)


class SalesDispatchReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
