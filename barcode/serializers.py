from rest_framework import serializers
from .models import (
    Box, Pallet, BoxMovement, PalletMovement, LabelPrintLog, ScanLog, LooseStock,
    LooseStockConsumption,
    DispatchSession, DispatchSessionLine, DispatchScanLog, DispatchScannedUnit,
    DispatchSapSyncLog, DispatchSettings, PalletBoxHistory,
    BarcodeAuditLog, IntercompanyTransfer, IntercompanyTransferLine,
    PalletVerifyRequest,
)


MAX_BOX_LABELS_PER_REQUEST = 5000


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
            'performed_at', 'notes',
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


class PalletBoxHistorySerializer(serializers.ModelSerializer):
    pallet_barcode = serializers.CharField(source='pallet.pallet_id', read_only=True, default='')
    box_barcode = serializers.CharField(source='box.box_barcode', read_only=True, default='')
    bill_number = serializers.CharField(source='dispatch_session.bill_number', read_only=True, default='')
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = PalletBoxHistory
        fields = [
            'id', 'pallet', 'pallet_barcode',
            'box', 'box_barcode',
            'action', 'old_status', 'new_status',
            'dispatch_session', 'bill_number',
            'remarks', 'created_by', 'created_by_name',
            'created_at',
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
            'dispatch_session', 'dispatched_at',
            'removed_from_pallet_at', 'removed_from_pallet_reason',
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
            'dispatch_session', 'dispatched_at',
            'removed_from_pallet_at', 'removed_from_pallet_reason',
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
    max_box_count = serializers.SerializerMethodField()

    class Meta:
        model = Pallet
        fields = [
            'id', 'pallet_id', 'item_code', 'item_name',
            'batch_number', 'box_count', 'total_boxes',
            'available_boxes', 'dispatched_boxes',
            'max_box_count', 'total_qty', 'uom',
            'mfg_date', 'exp_date',
            'current_warehouse', 'current_bin',
            'status', 'production_line',
            'dispatch_session', 'dispatched_at',
            'created_by', 'created_by_name',
            'created_at',
        ]

    def get_max_box_count(self, obj):
        return getattr(obj, 'max_box_count', 0) or 0


class PalletDetailSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )
    boxes = BoxListSerializer(many=True, read_only=True)
    dismantled_boxes = serializers.SerializerMethodField()
    movements = PalletMovementSerializer(many=True, read_only=True)
    max_box_count = serializers.SerializerMethodField()

    class Meta:
        model = Pallet
        fields = [
            'id', 'pallet_id', 'barcode_data',
            'item_code', 'item_name',
            'batch_number', 'box_count', 'total_boxes',
            'available_boxes', 'dispatched_boxes',
            'max_box_count', 'total_qty', 'uom',
            'mfg_date', 'exp_date',
            'production_run', 'production_line',
            'current_warehouse', 'current_bin',
            'status',
            'dispatch_session', 'dispatched_at',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            'boxes', 'dismantled_boxes', 'movements',
        ]

    def get_max_box_count(self, obj):
        return getattr(obj, 'max_box_count', 0) or 0

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
    box_count = serializers.IntegerField(min_value=1, max_value=MAX_BOX_LABELS_PER_REQUEST)
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
        child=serializers.IntegerField(), required=False, allow_empty=True, default=list
    )
    warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    production_line = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=''
    )
    production_run_id = serializers.IntegerField(required=False)
    item_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    item_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    batch_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    total_qty = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0
    )
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='PCS')
    mfg_date = serializers.DateField(required=False)
    exp_date = serializers.DateField(required=False, allow_null=True, default=None)
    max_box_count = serializers.IntegerField(min_value=0, max_value=MAX_BOX_LABELS_PER_REQUEST, required=False, default=0)

    def validate_box_ids(self, value):
        if value:
            raise serializers.ValidationError(
                "Create the pallet first, then attach boxes from the pallet QR print workflow."
            )
        return value


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False)


class PalletVoidSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False)
    box_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=None,
        help_text="Box IDs to also VOID. Omit or null to void none (boxes only disassociated).",
    )


# ---------------------------------------------------------------------------
# Voided items — traceability list (sourced from VOID movement records)
# ---------------------------------------------------------------------------

