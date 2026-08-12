"""Serializers for the marketplace app.

Split into input serializers (plain ``Serializer``) and output serializers
(``ModelSerializer``), following ``gate_core/serializers_sales_dispatch.py``.
Dispatch/return have a light *List* serializer and a *Detail* serializer that
computes resolved lines + scan progress.
"""
from django.db import transaction
from rest_framework import serializers

from .models import (
    ComboComponent,
    ComboComponentOption,
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceGatePass,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceOrderLine,
    MarketplaceReturn,
    MarketplaceReturnCondition,
    MarketplaceReturnScan,
    MarketplaceScan,
    MarketplaceSettings,
    MarketplaceWarehouse,
    SkuMapping,
    SkuMappingOption,
)
from .services import resolve_service, scan_service


# ── Settings ─────────────────────────────────────────────────────────────────
class MarketplaceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceSettings
        fields = ["id", "channel", "skip_packing", "defer_delivery_note", "updated_at"]
        read_only_fields = ["id", "channel", "updated_at"]


# ── Masters ──────────────────────────────────────────────────────────────────
class MarketplaceWarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceWarehouse
        fields = [
            "id", "channel", "name", "sap_warehouse_code", "sap_customer_card_code",
            "facility_code", "sap_series", "sap_tax_code", "sap_branch_id", "post_goods_issue",
            "is_default", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ComboComponentOptionSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    # Per-alternative quantity; blank/null falls back to the component quantity.
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, required=False, allow_null=True
    )

    class Meta:
        model = ComboComponentOption
        fields = ["id", "item_code", "item_name", "quantity", "is_default"]


class ComboComponentSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    # Interchangeable SAP items for this slot. Empty = ships its own item_code.
    options = ComboComponentOptionSerializer(many=True, required=False)

    class Meta:
        model = ComboComponent
        fields = ["id", "component_type", "item_code", "item_name", "quantity", "uom", "options"]


