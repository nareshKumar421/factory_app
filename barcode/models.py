from django.db import models
from django.conf import settings


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class PalletStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    PARTIAL = "PARTIAL", "Partial"
    INSIDE_VEHICLE = "INSIDE_VEHICLE", "Inside Vehicle"
    DISPATCHED = "DISPATCHED", "Dispatched"
    EMPTY = "EMPTY", "Empty"
    INACTIVE = "INACTIVE", "Inactive"
    CLEARED = "CLEARED", "Cleared"
    SPLIT = "SPLIT", "Split"
    VOID = "VOID", "Void"


class BoxStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    PARTIAL = "PARTIAL", "Partial"
    INSIDE_VEHICLE = "INSIDE_VEHICLE", "Inside Vehicle"
    DISPATCHED = "DISPATCHED", "Dispatched"
    DISMANTLED = "DISMANTLED", "Dismantled"
    VOID = "VOID", "Void"


class LabelType(models.TextChoices):
    BOX = "BOX", "Box"
    PALLET = "PALLET", "Pallet"
    BIN = "BIN", "Bin"
    WAREHOUSE = "WAREHOUSE", "Warehouse"


class PrintType(models.TextChoices):
    ORIGINAL = "ORIGINAL", "Original"
    REPRINT = "REPRINT", "Reprint"


class PalletMovementType(models.TextChoices):
    CREATE = "CREATE", "Create"
    MOVE = "MOVE", "Move"
    TRANSFER = "TRANSFER", "Transfer"
    OWNERSHIP_TRANSFER = "OWNERSHIP_TRANSFER", "Ownership Transfer"
    LOAD_VEHICLE = "LOAD_VEHICLE", "Loaded into Vehicle"
    UNLOAD_VEHICLE = "UNLOAD_VEHICLE", "Unloaded from Vehicle"
    DISPATCH = "DISPATCH", "Dispatch"
    REMOVE_FOR_DISPATCH = "REMOVE_FOR_DISPATCH", "Remove for Dispatch"
    DISMANTLE = "DISMANTLE", "Dismantle"
    CLEAR = "CLEAR", "Clear"
    SPLIT = "SPLIT", "Split"
    VOID = "VOID", "Void"


class BoxMovementType(models.TextChoices):
    CREATE = "CREATE", "Create"
    MOVE = "MOVE", "Move"
    TRANSFER = "TRANSFER", "Transfer"
    OWNERSHIP_TRANSFER = "OWNERSHIP_TRANSFER", "Ownership Transfer"
    LOAD_VEHICLE = "LOAD_VEHICLE", "Loaded into Vehicle"
    UNLOAD_VEHICLE = "UNLOAD_VEHICLE", "Unloaded from Vehicle"
    PALLETIZE = "PALLETIZE", "Palletize"
    DEPALLETIZE = "DEPALLETIZE", "Depalletize"
    DISPATCH = "DISPATCH", "Dispatch"
    REMOVE_FOR_DISPATCH = "REMOVE_FOR_DISPATCH", "Remove for Dispatch"
    DISMANTLE = "DISMANTLE", "Dismantle"
    VOID = "VOID", "Void"


class IntercompanyTransferStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    COMPLETED = "COMPLETED", "Completed"
    REVERSED = "REVERSED", "Reversed"
    SAP_SYNC_FAILED = "SAP_SYNC_FAILED", "SAP Sync Failed"


class BarcodeAuditTransactionType(models.TextChoices):
    MANUFACTURED = "MANUFACTURED", "Manufactured"
    SCANNED = "SCANNED", "Scanned"
    TRANSFER_CREATED = "TRANSFER_CREATED", "Transfer Created"
    TRANSFER_COMPLETED = "TRANSFER_COMPLETED", "Transfer Completed"
    TRANSFER_REVERSED = "TRANSFER_REVERSED", "Transfer Reversed"
    DISPATCH_COMPLETED = "DISPATCH_COMPLETED", "Dispatch Completed"


class DismantleReason(models.TextChoices):
    REPACK = "REPACK", "Repack"
    SAMPLE = "SAMPLE", "Sample"
    DAMAGED = "DAMAGED", "Damaged"
    RETURN = "RETURN", "Return"
    OTHER = "OTHER", "Other"


class LooseStockStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REPACKED = "REPACKED", "Repacked"
    CONSUMED = "CONSUMED", "Consumed"


class ScanType(models.TextChoices):
    RECEIVE = "RECEIVE", "Receive"
    PUTAWAY = "PUTAWAY", "Putaway"
    PICK = "PICK", "Pick"
    COUNT = "COUNT", "Count"
    TRANSFER = "TRANSFER", "Transfer"
    SHIP = "SHIP", "Ship"
    RETURN = "RETURN", "Return"
    LOOKUP = "LOOKUP", "Lookup"


class EntityType(models.TextChoices):
    BOX = "BOX", "Box"
    PALLET = "PALLET", "Pallet"
    BIN = "BIN", "Bin"
    ITEM = "ITEM", "Item"
    UNKNOWN = "UNKNOWN", "Unknown"


class ScanResult(models.TextChoices):
    SUCCESS = "SUCCESS", "Success"
    NOT_FOUND = "NOT_FOUND", "Not Found"
    DUPLICATE = "DUPLICATE", "Duplicate"
    ERROR = "ERROR", "Error"


class DispatchSessionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    PARTIAL = "PARTIAL", "Partial"
    READY_TO_DISPATCH = "READY_TO_DISPATCH", "Ready to Dispatch"
    COMPLETED = "COMPLETED", "Completed"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"
    SAP_SYNC_FAILED = "SAP_SYNC_FAILED", "SAP Sync Failed"


class DispatchSapUpdateStatus(models.TextChoices):
    NOT_CONFIGURED = "NOT_CONFIGURED", "Not Configured"
    PENDING = "PENDING", "Pending"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class DispatchSapSystemType(models.TextChoices):
    S4HANA = "S4HANA", "S/4HANA"
    ECC = "ECC", "ECC"
    BUSINESS_ONE = "BUSINESS_ONE", "SAP Business One"


class DispatchSapObjectType(models.TextChoices):
    BILLING_DOCUMENT = "BILLING_DOCUMENT", "Billing Document"
    AR_INVOICE = "AR_INVOICE", "A/R Invoice"
    OUTBOUND_DELIVERY = "OUTBOUND_DELIVERY", "Outbound Delivery"


class DispatchScanResult(models.TextChoices):
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"


class DispatchScanEntityType(models.TextChoices):
    ITEM = "ITEM", "Item"
    BOX = "BOX", "Box"
    PALLET = "PALLET", "Pallet"
    SERIAL = "SERIAL", "Serial"
    UNKNOWN = "UNKNOWN", "Unknown"


class DispatchScannedUnitStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REMOVED = "REMOVED", "Removed"
    DISPATCHED = "DISPATCHED", "Dispatched"


class BarcodeMasterType(models.TextChoices):
    ITEM = "ITEM", "Item"
    BOX = "BOX", "Box"
    PALLET = "PALLET", "Pallet"


# ---------------------------------------------------------------------------
# Barcode Sequence - reserves box/pallet numbers per company/date/line
# ---------------------------------------------------------------------------

class BarcodeSequence(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.CASCADE,
        related_name='barcode_sequences'
    )
    sequence_type = models.CharField(max_length=20)
    date_str = models.CharField(max_length=8)
    line_key = models.CharField(max_length=50)
    next_value = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('company', 'sequence_type', 'date_str', 'line_key')
        ordering = ['sequence_type', 'date_str', 'line_key']
        verbose_name = 'Barcode Sequence'
        verbose_name_plural = 'Barcode Sequences'

    def __str__(self):
        return f"{self.sequence_type} {self.date_str} {self.line_key}: {self.next_value}"


# ---------------------------------------------------------------------------
# Pallet — collection of boxes
# ---------------------------------------------------------------------------