class VoidedPalletSerializer(serializers.ModelSerializer):
    """A voided pallet, from its VOID PalletMovement (who/when/reason)."""
    pallet_id = serializers.IntegerField(source='pallet.id', read_only=True)
    pallet_code = serializers.CharField(source='pallet.pallet_id', read_only=True, default='')
    item_code = serializers.CharField(source='pallet.item_code', read_only=True, default='')
    item_name = serializers.CharField(source='pallet.item_name', read_only=True, default='')
    batch_number = serializers.CharField(source='pallet.batch_number', read_only=True, default='')
    box_count = serializers.IntegerField(source='pallet.box_count', read_only=True, default=0)
    total_qty = serializers.DecimalField(
        source='pallet.total_qty', max_digits=12, decimal_places=2, read_only=True, default=0,
    )
    uom = serializers.CharField(source='pallet.uom', read_only=True, default='')
    warehouse = serializers.CharField(source='from_warehouse', read_only=True, default='')
    voided_by_name = serializers.CharField(
        source='performed_by.full_name', read_only=True, default='',
    )
    voided_at = serializers.DateTimeField(source='performed_at', read_only=True)
    reason = serializers.CharField(source='notes', read_only=True, default='')

    class Meta:
        model = PalletMovement
        fields = [
            'id', 'pallet_id', 'pallet_code', 'item_code', 'item_name',
            'batch_number', 'box_count', 'total_qty', 'uom', 'warehouse',
            'voided_by_name', 'voided_at', 'reason',
        ]


class VoidedBoxSerializer(serializers.ModelSerializer):
    """A voided box, from its VOID BoxMovement (who/when/reason)."""
    box_id = serializers.IntegerField(source='box.id', read_only=True)
    box_barcode = serializers.CharField(source='box.box_barcode', read_only=True, default='')
    item_code = serializers.CharField(source='box.item_code', read_only=True, default='')
    item_name = serializers.CharField(source='box.item_name', read_only=True, default='')
    batch_number = serializers.CharField(source='box.batch_number', read_only=True, default='')
    qty = serializers.DecimalField(
        source='box.qty', max_digits=12, decimal_places=2, read_only=True, default=0,
    )
    uom = serializers.CharField(source='box.uom', read_only=True, default='')
    warehouse = serializers.CharField(source='from_warehouse', read_only=True, default='')
    from_pallet_code = serializers.CharField(
        source='from_pallet.pallet_id', read_only=True, default='',
    )
    voided_by_name = serializers.CharField(
        source='performed_by.full_name', read_only=True, default='',
    )
    voided_at = serializers.DateTimeField(source='performed_at', read_only=True)
    reason = serializers.CharField(source='notes', read_only=True, default='')

    class Meta:
        model = BoxMovement
        fields = [
            'id', 'box_id', 'box_barcode', 'item_code', 'item_name',
            'batch_number', 'qty', 'uom', 'warehouse', 'from_pallet_code',
            'voided_by_name', 'voided_at', 'reason',
        ]


class PalletMoveSerializer(serializers.Serializer):
    to_warehouse = serializers.CharField(max_length=20)
    # Destination location/bin inside the warehouse (set for own/WMS warehouses).
    to_bin = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PalletClearSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class PalletSplitSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    target_pallet_id = serializers.IntegerField()


class PalletAddBoxesSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)


class PalletRemoveBoxesSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)


class PalletReconcileSerializer(serializers.Serializer):
    """Physically-scanned box barcodes to reconcile against a pallet.

    With ``apply=False`` the reconcile is read-only. With ``apply=True`` the
    chosen actions (``pull_foreign`` / ``drop_missing``) are committed.
    """
    scanned_barcodes = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=128),
        allow_empty=True,
        default=list,
    )
    unlabeled_count = serializers.IntegerField(min_value=0, default=0)
    apply = serializers.BooleanField(default=False)
    pull_foreign = serializers.BooleanField(default=False)
    drop_missing = serializers.BooleanField(default=False)
    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=255)


# ---------------------------------------------------------------------------
# Pallet Verify Requests (ticket workflow)
# ---------------------------------------------------------------------------