class ComboDefinitionSerializer(serializers.ModelSerializer):
    components = ComboComponentSerializer(many=True)
    # Inline SKU mapping — a combo is defined AND FSN-mapped in one form, exactly
    # like a single (RAW) SKU. Written here; read back from the linked SkuMapping.
    fsn = serializers.CharField(required=False, allow_blank=True, default="", write_only=True)
    marketplace_sku = serializers.CharField(
        required=False, allow_blank=True, default="", write_only=True
    )
    sku_name = serializers.CharField(required=False, allow_blank=True, default="", write_only=True)

    class Meta:
        model = ComboDefinition
        fields = ["id", "channel", "code", "name", "is_active", "components",
                  "fsn", "marketplace_sku", "sku_name", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)  # write_only fields excluded
        mapping = instance.sku_mappings.order_by("id").first()
        data["fsn"] = mapping.fsn if mapping else ""
        data["marketplace_sku"] = mapping.marketplace_sku if mapping else ""
        data["sku_name"] = mapping.sku_name if mapping else ""
        return data

    def validate(self, attrs):
        components = attrs.get("components")
        if components is not None:
            if not components:
                raise serializers.ValidationError({"components": "Add at least one component."})
            for comp in components:
                if not str(comp.get("item_code", "")).strip():
                    raise serializers.ValidationError({"components": "Every component needs an item code."})
                if comp.get("quantity") is None or comp["quantity"] <= 0:
                    raise serializers.ValidationError({"components": "Component quantity must be greater than 0."})
            self._verify_against_item_master(components)
        return attrs

    def _verify_against_item_master(self, components):
        """Reject codes SAP does not have; make every name SAP's own.

        Checked across components AND their alternatives in one lookup, so a combo
        with several slots costs one query rather than one per row.
        """
        from .services.item_master import apply_sap_name, reject_unknown

        company = self._company()
        if company is None:
            return
        entries = []
        for comp in components:
            entries.append(comp)
            entries.extend(comp.get("options") or [])
        known = reject_unknown(
            company.code, [e.get("item_code") for e in entries], field="components")
        for entry in entries:
            apply_sap_name(entry, known)

    def _company(self):
        request = self.context.get("request")
        company_ctx = getattr(request, "company", None) if request else None
        return getattr(company_ctx, "company", None)

    @staticmethod
    def _pop_mapping(validated_data):
        return {
            "fsn": (validated_data.pop("fsn", "") or "").strip(),
            "marketplace_sku": (validated_data.pop("marketplace_sku", "") or "").strip(),
            "sku_name": (validated_data.pop("sku_name", "") or "").strip(),
        }

    def create(self, validated_data):
        with transaction.atomic():
            mapping = self._pop_mapping(validated_data)
            components = validated_data.pop("components", [])
            combo = ComboDefinition.objects.create(**validated_data)
            for comp in components:
                self._create_component(combo, comp)
            self._sync_mapping(combo, mapping)
        return combo

    def update(self, instance, validated_data):
        with transaction.atomic():
            mapping = self._pop_mapping(validated_data)
            components = validated_data.pop("components", None)
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()
            if components is not None:
                self._reconcile_components(instance, components)
            self._sync_mapping(instance, mapping)
        return instance

    def _reconcile_components(self, combo, components):
        """Match the payload onto the existing components by id.

        This used to delete every component and rebuild, which cascaded away all
        their ComboComponentOptions — so simply renaming a combo silently
        destroyed its alternatives. CB0030 lost its default option FG0000422 that
        way while its twin SL0000029, untouched, still has it.

        Now: update what the payload identifies, create what is new, and delete
        only what it omits.
        """
        existing = {c.id: c for c in combo.components.all()}
        seen = set()
        for comp in components:
            comp = dict(comp)
            comp_id = comp.pop("id", None)
            options = comp.pop("options", None)
            current = existing.get(comp_id) if comp_id else None
            if current is None:
                current = ComboComponent.objects.create(combo=combo, **comp)
                # A brand-new component has nothing to preserve, so an absent
                # options key means none rather than "leave alone".
                self._reconcile_options(current, options or [])
            else:
                for attr, value in comp.items():
                    setattr(current, attr, value)
                current.save()
                # Absent means untouched; an explicit [] is what clears them.
                if options is not None:
                    self._reconcile_options(current, options)
            seen.add(current.id)

        for comp_id, current in existing.items():
            if comp_id not in seen:
                current.delete()

    @staticmethod
    def _reconcile_options(component, options):
        """Same rule one level down, preserving the exactly-one-default invariant."""
        existing = {o.id: o for o in component.options.all()}
        seen = set()
        has_default = any(o.get("is_default") for o in options)
        for i, o in enumerate(options):
            qty = o.get("quantity")
            fields = {
                "item_code": (o.get("item_code") or "").strip(),
                "item_name": (o.get("item_name") or "").strip(),
                "quantity": qty if (qty is not None and qty > 0) else None,
                # Exactly one default: the flagged one, else the first.
                "is_default": bool(o.get("is_default")) if has_default else (i == 0),
            }
            current = existing.get(o.get("id")) if o.get("id") else None
            if current is None:
                current = ComboComponentOption.objects.create(component=component, **fields)
            else:
                for attr, value in fields.items():
                    setattr(current, attr, value)
                current.save()
            seen.add(current.id)

        for opt_id, current in existing.items():
            if opt_id not in seen:
                current.delete()

    @staticmethod
    def _create_component(combo, comp):
        """Create one component plus its interchangeable SAP items (exactly one
        marked default — the flagged one, else the first)."""
        comp = dict(comp)
        comp.pop("id", None)
        options = comp.pop("options", None) or []
        component = ComboComponent.objects.create(combo=combo, **comp)
        has_default = any(o.get("is_default") for o in options)
        for i, o in enumerate(options):
            qty = o.get("quantity")
            ComboComponentOption.objects.create(
                component=component,
                item_code=(o.get("item_code") or "").strip(),
                item_name=(o.get("item_name") or "").strip(),
                quantity=qty if (qty is not None and qty > 0) else None,
                is_default=bool(o.get("is_default")) if has_default else (i == 0),
            )
        return component

    @staticmethod
    def _sync_mapping(combo, mapping):
        """Create/update the combo's SKU mapping (FSN → this combo) from the form."""
        from django.db import IntegrityError
        from .models import SkuMapping, SkuType

        fsn = mapping["fsn"]
        msku = mapping["marketplace_sku"]
        if not fsn and not msku:
            return  # a combo may exist without an inline mapping
        obj = combo.sku_mappings.order_by("id").first()
        if obj is None:
            obj = SkuMapping(
                company=combo.company, channel=combo.channel, combo=combo,
                created_by=combo.created_by,
            )
        obj.sku_type = SkuType.COMBO
        obj.combo = combo
        obj.marketplace_sku = msku or combo.code  # marketplace_sku is required + unique
        obj.fsn = fsn
        obj.sku_name = mapping["sku_name"]
        obj.fg_item_code = ""
        obj.fg_item_name = ""
        try:
            with transaction.atomic():  # savepoint so a conflict doesn't break the outer txn
                obj.save()
        except IntegrityError:
            raise serializers.ValidationError(
                {"fsn": "That FSN or marketplace SKU is already mapped to another item."}
            )