class Pallet(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='pallets'
    )
    pallet_id = models.CharField(
        max_length=50, unique=True,
        help_text="Auto-generated, e.g. PLT-20260417-L4-001"
    )
    barcode_data = models.JSONField(
        default=dict, blank=True,
        help_text="Encoded QR payload"
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, default='')
    batch_number = models.CharField(max_length=100)
    box_count = models.PositiveIntegerField(default=0)
    total_boxes = models.PositiveIntegerField(default=0)
    available_boxes = models.PositiveIntegerField(default=0)
    dispatched_boxes = models.PositiveIntegerField(default=0)
    max_box_count = models.PositiveIntegerField(
        default=0,
        help_text="Editable pallet capacity fetched from SAP HANA when available"
    )
    total_qty = models.DecimalField(max_digits=12, decimal_places=2)
    uom = models.CharField(max_length=20, blank=True, default='')
    mfg_date = models.DateField()
    exp_date = models.DateField()

    production_run = models.ForeignKey(
        'production_execution.ProductionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pallets'
    )
    production_line = models.CharField(max_length=50, blank=True, default='')
    current_warehouse = models.CharField(max_length=20)
    current_bin = models.CharField(
        max_length=50, blank=True, default='',
        help_text="App-managed bin location (SAP bins not enabled)"
    )

    status = models.CharField(
        max_length=20, choices=PalletStatus.choices,
        default=PalletStatus.ACTIVE
    )
    dispatch_session = models.ForeignKey(
        'DispatchSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispatched_pallets',
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pallets_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Pallet'
        verbose_name_plural = 'Pallets'

    def __str__(self):
        return f"{self.pallet_id} — {self.item_code} x {self.total_qty}"


# ---------------------------------------------------------------------------
# Box — individual carton with barcode
# ---------------------------------------------------------------------------

class Box(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='boxes'
    )
    box_barcode = models.CharField(
        max_length=50, unique=True,
        help_text="Auto-generated, e.g. BOX-20260417-L4-0001"
    )
    barcode_data = models.JSONField(
        default=dict, blank=True,
        help_text="Encoded QR payload"
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, default='')
    batch_number = models.CharField(max_length=100)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    uom = models.CharField(max_length=20, blank=True, default='')
    g_weight = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, default=None)
    n_weight = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, default=None)
    mfg_date = models.DateField()
    exp_date = models.DateField()

    pallet = models.ForeignKey(
        Pallet, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='boxes'
    )
    production_run = models.ForeignKey(
        'production_execution.ProductionRun', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='boxes'
    )
    production_line = models.CharField(max_length=50, blank=True, default='')
    current_warehouse = models.CharField(max_length=20)
    current_bin = models.CharField(
        max_length=50, blank=True, default='',
        help_text="App-managed bin location (SAP bins not enabled)"
    )

    status = models.CharField(
        max_length=20, choices=BoxStatus.choices,
        default=BoxStatus.ACTIVE
    )
    dispatch_session = models.ForeignKey(
        'DispatchSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='dispatched_boxes',
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    pre_load_status = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Status before the box was loaded INSIDE_VEHICLE (restored on unscan)"
    )
    pre_load_bin = models.CharField(
        max_length=50, blank=True, default='',
        help_text="Bin before the box was loaded INSIDE_VEHICLE (restored on unscan)"
    )
    removed_from_pallet_at = models.DateTimeField(null=True, blank=True)
    removed_from_pallet_reason = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='boxes_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Box'
        verbose_name_plural = 'Boxes'

    def __str__(self):
        return f"{self.box_barcode} — {self.item_code} x {self.qty}"


# ---------------------------------------------------------------------------
# Barcode Master — optional normalized barcode mapping for dispatch validation
# ---------------------------------------------------------------------------

class BarcodeMaster(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='barcode_master_records'
    )
    barcode = models.CharField(max_length=200)
    barcode_type = models.CharField(max_length=20, choices=BarcodeMasterType.choices)
    material_code = models.CharField(max_length=80, blank=True, default='')
    quantity = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=30, blank=True, default='')
    pallet = models.ForeignKey(
        Pallet,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_master_records',
    )
    box = models.ForeignKey(
        Box,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_master_records',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['barcode']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'barcode'],
                name='unique_barcode_master_per_company',
            )
        ]
        indexes = [
            models.Index(fields=['company', 'barcode_type', 'is_active']),
            models.Index(fields=['material_code']),
        ]
        verbose_name = 'Barcode Master'
        verbose_name_plural = 'Barcode Master'

    def __str__(self):
        return f"{self.barcode_type} {self.barcode}"


# ---------------------------------------------------------------------------
# Intercompany Barcode Transfer
# ---------------------------------------------------------------------------