class PalletVerifyRequestListSerializer(serializers.ModelSerializer):
    pallet_code = serializers.CharField(source='pallet.pallet_id', read_only=True)
    item_code = serializers.CharField(source='pallet.item_code', read_only=True)
    item_name = serializers.CharField(source='pallet.item_name', read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    resolved_by_name = serializers.SerializerMethodField()
    missing_count = serializers.SerializerMethodField()
    foreign_count = serializers.SerializerMethodField()

    class Meta:
        model = PalletVerifyRequest
        fields = [
            'id', 'pallet', 'pallet_code', 'item_code', 'item_name',
            'status', 'source', 'source_reference', 'reason',
            'requested_by', 'requested_by_name', 'requested_at',
            'resolved_by', 'resolved_by_name', 'resolved_at',
            'resolution_note', 'missing_count', 'foreign_count', 'updated_at',
        ]

    @staticmethod
    def _name(user):
        if not user:
            return ''
        return getattr(user, 'full_name', '') or getattr(user, 'email', '') or ''

    def get_requested_by_name(self, obj):
        return self._name(obj.requested_by)

    def get_resolved_by_name(self, obj):
        return self._name(obj.resolved_by)

    def get_missing_count(self, obj):
        return len((obj.findings or {}).get('missing', []) or [])

    def get_foreign_count(self, obj):
        return len((obj.findings or {}).get('foreign', []) or [])


class PalletVerifyRequestDetailSerializer(PalletVerifyRequestListSerializer):
    class Meta(PalletVerifyRequestListSerializer.Meta):
        fields = PalletVerifyRequestListSerializer.Meta.fields + ['findings']


class PalletVerifyRequestCreateSerializer(serializers.Serializer):
    pallet_id = serializers.IntegerField()
    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=1000)
    source = serializers.CharField(required=False, allow_blank=True, default='MANUAL', max_length=20)
    source_reference = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=100
    )
    scanned_barcodes = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=128),
        allow_empty=True, default=list,
    )
    unlabeled_count = serializers.IntegerField(min_value=0, default=0)


class PalletVerifyRequestResolveSerializer(serializers.Serializer):
    resolution_note = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=1000
    )


class PalletVerifyRequestCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='', max_length=1000)


class BoxTransferSerializer(serializers.Serializer):
    box_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    to_warehouse = serializers.CharField(max_length=20)
    # Destination location/bin inside the warehouse (set for own/WMS warehouses).
    to_bin = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    to_pallet_id = serializers.IntegerField(required=False, default=None)


class IntercompanyBarcodeScanSerializer(serializers.Serializer):
    barcode = serializers.CharField(max_length=200)
    source_company_code = serializers.CharField(max_length=50)
    destination_company_code = serializers.CharField(max_length=50)
    transfer_type = serializers.ChoiceField(choices=['BOX', 'PALLET'], default='BOX')
    device_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')


class IntercompanyTransferCreateSerializer(serializers.Serializer):
    source_company_code = serializers.CharField(max_length=50)
    destination_company_code = serializers.CharField(max_length=50)
    transfer_type = serializers.ChoiceField(choices=['BOX', 'PALLET'], default='BOX')
    barcodes = serializers.ListField(child=serializers.CharField(max_length=200), min_length=1)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    device_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    sap_enabled = serializers.BooleanField(required=False, default=False)
    destination_warehouse = serializers.CharField(
        max_length=20, required=False, allow_blank=True, default=''
    )


class IntercompanyTransferReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    device_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')


class IntercompanyTransferLineSerializer(serializers.ModelSerializer):
    from_company_code = serializers.CharField(source='from_company.code', read_only=True)
    from_company_name = serializers.CharField(source='from_company.name', read_only=True)
    to_company_code = serializers.CharField(source='to_company.code', read_only=True)
    to_company_name = serializers.CharField(source='to_company.name', read_only=True)

    class Meta:
        model = IntercompanyTransferLine
        fields = [
            'id', 'box', 'barcode', 'item_code', 'item_name',
            'batch_number', 'qty', 'uom', 'from_warehouse',
            'from_company_code', 'from_company_name',
            'to_company_code', 'to_company_name', 'created_at',
        ]