class SkuMappingOptionSerializer(serializers.ModelSerializer):
    combo_code = serializers.CharField(source="combo.code", read_only=True, default="")

    class Meta:
        model = SkuMappingOption
        fields = [
            "id", "label", "sku_type", "fg_item_code", "fg_item_name",
            "combo", "combo_code", "is_default",
        ]
        read_only_fields = ["id"]


class SkuMappingSerializer(serializers.ModelSerializer):
    combo_code = serializers.CharField(source="combo.code", read_only=True, default="")
    # Item/combo NAMES so the masters list reads in plain language, not just codes.
    combo_name = serializers.CharField(source="combo.name", read_only=True, default="")
    # The SAP items this FSN MAY ship as. Optional — a mapping with no options ships
    # its single fg_item_code/combo exactly as before.
    options = SkuMappingOptionSerializer(many=True, required=False)

    class Meta:
        model = SkuMapping
        fields = [
            "id", "channel", "marketplace_sku", "fsn", "sku_name", "sku_type",
            "fg_item_code", "fg_item_name", "combo", "combo_code", "combo_name",
            "default_uom", "is_active", "options", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        sku_type = attrs.get("sku_type", getattr(self.instance, "sku_type", None))
        combo = attrs.get("combo", getattr(self.instance, "combo", None))
        fg = attrs.get("fg_item_code", getattr(self.instance, "fg_item_code", None))
        if sku_type == "COMBO" and not combo:
            raise serializers.ValidationError({"combo": "Required for combo SKUs."})
        if sku_type == "RAW" and not fg:
            raise serializers.ValidationError({"fg_item_code": "Required for raw SKUs."})
        self._verify_against_item_master(attrs)
        return attrs

    def _verify_against_item_master(self, attrs):
        """Reject codes SAP does not have; make every name SAP's own.

        Covers the mapping's own item and its alternatives in one lookup. Only
        codes actually being written are checked — a partial update that never
        mentions fg_item_code leaves it alone.
        """
        from .services.item_master import apply_sap_name, reject_unknown

        request = self.context.get("request")
        company_ctx = getattr(request, "company", None) if request else None
        company = getattr(company_ctx, "company", None)
        if company is None:
            return

        entries = []
        if "fg_item_code" in attrs:
            entries.append(("fg_item_code", "fg_item_name", attrs))
        for option in attrs.get("options") or []:
            entries.append(("fg_item_code", "fg_item_name", option))

        codes = [e[2].get(e[0]) for e in entries]
        known = reject_unknown(company.code, codes, field="fg_item_code")
        for code_key, name_key, entry in entries:
            apply_sap_name(entry, known, code_key=code_key, name_key=name_key)

    def _save_options(self, mapping, options):
        """Replace a mapping's options wholesale. Exactly one is marked default
        (the flagged one, else the first)."""
        mapping.options.all().delete()
        if not options:
            return
        has_default = any(o.get("is_default") for o in options)
        for i, o in enumerate(options):
            SkuMappingOption.objects.create(
                mapping=mapping,
                label=o.get("label", ""),
                sku_type=o.get("sku_type", "RAW"),
                fg_item_code=o.get("fg_item_code", ""),
                fg_item_name=o.get("fg_item_name", ""),
                combo=o.get("combo"),
                is_default=bool(o.get("is_default")) if has_default else (i == 0),
            )

    def create(self, validated_data):
        options = validated_data.pop("options", None)
        mapping = super().create(validated_data)
        if options is not None:
            self._save_options(mapping, options)
        return mapping

    def update(self, instance, validated_data):
        options = validated_data.pop("options", None)
        mapping = super().update(instance, validated_data)
        if options is not None:
            self._save_options(mapping, options)
        return mapping


class SkuMappingImportSerializer(serializers.Serializer):
    rows = SkuMappingSerializer(many=True)


# ── Orders ───────────────────────────────────────────────────────────────────
class MarketplaceOrderLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceOrderLine
        fields = ["id", "marketplace_sku", "sku_name", "ordered_quantity"]


class MarketplaceOrderSerializer(serializers.ModelSerializer):
    lines = MarketplaceOrderLineSerializer(many=True, read_only=True)
    # Annotated by OrderListView; true once warehouse materials were issued.
    dispatch_ready = serializers.BooleanField(read_only=True, default=False)

    class Meta:
        model = MarketplaceOrder
        fields = [
            "id", "channel", "order_id", "order_date", "buyer_name",
            "sap_warehouse_code", "status", "lines", "created_at", "dispatch_ready",
        ]


class ResolvedLineSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    component_type = serializers.CharField()
    required_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    uom = serializers.CharField()
    warehouse_code = serializers.CharField()
    source_skus = serializers.ListField(child=serializers.CharField())


class ResolvedOrderSerializer(serializers.Serializer):
    order = MarketplaceOrderSerializer()
    resolved_lines = ResolvedLineSerializer(many=True)
    unmapped_skus = serializers.ListField(child=serializers.CharField())


# ── Scans + dispatch ─────────────────────────────────────────────────────────
class MarketplaceScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(source="scanned_by.full_name", read_only=True, default="")

    class Meta:
        model = MarketplaceScan
        fields = [
            "id", "dispatch", "barcode_raw", "item_code", "item_name",
            "component_type", "source_sku", "quantity", "uom", "warehouse_code",
            "scanned_by", "scanned_by_name", "scanned_at",
        ]
        read_only_fields = fields


class MarketplaceDispatchListSerializer(serializers.ModelSerializer):
    order_id = serializers.CharField(source="order.order_id", read_only=True)
    buyer_name = serializers.CharField(source="order.buyer_name", read_only=True, default="")
    internal_billing_num = serializers.CharField(
        source="internal_billing.invoice_number", read_only=True, default=""
    )
    scanned_count = serializers.SerializerMethodField()

    def get_scanned_count(self, obj):
        # Number of distinct items scanned — for the Outward board's per-item progress.
        # Uses the list view's annotation when present to avoid an N+1 count query.
        ann = getattr(obj, "scanned_count_ann", None)
        return ann if ann is not None else obj.scans.filter(is_active=True).count()

    class Meta:
        model = MarketplaceDispatch
        fields = [
            "id", "channel", "order", "order_id", "buyer_name", "sap_warehouse_code",
            "status", "scanned_count", "sap_delivery_note_num", "sap_goods_issue_num",
            "internal_billing_num", "sap_post_status", "sap_error", "confirmed_at",
            "created_at", "updated_at",
        ]


class MarketplaceDispatchDetailSerializer(MarketplaceDispatchListSerializer):
    scans = MarketplaceScanSerializer(many=True, read_only=True)
    resolved_lines = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    unmapped_skus = serializers.SerializerMethodField()

    class Meta(MarketplaceDispatchListSerializer.Meta):
        fields = MarketplaceDispatchListSerializer.Meta.fields + [
            "scans", "resolved_lines", "progress", "unmapped_skus",
        ]

    def _resolved(self, obj):
        if not hasattr(self, "_resolved_cache"):
            self._resolved_cache = resolve_service.resolve_order(obj.order)
        return self._resolved_cache

    def get_resolved_lines(self, obj):
        return ResolvedLineSerializer(self._resolved(obj)["resolved_lines"], many=True).data

    def get_unmapped_skus(self, obj):
        return self._resolved(obj)["unmapped_skus"]

    def get_progress(self, obj):
        flines = resolve_service.fg_lines(self._resolved(obj)["resolved_lines"])
        scanned = {
            (ic or "").strip().upper(): q
            for ic, q in obj.scans.filter(is_active=True).values_list("item_code", "quantity")
        }
        from decimal import Decimal
        scanned = {k: Decimal(v) for k, v in scanned.items()}
        return scan_service.build_progress(flines, scanned)


# ── Returns ──────────────────────────────────────────────────────────────────
class MarketplaceReturnScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(source="scanned_by.full_name", read_only=True, default="")

    class Meta:
        model = MarketplaceReturnScan
        fields = [
            "id", "mp_return", "barcode_raw", "item_code", "item_name",
            "component_type", "source_sku", "quantity", "uom",
            "condition", "condition_remarks",
            "scanned_by", "scanned_by_name", "scanned_at",
        ]
        read_only_fields = fields


class ReturnScanConditionSerializer(serializers.Serializer):
    """Set the condition (+ optional remarks) on a returned item."""

    condition = serializers.ChoiceField(
        choices=MarketplaceReturnCondition.choices, allow_blank=True, required=False,
    )
    condition_remarks = serializers.CharField(
        max_length=255, allow_blank=True, required=False, default="",
    )


class MarketplaceReturnListSerializer(serializers.ModelSerializer):
    order_id = serializers.CharField(source="order.order_id", read_only=True)
    buyer_name = serializers.CharField(source="order.buyer_name", read_only=True, default="")
    # The submitted return's document is presented as a Return Note; the number is
    # stored on ``internal_credit_doc_num`` for back-compat.
    return_note_num = serializers.CharField(source="internal_credit_doc_num", read_only=True)

    class Meta:
        model = MarketplaceReturn
        fields = [
            "id", "channel", "order", "order_id", "buyer_name", "status",
            "internal_credit_doc_num", "return_note_num",
            "submitted_at", "created_at", "updated_at",
        ]


class MarketplaceReturnDetailSerializer(MarketplaceReturnListSerializer):
    scans = MarketplaceReturnScanSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True, default=""
    )

    class Meta(MarketplaceReturnListSerializer.Meta):
        fields = MarketplaceReturnListSerializer.Meta.fields + [
            "scans", "progress", "submitted_by_name",
        ]

    def get_progress(self, obj):
        return scan_service.return_progress(obj)


class MarketplaceOrderBillingSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceOrderBilling
        fields = [
            "id", "channel", "order_id", "invoice_number", "buyer_name",
            "sap_delivery_note_num", "total_amount", "status", "created_at",
        ]


# ── Input serializers ────────────────────────────────────────────────────────
class ChannelField(serializers.ChoiceField):
    def __init__(self, **kwargs):
        from .models import MarketplaceChannel
        super().__init__(choices=MarketplaceChannel.choices, **kwargs)


class DispatchCreateSerializer(serializers.Serializer):
    channel = ChannelField()
    order_id = serializers.CharField(max_length=120)


class ScanCreateSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=500, trim_whitespace=True)
    item_code = serializers.CharField(max_length=100, required=False, allow_blank=True)
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, required=False, min_value=0
    )


class ConfirmSerializer(serializers.Serializer):
    override_deviation = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True)


class DeliveryNoteCutSerializer(serializers.Serializer):
    """Validates the bulk delivery-note cut body so bad input is a 400, not a 500
    (``dispatch_ids`` flows into ``.filter(id__in=...)``; the ids must be ints)."""
    channel = ChannelField(required=False)
    dispatch_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )
    warehouse_id = serializers.IntegerField(required=False, allow_null=True)
    batch_id = serializers.IntegerField(required=False, allow_null=True)
    # Posting date (SAP DocDate). Omitted → today. A date in a previous month
    # back-dates the note; the service validates and permission-gates that.
    doc_date = serializers.DateField(required=False, allow_null=True)


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class ReturnCreateSerializer(serializers.Serializer):
    channel = ChannelField()
    order_id = serializers.CharField(max_length=120)


class ReturnSubmitSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)


class GatePassSerializer(serializers.ModelSerializer):
    """One outward trip, as the gate screen reads it.

    Transport is served from the frozen snapshot, not the FK: what the pass was
    printed with is what the gate person must see.
    """

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    sheet = serializers.CharField(source="import_batch.filename", read_only=True, default="")
    printed_by_name = serializers.CharField(
        source="printed_by.full_name", read_only=True, default="")
    dispatched_by_name = serializers.CharField(
        source="dispatched_by.full_name", read_only=True, default="")
    # Why this trip cannot leave yet, or "" — so the screen can disable the
    # button and say why in the same breath.
    weight_error = serializers.SerializerMethodField()
    is_weighed = serializers.BooleanField(read_only=True)

    class Meta:
        model = MarketplaceGatePass
        fields = [
            "id", "channel", "status", "status_display",
            "import_batch", "sheet",
            "vehicle", "vehicle_no",
            "transporter", "transporter_name", "transporter_gstin",
            "driver", "driver_name", "driver_mobile_no", "driver_license_no",
            "tare_weight", "gross_weight", "net_weight", "is_weighed",
            "weighbridge_slip_no", "first_weighment_at", "second_weighment_at",
            "weight_error",
            "order_count", "parcel_count",
            "gatepass_no", "random_code", "qr_payload",
            "printed_by_name", "printed_at",
            "gate_out_date", "out_time", "security_name",
            "dispatched_by_name", "dispatched_at",
            "remarks", "cancel_reason", "cancelled_at",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_weight_error(self, obj):
        from .services.gate_pass_service import weight_error

        return weight_error(obj)


class GatePassCreateSerializer(serializers.Serializer):
    """Open a trip against a sheet. Transport may be filled in later."""

    batch_id = serializers.IntegerField()
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    transporter_id = serializers.IntegerField(required=False, allow_null=True)
    driver_id = serializers.IntegerField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class GatePassTransportSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    transporter_id = serializers.IntegerField(required=False, allow_null=True)
    driver_id = serializers.IntegerField(required=False, allow_null=True)


class GatePassWeighmentSerializer(serializers.Serializer):
    """Either half may arrive on its own — empty before loading, full after."""

    tare_weight = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True)
    gross_weight = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True)
    weighbridge_slip_no = serializers.CharField(
        required=False, allow_blank=True, max_length=50)

    def validate(self, attrs):
        if all(attrs.get(f) is None for f in ("tare_weight", "gross_weight")) and (
            attrs.get("weighbridge_slip_no") is None
        ):
            raise serializers.ValidationError("Enter a tare or gross weight.")
        return attrs


class GatePassDispatchSerializer(serializers.Serializer):
    security_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    out_date = serializers.DateField(required=False, allow_null=True)
    out_time = serializers.TimeField(required=False, allow_null=True)


class GatePassCancelSerializer(serializers.Serializer):
    reason = serializers.CharField()