class IntercompanyTransfer(models.Model):
    transfer_number = models.CharField(max_length=60, unique=True)
    entity_type = models.CharField(
        max_length=20,
        choices=[
            (EntityType.BOX, "Box"),
            (EntityType.PALLET, "Pallet"),
        ],
        default=EntityType.BOX,
    )
    source_company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='intercompany_transfers_out',
    )
    destination_company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='intercompany_transfers_in',
    )
    status = models.CharField(
        max_length=20,
        choices=IntercompanyTransferStatus.choices,
        default=IntercompanyTransferStatus.COMPLETED,
    )
    total_barcodes = models.PositiveIntegerField(default=0)
    total_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=30, blank=True, default='')
    destination_warehouse = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Destination-company warehouse the stock was received into "
                  "(blank = stock kept its source warehouse code)",
    )
    sap_enabled = models.BooleanField(default=False)
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.CharField(max_length=80, blank=True, default='')
    sap_status = models.CharField(max_length=40, blank=True, default='')
    sap_error = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='')
    device_id = models.CharField(max_length=120, blank=True, default='')
    reversed_of = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reverse_transfers',
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='intercompany_transfers_reversed',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='intercompany_transfers_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source_company', 'destination_company', 'created_at']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['transfer_number']),
            models.Index(fields=['sap_doc_entry']),
        ]
        permissions = [
            ("can_view_intercompany_transfer", "Can view intercompany barcode transfers"),
            ("can_create_intercompany_transfer", "Can create intercompany barcode transfers"),
            ("can_scan_intercompany_transfer", "Can scan intercompany transfer barcodes"),
            ("can_reverse_intercompany_transfer", "Can reverse intercompany barcode transfers"),
            ("can_manage_intercompany_transfer_settings", "Can manage intercompany transfer settings"),
        ]

    def __str__(self):
        return f"{self.transfer_number}: {self.source_company.code} -> {self.destination_company.code}"


class IntercompanyTransferLine(models.Model):
    transfer = models.ForeignKey(
        IntercompanyTransfer,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    box = models.ForeignKey(
        Box,
        on_delete=models.PROTECT,
        related_name='intercompany_transfer_lines',
    )
    barcode = models.CharField(max_length=200)
    item_code = models.CharField(max_length=80)
    item_name = models.CharField(max_length=255, blank=True, default='')
    batch_number = models.CharField(max_length=120, blank=True, default='')
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    uom = models.CharField(max_length=30, blank=True, default='')
    from_warehouse = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Box's warehouse at transfer time (restored on reverse)",
    )
    from_company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='intercompany_transfer_lines_out',
    )
    to_company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='intercompany_transfer_lines_in',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['transfer', 'box'],
                name='unique_box_per_intercompany_transfer',
            )
        ]
        indexes = [
            models.Index(fields=['barcode']),
            models.Index(fields=['item_code', 'batch_number']),
            models.Index(fields=['from_company', 'to_company']),
        ]

    def __str__(self):
        return f"{self.transfer.transfer_number} {self.barcode}"


class BarcodeAuditLog(models.Model):
    box = models.ForeignKey(
        Box,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )
    barcode = models.CharField(max_length=200)
    transaction_type = models.CharField(
        max_length=40,
        choices=BarcodeAuditTransactionType.choices,
    )
    transfer = models.ForeignKey(
        IntercompanyTransfer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
    )
    from_company = models.ForeignKey(
        'company.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_audit_from',
    )
    to_company = models.ForeignKey(
        'company.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_audit_to',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_audit_logs',
    )
    device_id = models.CharField(max_length=120, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['barcode', 'created_at']),
            models.Index(fields=['transaction_type', 'created_at']),
            models.Index(fields=['from_company', 'to_company']),
        ]

    def __str__(self):
        return f"{self.barcode} {self.transaction_type}"


# ---------------------------------------------------------------------------
# Label Print Log — audit trail for prints and reprints
# ---------------------------------------------------------------------------

class LabelPrintLog(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='label_print_logs'
    )
    label_type = models.CharField(max_length=20, choices=LabelType.choices)
    reference_id = models.CharField(
        max_length=100,
        help_text="PK of the Box, Pallet, or Bin as string"
    )
    reference_code = models.CharField(
        max_length=100,
        help_text="The barcode string itself"
    )
    print_type = models.CharField(
        max_length=20, choices=PrintType.choices,
        default=PrintType.ORIGINAL
    )
    reprint_reason = models.TextField(blank=True, default='')
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='label_prints'
    )
    printed_at = models.DateTimeField(auto_now_add=True)
    printer_name = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        ordering = ['-printed_at']
        verbose_name = 'Label Print Log'
        verbose_name_plural = 'Label Print Logs'

    def __str__(self):
        return f"{self.label_type} {self.print_type} — {self.reference_code}"