class IntercompanyTransferSerializer(serializers.ModelSerializer):
    source_company_code = serializers.CharField(source='source_company.code', read_only=True)
    source_company_name = serializers.CharField(source='source_company.name', read_only=True)
    destination_company_code = serializers.CharField(source='destination_company.code', read_only=True)
    destination_company_name = serializers.CharField(source='destination_company.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True, default='')
    reversed_by_name = serializers.CharField(source='reversed_by.full_name', read_only=True, default='')
    lines = IntercompanyTransferLineSerializer(many=True, read_only=True)

    class Meta:
        model = IntercompanyTransfer
        fields = [
            'id', 'transfer_number', 'source_company_code', 'source_company_name',
            'destination_company_code', 'destination_company_name',
            'entity_type', 'status', 'total_barcodes', 'total_qty', 'uom',
            'destination_warehouse',
            'sap_enabled', 'sap_doc_entry', 'sap_doc_num', 'sap_status', 'sap_error',
            'notes', 'device_id', 'reversed_at', 'reversed_by_name',
            'created_by_name', 'created_at', 'updated_at', 'lines',
        ]


class BarcodeAuditLogSerializer(serializers.ModelSerializer):
    from_company_code = serializers.CharField(source='from_company.code', read_only=True, default='')
    from_company_name = serializers.CharField(source='from_company.name', read_only=True, default='')
    to_company_code = serializers.CharField(source='to_company.code', read_only=True, default='')
    to_company_name = serializers.CharField(source='to_company.name', read_only=True, default='')
    user_name = serializers.CharField(source='user.full_name', read_only=True, default='')
    transfer_number = serializers.CharField(source='transfer.transfer_number', read_only=True, default='')

    class Meta:
        model = BarcodeAuditLog
        fields = [
            'id', 'barcode', 'transaction_type', 'transfer', 'transfer_number',
            'from_company_code', 'from_company_name',
            'to_company_code', 'to_company_name',
            'user_name', 'device_id', 'notes', 'created_at',
        ]


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

class PrintRequestSerializer(serializers.Serializer):
    print_type = serializers.ChoiceField(
        choices=['ORIGINAL', 'REPRINT'], default='ORIGINAL'
    )
    reprint_reason = serializers.CharField(required=False, allow_blank=True, default='')
    printer_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=''
    )


class PalletPrintWorkflowSerializer(PrintRequestSerializer):
    box_count = serializers.IntegerField(
        min_value=1, max_value=MAX_BOX_LABELS_PER_REQUEST, required=False
    )


class BulkPrintItemSerializer(serializers.Serializer):
    label_type = serializers.ChoiceField(choices=['BOX', 'PALLET'])
    id = serializers.IntegerField(min_value=1)
    print_type = serializers.ChoiceField(
        choices=['ORIGINAL', 'REPRINT'], required=False, default='ORIGINAL'
    )
    reprint_reason = serializers.CharField(required=False, allow_blank=True, default='')
    printer_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default=''
    )


class BulkPrintRequestSerializer(serializers.Serializer):
    items = serializers.ListField(
        child=BulkPrintItemSerializer(),
        min_length=1,
        help_text=(
            "[{label_type: 'BOX'|'PALLET', id: int, print_type: "
            "'ORIGINAL'|'REPRINT', reprint_reason: '', printer_name: ''}]"
        ),
    )


# ---------------------------------------------------------------------------
# Print & Scan Log Output
# ---------------------------------------------------------------------------

class LabelPrintLogSerializer(serializers.ModelSerializer):
    printed_by_name = serializers.CharField(
        source='printed_by.full_name', read_only=True, default=''
    )
    printed_by_email = serializers.CharField(
        source='printed_by.email', read_only=True, default=''
    )

    class Meta:
        model = LabelPrintLog
        fields = [
            'id', 'label_type', 'reference_id', 'reference_code',
            'print_type', 'reprint_reason',
            'printed_by', 'printed_by_name', 'printed_by_email', 'printed_at',
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
    item_code = serializers.CharField(max_length=50)
    qty = serializers.DecimalField(max_digits=12, decimal_places=2)
    warehouse = serializers.CharField(max_length=20)
    batch_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
        help_text="Batch for the new box. Blank → single source batch, or MIXED."
    )


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


class LooseStockConsumptionSerializer(serializers.ModelSerializer):
    box_barcode = serializers.CharField(
        source='box.box_barcode', read_only=True, default=''
    )

    class Meta:
        model = LooseStockConsumption
        fields = ['id', 'box', 'box_barcode', 'qty', 'created_at']


