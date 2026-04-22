from rest_framework import serializers
from .models import (
    Box, Pallet, BoxMovement, PalletMovement, LabelPrintLog, ScanLog, LooseStock,
)


# ---------------------------------------------------------------------------
# Box Movement
# ---------------------------------------------------------------------------

class BoxMovementSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source='performed_by.full_name', read_only=True, default=''
    )
    from_pallet_id = serializers.CharField(
        source='from_pallet.pallet_id', read_only=True, default=''
    )
    to_pallet_id = serializers.CharField(
        source='to_pallet.pallet_id', read_only=True, default=''
    )

    class Meta:
        model = BoxMovement
        fields = [
            'id', 'movement_type',
            'from_warehouse', 'to_warehouse',
            'from_bin', 'to_bin',
            'from_pallet', 'from_pallet_id',
            'to_pallet', 'to_pallet_id',
            'performed_by', 'performed_by_name',
            'performed_at',
        ]


# ---------------------------------------------------------------------------
# Pallet Movement
# ---------------------------------------------------------------------------

class PalletMovementSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source='performed_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = PalletMovement
        fields = [
            'id', 'movement_type',
            'from_warehouse', 'to_warehouse',
            'from_bin', 'to_bin',
            'sap_transfer_doc_entry', 'quantity',
            'performed_by', 'performed_by_name',
            'performed_at', 'notes',
        ]


# ---------------------------------------------------------------------------
# Box
# ---------------------------------------------------------------------------

class BoxListSerializer(serializers.ModelSerializer):
    pallet_code = serializers.CharField(
        source='pallet.pallet_id', read_only=True, default=''
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = Box
        fields = [
            'id', 'box_barcode', 'item_code', 'item_name',
            'batch_number', 'qty', 'uom',
            'g_weight', 'n_weight',
            'mfg_date', 'exp_date',
            'pallet', 'pallet_code',
            'current_warehouse', 'current_bin',
            'status', 'production_line',
            'created_by', 'created_by_name',
            'created_at',
        ]


class BoxDetailSerializer(serializers.ModelSerializer):
    pallet_code = serializers.CharField(
        source='pallet.pallet_id', read_only=True, default=''
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )
    movements = BoxMovementSerializer(many=True, read_only=True)
    dismantled_into = serializers.SerializerMethodField()
    repacked_from = serializers.SerializerMethodField()

    class Meta:
        model = Box
        fields = [
            'id', 'box_barcode', 'barcode_data',
            'item_code', 'item_name',
            'batch_number', 'qty', 'uom',
            'mfg_date', 'exp_date',
            'pallet', 'pallet_code',
            'production_run', 'production_line',
            'current_warehouse', 'current_bin',
            'status',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            'movements', 'dismantled_into', 'repacked_from',
        ]

    def get_dismantled_into(self, obj):
        """Loose stock records created when this box was dismantled."""
        qs = obj.loose_stocks.all().select_related(
            'repacked_into_box', 'created_by'
        )
        return [{
            'id': ls.id,
            'qty': str(ls.original_qty),
            'reason': ls.reason,
            'status': ls.status,
            'repacked_into_box_id': ls.repacked_into_box_id,
            'repacked_into_barcode': ls.repacked_into_box.box_barcode if ls.repacked_into_box else '',
            'created_at': ls.created_at.isoformat(),
        } for ls in qs]

    def get_repacked_from(self, obj):
        """Loose stock records that were repacked into this box."""
        qs = obj.repacked_from.all().select_related(
            'source_box', 'created_by'
        )
        return [{
            'id': ls.id,
            'qty': str(ls.original_qty),
            'reason': ls.reason,
            'source_box_id': ls.source_box_id,
            'source_box_barcode': ls.source_box.box_barcode if ls.source_box else '',
            'created_at': ls.created_at.isoformat(),
        } for ls in qs]


# ---------------------------------------------------------------------------
# Pallet
# ---------------------------------------------------------------------------

class PalletListSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = Pallet
        fields = [
            'id', 'pallet_id', 'item_code', 'item_name',
            'batch_number', 'box_count', 'total_qty', 'uom',
            'mfg_date', 'exp_date',
            'current_warehouse', 'current_bin',
            'status', 'production_line',
            'created_by', 'created_by_name',
            'created_at',
        ]


class PalletDetailSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )
    boxes = BoxListSerializer(many=True, read_only=True)
    dismantled_boxes = serializers.SerializerMethodField()
    movements = PalletMovementSerializer(many=True, read_only=True)

    class Meta:
        model = Pallet
        fields = [
            'id', 'pallet_id', 'barcode_data',
            'item_code', 'item_name',
            'batch_number', 'box_count', 'total_qty', 'uom',
            'mfg_date', 'exp_date',
            'production_run', 'production_line',
            'current_warehouse', 'current_bin',
            'status',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            'boxes', 'dismantled_boxes', 'movements',
        ]

    def get_dismantled_boxes(self, obj):
        """Boxes that were removed from this pallet (via depalletize/dismantle movements)."""
        from .models import BoxMovement, BoxMovementType
        removed_box_ids = (
            BoxMovement.objects
            .filter(
                from_pallet=obj,
                movement_type__in=[BoxMovementType.DEPALLETIZE, BoxMovementType.DISMANTLE]
            )
            .values_list('box_id', flat=True)
            .distinct()
        )
        removed_boxes = (
            Box.objects
            .filter(id__in=removed_box_ids)
            .exclude(pallet=obj)  # exclude boxes that were re-added
            .select_related('pallet', 'created_by')
        )
        return BoxListSerializer(removed_boxes, many=True).data


# ---------------------------------------------------------------------------
# Input Serializers
# ---------------------------------------------------------------------------

class BoxGenerateSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    batch_number = serializers.CharField(max_length=100)
    qty = serializers.DecimalField(max_digits=12, decimal_places=2)
    box_count = serializers.IntegerField(min_value=1, max_value=500)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='PCS')
    g_weight = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True, default=None)
    n_weight = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True, default=None)
    mfg_date = serializers.DateField()
    exp_date = serializers.DateField(required=False, allow_null=True, default=None)
    warehouse = serializers.CharField(max_length=20)
    production_line = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    production_run_id = serializers.IntegerField(required=False)


class PalletCreateSerializer(serializers.Serializer):
    box_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=1
    )
    warehouse = serializers.CharField(max_length=20)
    production_line = serializers.CharField(max_length=50, required=False, default='')
    production_run_id = serializers.IntegerField(required=False)


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class PalletMoveSerializer(serializers.Serializer):
    to_warehouse = serializers.CharField(max_length=20)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PalletClearSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PalletSplitSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    warehouse = serializers.CharField(max_length=20)


class PalletAddBoxesSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)


class PalletRemoveBoxesSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)


class BoxTransferSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    to_warehouse = serializers.CharField(max_length=20)
    to_pallet_id = serializers.IntegerField(required=False, default=None)


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

class PrintRequestSerializer(serializers.Serializer):
    print_type = serializers.ChoiceField(
        choices=['ORIGINAL', 'REPRINT'], default='ORIGINAL'
    )
    reprint_reason = serializers.CharField(required=False, allow_blank=True, default='')
    printer_name = serializers.CharField(required=False, allow_blank=True, default='')


class BulkPrintRequestSerializer(serializers.Serializer):
    items = serializers.ListField(
        child=serializers.DictField(), min_length=1,
        help_text="[{label_type: 'BOX'|'PALLET', id: int, print_type: 'ORIGINAL'|'REPRINT', reprint_reason: ''}]"
    )


# ---------------------------------------------------------------------------
# Print & Scan Log Output
# ---------------------------------------------------------------------------

class LabelPrintLogSerializer(serializers.ModelSerializer):
    printed_by_name = serializers.CharField(
        source='printed_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = LabelPrintLog
        fields = [
            'id', 'label_type', 'reference_id', 'reference_code',
            'print_type', 'reprint_reason',
            'printed_by', 'printed_by_name', 'printed_at',
            'printer_name',
        ]


class ScanLogSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(
        source='scanned_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = ScanLog
        fields = [
            'id', 'scan_type', 'barcode_raw', 'barcode_parsed',
            'entity_type', 'entity_id', 'scan_result',
            'context_ref_type', 'context_ref_id',
            'scanned_by', 'scanned_by_name', 'scanned_at',
            'device_info',
        ]


# ---------------------------------------------------------------------------
# Dismantle & Repack
# ---------------------------------------------------------------------------

class DismantlePalletSerializer(serializers.Serializer):
    box_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=None,
        help_text="Box IDs to remove. Omit or null to dismantle ALL boxes."
    )
    reason = serializers.ChoiceField(
        choices=['REPACK', 'SAMPLE', 'DAMAGED', 'RETURN', 'OTHER'],
        default='OTHER'
    )
    reason_notes = serializers.CharField(required=False, allow_blank=True, default='')


class DismantleBoxSerializer(serializers.Serializer):
    qty = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, default=None,
        help_text="Qty to dismantle. Omit or null for full box."
    )
    reason = serializers.ChoiceField(
        choices=['REPACK', 'SAMPLE', 'DAMAGED', 'RETURN', 'OTHER'],
        default='OTHER'
    )
    reason_notes = serializers.CharField(required=False, allow_blank=True, default='')


class RepackSerializer(serializers.Serializer):
    loose_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=1
    )
    qty_per_loose = serializers.DictField(
        child=serializers.CharField(), required=False, default=None,
        help_text="{loose_id: qty_to_use}. Omit to use full qty from each."
    )
    warehouse = serializers.CharField(max_length=20)


class LooseStockListSerializer(serializers.ModelSerializer):
    source_box_barcode = serializers.CharField(
        source='source_box.box_barcode', read_only=True, default=''
    )
    source_pallet_id = serializers.CharField(
        source='source_pallet.pallet_id', read_only=True, default=''
    )
    repacked_into_barcode = serializers.CharField(
        source='repacked_into_box.box_barcode', read_only=True, default=''
    )
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = LooseStock
        fields = [
            'id', 'item_code', 'item_name', 'batch_number',
            'qty', 'original_qty', 'uom',
            'source_box', 'source_box_barcode',
            'source_pallet', 'source_pallet_id',
            'reason', 'reason_notes',
            'current_warehouse', 'status',
            'repacked_into_box', 'repacked_into_barcode',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]


class LooseStockDetailSerializer(LooseStockListSerializer):
    class Meta(LooseStockListSerializer.Meta):
        pass


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

class ProductionLabelsSerializer(serializers.Serializer):
    qty_per_box = serializers.DecimalField(max_digits=12, decimal_places=2)
    box_count = serializers.IntegerField(min_value=1, max_value=500)
    batch_number = serializers.CharField(max_length=100)
    warehouse = serializers.CharField(max_length=20)


class ProductionPalletSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    warehouse = serializers.CharField(max_length=20)


class ScanRequestSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=500)
    scan_type = serializers.ChoiceField(
        choices=['RECEIVE', 'PUTAWAY', 'PICK', 'COUNT', 'TRANSFER', 'SHIP', 'RETURN', 'LOOKUP'],
        default='LOOKUP'
    )
    context_ref_type = serializers.CharField(required=False, allow_blank=True, default='')
    context_ref_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    device_info = serializers.CharField(required=False, allow_blank=True, default='')