# ---------------------------------------------------------------------------
# Pallet Movement — tracks every pallet operation
# ---------------------------------------------------------------------------

class PalletMovement(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='pallet_movements'
    )
    pallet = models.ForeignKey(
        Pallet, on_delete=models.CASCADE,
        related_name='movements'
    )
    movement_type = models.CharField(
        max_length=20, choices=PalletMovementType.choices
    )
    from_warehouse = models.CharField(max_length=20, blank=True, default='')
    to_warehouse = models.CharField(max_length=20, blank=True, default='')
    from_bin = models.CharField(max_length=50, blank=True, default='')
    to_bin = models.CharField(max_length=50, blank=True, default='')
    sap_transfer_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Stock Transfer DocEntry (if posted)"
    )
    quantity = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pallet_movements_performed'
    )
    performed_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-performed_at']
        verbose_name = 'Pallet Movement'
        verbose_name_plural = 'Pallet Movements'

    def __str__(self):
        return f"{self.movement_type} — {self.pallet.pallet_id}"


# ---------------------------------------------------------------------------
# Box Movement — tracks every box operation
# ---------------------------------------------------------------------------

class BoxMovement(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='box_movements'
    )
    box = models.ForeignKey(
        Box, on_delete=models.CASCADE,
        related_name='movements'
    )
    movement_type = models.CharField(
        max_length=20, choices=BoxMovementType.choices
    )
    from_warehouse = models.CharField(max_length=20, blank=True, default='')
    to_warehouse = models.CharField(max_length=20, blank=True, default='')
    from_bin = models.CharField(max_length=50, blank=True, default='')
    to_bin = models.CharField(max_length=50, blank=True, default='')
    from_pallet = models.ForeignKey(
        Pallet, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='box_movements_from'
    )
    to_pallet = models.ForeignKey(
        Pallet, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='box_movements_to'
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='box_movements_performed'
    )
    performed_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-performed_at']
        verbose_name = 'Box Movement'
        verbose_name_plural = 'Box Movements'

    def __str__(self):
        return f"{self.movement_type} — {self.box.box_barcode}"


class PalletBoxHistory(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='pallet_box_history'
    )
    pallet = models.ForeignKey(
        Pallet,
        on_delete=models.CASCADE,
        related_name='box_history',
    )
    box = models.ForeignKey(
        Box,
        on_delete=models.CASCADE,
        related_name='pallet_history',
    )
    action = models.CharField(max_length=80)
    old_status = models.CharField(max_length=40, blank=True, default='')
    new_status = models.CharField(max_length=40, blank=True, default='')
    dispatch_session = models.ForeignKey(
        'DispatchSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pallet_box_history',
    )
    remarks = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pallet_box_history_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['pallet', 'box', 'created_at']),
            models.Index(fields=['dispatch_session', 'created_at']),
            models.Index(fields=['action']),
        ]
        verbose_name = 'Pallet Box History'
        verbose_name_plural = 'Pallet Box History'

    def __str__(self):
        return f"{self.action} — {self.pallet.pallet_id}/{self.box.box_barcode}"


# ---------------------------------------------------------------------------
# Loose Stock — items dismantled from boxes
# ---------------------------------------------------------------------------