class LooseStockDetailSerializer(LooseStockListSerializer):
    consumptions = LooseStockConsumptionSerializer(many=True, read_only=True)

    class Meta(LooseStockListSerializer.Meta):
        fields = LooseStockListSerializer.Meta.fields + ['consumptions']


class LooseStockSummarySerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    total_qty = serializers.DecimalField(max_digits=14, decimal_places=2)
    record_count = serializers.IntegerField()
    batches = serializers.ListField(child=serializers.CharField())
    warehouses = serializers.ListField(child=serializers.CharField())


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

class ProductionLabelsSerializer(serializers.Serializer):
    qty_per_box = serializers.DecimalField(max_digits=12, decimal_places=2)
    box_count = serializers.IntegerField(min_value=1, max_value=MAX_BOX_LABELS_PER_REQUEST)
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


# ---------------------------------------------------------------------------
# Barcode Dispatch
# ---------------------------------------------------------------------------

class DispatchBillLookupSerializer(serializers.Serializer):
    bill_number = serializers.CharField(max_length=80)


class DispatchSessionCreateSerializer(serializers.Serializer):
    bill_number = serializers.CharField(max_length=80)


class DispatchScanSubmitSerializer(serializers.Serializer):
    barcode = serializers.CharField(max_length=1000)
    line_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    device_id = serializers.CharField(max_length=120, required=False, allow_blank=True, default='')
    request_id = serializers.UUIDField(required=False, allow_null=True, default=None)


class DispatchScannedBoxQtySerializer(serializers.Serializer):
    dispatch_qty = serializers.DecimalField(max_digits=18, decimal_places=3)


class DispatchCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False)


class DispatchSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = DispatchSettings
        fields = [
            'allow_partial_dispatch',
            'allow_partial_pallet_dispatch',
            'allow_box_dispatch_from_pallet',
            'require_sequential_item_scanning',
            'require_sap_sync_on_completion',
            'allow_manual_close',
            'allow_admin_override',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class DispatchSessionLineSerializer(serializers.ModelSerializer):
    remaining_qty = serializers.SerializerMethodField()
    expected_qty = serializers.DecimalField(source='bill_qty', max_digits=18, decimal_places=3, read_only=True)
    pending_qty = serializers.SerializerMethodField()
    expected_boxes = serializers.SerializerMethodField()
    scanned_boxes = serializers.SerializerMethodField()
    pending_boxes = serializers.SerializerMethodField()

    class Meta:
        model = DispatchSessionLine
        fields = [
            'id', 'sequence_no', 'sap_line_no',
            'material_code', 'material_description',
            'bill_qty', 'expected_qty', 'scanned_qty', 'remaining_qty',
            'pending_qty',
            'bill_boxes', 'expected_boxes', 'scanned_boxes', 'pending_boxes',
            'uom', 'batch_number', 'warehouse_code',
            'serial_required', 'status',
        ]

    def get_remaining_qty(self, obj):
        return str(max(obj.bill_qty - obj.scanned_qty, 0))

    def get_pending_qty(self, obj):
        return self.get_remaining_qty(obj)

    def get_expected_boxes(self, obj):
        return str(obj.bill_boxes or 0)

    def get_scanned_boxes(self, obj):
        units = getattr(obj, '_prefetched_objects_cache', {}).get('scanned_units')
        if units is None:
            count = obj.scanned_units.filter(entity_type='BOX').exclude(scan_status='REMOVED').count()
        else:
            count = sum(
                1 for unit in units
                if unit.entity_type == 'BOX' and unit.scan_status != 'REMOVED'
            )
        return str(count)

    def get_pending_boxes(self, obj):
        try:
            expected = obj.bill_boxes or 0
            scanned = int(self.get_scanned_boxes(obj))
            return str(max(expected - scanned, 0))
        except Exception:
            return "0"


class DispatchScanLogSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(
        source='scanned_by.full_name', read_only=True, default=''
    )
    scan_type = serializers.CharField(source='entity_type', read_only=True)
    parsed_material_code = serializers.CharField(source='material_code', read_only=True)
    qty_delta = serializers.DecimalField(source='qty', max_digits=18, decimal_places=3, read_only=True)
    rejection_reason = serializers.CharField(source='reject_message', read_only=True)
    pallet_id = serializers.SerializerMethodField()
    box_id = serializers.SerializerMethodField()

    class Meta:
        model = DispatchScanLog
        fields = [
            'id', 'line', 'raw_barcode', 'parsed_barcode',
            'scan_type', 'entity_type', 'entity_id',
            'material_code', 'batch_number', 'qty', 'uom',
            'parsed_material_code', 'pallet_id', 'box_id', 'qty_delta',
            'result', 'reject_code', 'reject_message',
            'rejection_reason',
            'device_id', 'ip_address',
            'scanned_by', 'scanned_by_name', 'scanned_at',
            'request_id',
        ]

    def _first_unit(self, obj):
        units = getattr(obj, '_prefetched_objects_cache', {}).get('scanned_units')
        if units:
            return units[0]
        return obj.scanned_units.select_related('box', 'pallet').first()

    def get_pallet_id(self, obj):
        unit = self._first_unit(obj)
        return unit.pallet_id if unit else None

    def get_box_id(self, obj):
        unit = self._first_unit(obj)
        return unit.box_id if unit else None


class DispatchScannedUnitSerializer(serializers.ModelSerializer):
    box_barcode = serializers.CharField(source='box.box_barcode', read_only=True, default='')
    item_code = serializers.CharField(source='box.item_code', read_only=True, default='')
    item_name = serializers.CharField(source='box.item_name', read_only=True, default='')
    warehouse = serializers.CharField(source='box.current_warehouse', read_only=True, default='')
    scanned_at = serializers.DateTimeField(source='created_at', read_only=True)
    barcode_type = serializers.CharField(source='entity_type', read_only=True)
    original_qty = serializers.DecimalField(source='total_box_qty', max_digits=18, decimal_places=3, read_only=True)
    available_qty = serializers.DecimalField(source='total_box_qty', max_digits=18, decimal_places=3, read_only=True)
    required_pending_qty = serializers.SerializerMethodField()
    status_after_scan = serializers.SerializerMethodField()
    dispatch_doc_no = serializers.CharField(source='session.bill_number', read_only=True, default='')
    dispatch_date_time = serializers.DateTimeField(source='session.dispatched_at', read_only=True)
    scanned_by_name = serializers.CharField(source='scan_log.scanned_by.full_name', read_only=True, default='')
    customer_name = serializers.CharField(source='session.customer_name', read_only=True, default='')

    class Meta:
        model = DispatchScannedUnit
        fields = [
            'id', 'line', 'scan_log', 'barcode_value',
            'entity_type', 'box', 'pallet', 'serial_number',
            'barcode_type',
            'box_barcode', 'item_code', 'item_name',
            'material_code', 'batch_number',
            'original_qty', 'available_qty', 'required_pending_qty',
            'total_box_qty', 'dispatch_qty', 'remaining_qty',
            'qty', 'uom', 'warehouse', 'scan_status',
            'status_after_scan', 'dispatch_doc_no', 'dispatch_date_time',
            'scanned_by_name', 'customer_name',
            'created_at', 'scanned_at',
        ]

    def _parsed(self, obj):
        return obj.scan_log.parsed_barcode if obj.scan_log_id and obj.scan_log else {}

    def get_required_pending_qty(self, obj):
        return self._parsed(obj).get('required_pending_qty') or str(obj.dispatch_qty)

    def get_status_after_scan(self, obj):
        return self._parsed(obj).get('status_after_scan') or (
            'Partial Dispatch' if obj.remaining_qty > 0 else 'Full Dispatch'
        )


class DispatchSapSyncLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = DispatchSapSyncLog
        fields = [
            'id', 'operation', 'request_payload', 'response_payload',
            'status', 'error_message', 'attempt_no', 'created_at',
        ]


class DispatchSessionSerializer(serializers.ModelSerializer):
    lines = DispatchSessionLineSerializer(many=True, read_only=True)
    scanned_units = DispatchScannedUnitSerializer(many=True, read_only=True)
    active_line = serializers.SerializerMethodField()
    can_dispatch = serializers.SerializerMethodField()
    can_scan = serializers.SerializerMethodField()
    pending_qty = serializers.SerializerMethodField()
    total_remaining_qty = serializers.SerializerMethodField()
    removed_box_count = serializers.SerializerMethodField()
    line_count = serializers.SerializerMethodField()
    completed_line_count = serializers.SerializerMethodField()
    accepted_scan_count = serializers.SerializerMethodField()
    rejected_scan_count = serializers.SerializerMethodField()
    pallet_scan_count = serializers.SerializerMethodField()
    box_scan_count = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(
        source='created_by.full_name', read_only=True, default=''
    )
    dispatched_by_name = serializers.CharField(
        source='dispatched_by.full_name', read_only=True, default=''
    )
    completed_by = serializers.IntegerField(source='dispatched_by_id', read_only=True)
    completed_by_name = serializers.CharField(source='dispatched_by.full_name', read_only=True, default='')
    closed_by_name = serializers.CharField(source='closed_by.full_name', read_only=True, default='')
    sap_sync_status = serializers.CharField(source='sap_update_status', read_only=True)
    sap_sync_error = serializers.CharField(source='sap_update_error', read_only=True)

    class Meta:
        model = DispatchSession
        fields = [
            'id', 'bill_number',
            'sap_system_type', 'sap_object_type',
            'sap_doc_entry', 'sap_doc_num',
            'delivery_number', 'reference_delivery_number',
            'customer_code', 'customer_name',
            'ship_to_code', 'ship_to_name',
            'bill_date', 'status',
            'total_expected_qty', 'total_scanned_qty', 'pending_qty',
            'total_remaining_qty', 'removed_box_count',
            'sap_dispatch_status',
            'sap_update_status', 'sap_update_error',
            'sap_sync_status', 'sap_sync_error',
            'started_at', 'completed_at',
            'dispatched_at', 'dispatched_by', 'dispatched_by_name',
            'completed_by', 'completed_by_name',
            'closed_at', 'closed_by', 'closed_by_name', 'close_reason',
            'cancelled_at', 'cancel_reason',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            'line_count', 'completed_line_count',
            'accepted_scan_count', 'rejected_scan_count',
            'pallet_scan_count', 'box_scan_count',
            'active_line', 'can_dispatch', 'can_scan',
            'lines', 'scanned_units',
        ]

    def _lines(self, obj):
        return list(obj.lines.all())

    def get_active_line(self, obj):
        for line in self._lines(obj):
            if line.scanned_qty < line.bill_qty:
                return DispatchSessionLineSerializer(line).data
        return None

    def get_can_dispatch(self, obj):
        return obj.total_scanned_qty > 0 and obj.status not in {
            'COMPLETED', 'CLOSED', 'CANCELLED', 'SAP_SYNC_FAILED',
        }

    def get_can_scan(self, obj):
        return obj.status in {'DRAFT', 'ACTIVE', 'PARTIAL', 'READY_TO_DISPATCH'}

    def get_pending_qty(self, obj):
        return str(max(obj.total_expected_qty - obj.total_scanned_qty, 0))

    def get_total_remaining_qty(self, obj):
        units = getattr(obj, '_prefetched_objects_cache', {}).get('scanned_units')
        qs = units if units is not None else obj.scanned_units.all()
        total = sum(
            unit.remaining_qty
            for unit in qs
            if unit.entity_type == 'BOX' and unit.scan_status != 'REMOVED'
        )
        return str(total)

    def get_removed_box_count(self, obj):
        units = getattr(obj, '_prefetched_objects_cache', {}).get('scanned_units')
        if units is None:
            return obj.scanned_units.filter(entity_type='BOX', scan_status='REMOVED').count()
        return sum(1 for unit in units if unit.entity_type == 'BOX' and unit.scan_status == 'REMOVED')

    def get_line_count(self, obj):
        return len(self._lines(obj))

    def get_completed_line_count(self, obj):
        return sum(1 for line in self._lines(obj) if line.scanned_qty >= line.bill_qty)

    def get_accepted_scan_count(self, obj):
        return obj.scan_logs.filter(result='ACCEPTED').count()

    def get_rejected_scan_count(self, obj):
        return obj.scan_logs.filter(result='REJECTED').count()

    def get_pallet_scan_count(self, obj):
        return obj.scan_logs.filter(result='ACCEPTED', entity_type='PALLET').count()

    def get_box_scan_count(self, obj):
        return obj.scanned_units.filter(entity_type='BOX').exclude(scan_status='REMOVED').count()
