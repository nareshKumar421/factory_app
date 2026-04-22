from django.db import models
from django.conf import settings


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class PalletStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CLEARED = "CLEARED", "Cleared"
    SPLIT = "SPLIT", "Split"
    VOID = "VOID", "Void"


class BoxStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    PARTIAL = "PARTIAL", "Partial"
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
    DISMANTLE = "DISMANTLE", "Dismantle"
    CLEAR = "CLEAR", "Clear"
    SPLIT = "SPLIT", "Split"
    VOID = "VOID", "Void"


class BoxMovementType(models.TextChoices):
    CREATE = "CREATE", "Create"
    MOVE = "MOVE", "Move"
    TRANSFER = "TRANSFER", "Transfer"
    PALLETIZE = "PALLETIZE", "Palletize"
    DEPALLETIZE = "DEPALLETIZE", "Depalletize"
    DISMANTLE = "DISMANTLE", "Dismantle"
    VOID = "VOID", "Void"


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

    class Meta:
        ordering = ['-performed_at']
        verbose_name = 'Box Movement'
        verbose_name_plural = 'Box Movements'

    def __str__(self):
        return f"{self.movement_type} — {self.box.box_barcode}"


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