class LooseStock(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='loose_stocks'
    )
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, default='')
    batch_number = models.CharField(max_length=100)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    original_qty = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Qty at time of dismantle (before any repack consumption)"
    )
    uom = models.CharField(max_length=20, blank=True, default='')

    source_box = models.ForeignKey(
        Box, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='loose_stocks'
    )
    source_pallet = models.ForeignKey(
        Pallet, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='loose_stocks'
    )

    reason = models.CharField(
        max_length=20, choices=DismantleReason.choices,
        default=DismantleReason.OTHER
    )
    reason_notes = models.TextField(blank=True, default='')

    current_warehouse = models.CharField(max_length=20)
    status = models.CharField(
        max_length=20, choices=LooseStockStatus.choices,
        default=LooseStockStatus.ACTIVE
    )

    # If repacked, link to the new box
    repacked_into_box = models.ForeignKey(
        Box, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='repacked_from'
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='loose_stocks_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Loose Stock'
        verbose_name_plural = 'Loose Stock'

    def __str__(self):
        return f"Loose {self.item_code} x {self.qty} from {self.source_box}"


# ---------------------------------------------------------------------------
# Scan Log — audit trail for every barcode scan
# ---------------------------------------------------------------------------

class ScanLog(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='scan_logs'
    )
    scan_type = models.CharField(max_length=20, choices=ScanType.choices)
    barcode_raw = models.CharField(
        max_length=500,
        help_text="Raw scanned string"
    )
    barcode_parsed = models.JSONField(
        default=dict, blank=True,
        help_text="Decoded barcode data"
    )
    entity_type = models.CharField(
        max_length=20, choices=EntityType.choices,
        default=EntityType.UNKNOWN
    )
    entity_id = models.CharField(max_length=100, blank=True, default='')
    scan_result = models.CharField(
        max_length=20, choices=ScanResult.choices,
        default=ScanResult.SUCCESS
    )
    context_ref_type = models.CharField(
        max_length=50, blank=True, default='',
        help_text="E.g. count_session, pick_list, transfer"
    )
    context_ref_id = models.IntegerField(
        null=True, blank=True,
        help_text="PK of the context entity"
    )
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='scans_performed'
    )
    scanned_at = models.DateTimeField(auto_now_add=True)
    device_info = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-scanned_at']
        verbose_name = 'Scan Log'
        verbose_name_plural = 'Scan Logs'

    def __str__(self):
        return f"{self.scan_type} — {self.barcode_raw[:50]}"


# ---------------------------------------------------------------------------
# Dispatch Session - SAP-backed barcode dispatch workflow
# ---------------------------------------------------------------------------

class DispatchSettings(models.Model):
    company = models.OneToOneField(
        'company.Company',
        on_delete=models.CASCADE,
        related_name='barcode_dispatch_settings',
    )
    allow_partial_dispatch = models.BooleanField(default=True)
    allow_partial_pallet_dispatch = models.BooleanField(default=True)
    allow_box_dispatch_from_pallet = models.BooleanField(default=True)
    require_sequential_item_scanning = models.BooleanField(default=True)
    require_sap_sync_on_completion = models.BooleanField(default=False)
    allow_manual_close = models.BooleanField(default=True)
    allow_admin_override = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Dispatch Settings'
        verbose_name_plural = 'Dispatch Settings'

    def __str__(self):
        return f"Dispatch settings — {self.company.code}"


class DispatchSession(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='barcode_dispatch_sessions'
    )
    bill_number = models.CharField(max_length=80)
    sap_system_type = models.CharField(
        max_length=30,
        choices=DispatchSapSystemType.choices,
        default=DispatchSapSystemType.BUSINESS_ONE,
    )
    sap_object_type = models.CharField(
        max_length=40,
        choices=DispatchSapObjectType.choices,
        default=DispatchSapObjectType.AR_INVOICE,
    )
    sap_doc_entry = models.CharField(max_length=80, blank=True, default='')
    sap_doc_num = models.CharField(max_length=80, blank=True, default='')
    delivery_number = models.CharField(max_length=80, blank=True, default='')
    reference_delivery_number = models.CharField(max_length=80, blank=True, default='')
    customer_code = models.CharField(max_length=80, blank=True, default='')
    customer_name = models.CharField(max_length=255, blank=True, default='')
    ship_to_code = models.CharField(max_length=80, blank=True, default='')
    ship_to_name = models.CharField(max_length=255, blank=True, default='')
    bill_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=30,
        choices=DispatchSessionStatus.choices,
        default=DispatchSessionStatus.DRAFT,
    )
    total_expected_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    total_scanned_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    sap_dispatch_status = models.CharField(max_length=80, blank=True, default='')
    sap_update_status = models.CharField(
        max_length=30,
        choices=DispatchSapUpdateStatus.choices,
        default=DispatchSapUpdateStatus.NOT_CONFIGURED,
    )
    sap_update_error = models.TextField(blank=True, default='')
    sap_snapshot = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatches_completed',
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatches_closed',
    )
    close_reason = models.TextField(blank=True, default='')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatches_cancelled',
    )
    cancel_reason = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatches_created',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatches_updated',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'bill_number'],
                condition=models.Q(status__in=[
                    DispatchSessionStatus.DRAFT,
                    DispatchSessionStatus.ACTIVE,
                    DispatchSessionStatus.PARTIAL,
                    DispatchSessionStatus.READY_TO_DISPATCH,
                    DispatchSessionStatus.COMPLETED,
                    DispatchSessionStatus.SAP_SYNC_FAILED,
                ]),
                name='unique_active_barcode_dispatch_bill',
            )
        ]
        indexes = [
            models.Index(fields=['company', 'bill_number']),
            models.Index(fields=['company', 'status', 'created_at']),
            models.Index(fields=['company', 'sap_doc_entry']),
        ]
        permissions = [
            ("can_view_barcode_dispatch", "Can view barcode dispatch sessions"),
            ("can_create_barcode_dispatch", "Can create barcode dispatch sessions"),
            ("can_scan_barcode_dispatch", "Can scan barcode dispatch labels"),
            ("can_complete_barcode_dispatch", "Can mark barcode dispatch complete"),
            ("can_close_barcode_dispatch", "Can close barcode dispatch sessions"),
            ("can_retry_barcode_dispatch_sap", "Can retry barcode dispatch SAP sync"),
            ("can_manage_barcode_dispatch_settings", "Can manage barcode dispatch settings"),
            ("can_view_barcode_dispatch_reports", "Can view barcode dispatch reports"),
        ]

    def __str__(self):
        return f"{self.company.code} dispatch {self.bill_number}"


class DispatchSessionLine(models.Model):
    session = models.ForeignKey(
        DispatchSession,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    sequence_no = models.PositiveIntegerField()
    sap_line_no = models.CharField(max_length=80)
    material_code = models.CharField(max_length=80)
    material_description = models.TextField(blank=True, default='')
    bill_qty = models.DecimalField(max_digits=18, decimal_places=3)
    bill_boxes = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    scanned_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    uom = models.CharField(max_length=30, blank=True, default='')
    batch_number = models.CharField(max_length=120, blank=True, default='')
    warehouse_code = models.CharField(max_length=40, blank=True, default='')
    serial_required = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sequence_no', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'sequence_no'],
                name='unique_dispatch_session_sequence',
            ),
            models.UniqueConstraint(
                fields=['session', 'sap_line_no'],
                name='unique_dispatch_session_sap_line',
            ),
            models.CheckConstraint(
                condition=models.Q(scanned_qty__lte=models.F('bill_qty')),
                name='dispatch_line_scanned_lte_bill',
            ),
        ]
        indexes = [
            models.Index(fields=['session', 'sequence_no']),
            models.Index(fields=['session', 'status']),
            models.Index(fields=['material_code']),
        ]

    def __str__(self):
        return f"{self.session.bill_number} line {self.sequence_no} {self.material_code}"


class DispatchScanLog(models.Model):
    session = models.ForeignKey(
        DispatchSession,
        on_delete=models.CASCADE,
        related_name='scan_logs',
    )
    line = models.ForeignKey(
        DispatchSessionLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='scan_logs',
    )
    raw_barcode = models.TextField()
    parsed_barcode = models.JSONField(default=dict, blank=True)
    entity_type = models.CharField(
        max_length=20,
        choices=DispatchScanEntityType.choices,
        default=DispatchScanEntityType.UNKNOWN,
    )
    entity_id = models.CharField(max_length=120, blank=True, default='')
    material_code = models.CharField(max_length=80, blank=True, default='')
    batch_number = models.CharField(max_length=120, blank=True, default='')
    qty = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=30, blank=True, default='')
    result = models.CharField(max_length=20, choices=DispatchScanResult.choices)
    reject_code = models.CharField(max_length=80, blank=True, default='')
    reject_message = models.TextField(blank=True, default='')
    device_id = models.CharField(max_length=120, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='barcode_dispatch_scans',
    )
    scanned_at = models.DateTimeField(auto_now_add=True)
    request_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ['-scanned_at']
        indexes = [
            models.Index(fields=['session', 'scanned_at']),
            models.Index(fields=['session', 'result']),
            models.Index(fields=['session', 'reject_code']),
            models.Index(fields=['entity_type', 'entity_id']),
        ]

    def __str__(self):
        return f"{self.session.bill_number} {self.result} {self.raw_barcode[:50]}"


class DispatchScannedUnit(models.Model):
    session = models.ForeignKey(
        DispatchSession,
        on_delete=models.CASCADE,
        related_name='scanned_units',
    )
    line = models.ForeignKey(
        DispatchSessionLine,
        on_delete=models.CASCADE,
        related_name='scanned_units',
    )
    scan_log = models.ForeignKey(
        DispatchScanLog,
        on_delete=models.CASCADE,
        related_name='scanned_units',
    )
    barcode_value = models.CharField(max_length=200)
    entity_type = models.CharField(
        max_length=20,
        choices=DispatchScanEntityType.choices,
    )
    box = models.ForeignKey(
        Box,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='dispatch_scanned_units',
    )
    pallet = models.ForeignKey(
        Pallet,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='dispatch_scanned_units',
    )
    serial_number = models.CharField(max_length=120, blank=True, default='')
    material_code = models.CharField(max_length=80)
    batch_number = models.CharField(max_length=120, blank=True, default='')
    total_box_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    dispatch_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    remaining_qty = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    uom = models.CharField(max_length=30, blank=True, default='')
    scan_status = models.CharField(
        max_length=20,
        choices=DispatchScannedUnitStatus.choices,
        default=DispatchScannedUnitStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'barcode_value'],
                name='unique_dispatch_barcode_per_session',
            ),
            models.UniqueConstraint(
                fields=['session', 'serial_number'],
                condition=~models.Q(serial_number=''),
                name='unique_dispatch_serial_per_session',
            ),
        ]
        indexes = [
            models.Index(fields=['session', 'line']),
            models.Index(fields=['session', 'scan_status']),
            models.Index(fields=['barcode_value']),
        ]

    def __str__(self):
        return f"{self.session.bill_number} {self.barcode_value}"


class DispatchSapSyncLog(models.Model):
    session = models.ForeignKey(
        DispatchSession,
        on_delete=models.CASCADE,
        related_name='sap_sync_logs',
    )
    operation = models.CharField(max_length=80)
    request_payload = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Pending'),
            ('SUCCESS', 'Success'),
            ('FAILED', 'Failed'),
        ],
        default='PENDING',
    )
    error_message = models.TextField(blank=True, default='')
    attempt_no = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['session', 'operation', 'status']),
        ]

    def __str__(self):
        return f"{self.session.bill_number} {self.operation} {self.status}"


# ---------------------------------------------------------------------------
# Pallet Verify Request — a ticket asking the barcode team to reconcile a pallet
# ---------------------------------------------------------------------------

class PalletVerifyRequestStatus(models.TextChoices):
    OPEN = 'OPEN', 'Open'
    IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
    RESOLVED = 'RESOLVED', 'Resolved'
    CANCELLED = 'CANCELLED', 'Cancelled'


class PalletVerifyRequestSource(models.TextChoices):
    MANUAL = 'MANUAL', 'Manual'
    DISPATCH = 'DISPATCH', 'Dispatch'


class PalletVerifyRequest(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='pallet_verify_requests'
    )
    pallet = models.ForeignKey(
        'Pallet', on_delete=models.CASCADE,
        related_name='verify_requests'
    )
    status = models.CharField(
        max_length=20, choices=PalletVerifyRequestStatus.choices,
        default=PalletVerifyRequestStatus.OPEN
    )
    source = models.CharField(
        max_length=20, choices=PalletVerifyRequestSource.choices,
        default=PalletVerifyRequestSource.MANUAL
    )
    source_reference = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Originating context, e.g. a dispatch bill number"
    )
    reason = models.TextField(
        blank=True, default='',
        help_text="Requester's note describing the problem"
    )
    findings = models.JSONField(
        default=dict, blank=True,
        help_text="Snapshot of the requester's reconcile result at submit time"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pallet_verify_requests'
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    resolution_note = models.TextField(blank=True, default='')
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pallet_verify_requests_resolved'
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-requested_at']
        verbose_name = 'Pallet Verify Request'
        verbose_name_plural = 'Pallet Verify Requests'
        indexes = [
            models.Index(fields=['company', 'status']),
            models.Index(fields=['requested_by', 'status']),
        ]

    def __str__(self):
        return f"VerifyRequest#{self.pk} {self.pallet_id} [{self.status}]"
