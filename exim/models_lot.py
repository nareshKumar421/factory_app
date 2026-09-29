"""Oil lots: every lot of raw or imported oil from contract to tank.

EXIM called this "Stock Status" (``stock.StockStatus``), which it is not: a lot
is bought (IN_CONTRACT), loaded and shipped, sits at a port or a refinery, is
trucked to the factory gate (OUT_SIDE_FACTORY), and is weighed into the tank
farm (IN_TANK). When the farm has used it up it is COMPLETED. The rules that
move a lot between those are in ``exim.services_lot``.

UNITS
 - ``quantity`` is KILOGRAMS and ``rate`` is per kilogram, as the lot is bought.
 - ``quantity_litres`` and ``rate_per_litre`` are worked out from them at
   ``DENSITY`` (1.0989 L to the kg) on every save, as EXIM did.
 - A shortage is in METRIC TONNES and its rate per tonne.

SPLITS
Dispatching part of a lot makes a new lot for the part that moved; ``parent``
points it back at the lot it was split from (or at that lot's own storage
parent). ``is_accumulator`` marks the one lot that collects every arrival of a
parent at a refinery.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from company.models import Company

from .models_tank import TankItem


class LotStatus(models.TextChoices):
    """EXIM's statuses, in the order a lot usually moves through them."""

    IN_CONTRACT = "IN_CONTRACT", "In contract"
    UNDER_LOADING = "UNDER_LOADING", "Under loading"
    ON_THE_SEA = "ON_THE_SEA", "On the sea"
    MUNDRA_PORT = "MUNDRA_PORT", "Mundra port"
    KANDLA_STORAGE = "KANDLA_STORAGE", "Kandla storage"
    OTW_TO_REFINERY = "OTW_TO_REFINERY", "On the way to refinery"
    AT_REFINERY = "AT_REFINERY", "At refinery"
    ON_THE_WAY = "ON_THE_WAY", "On the way"
    OUT_SIDE_FACTORY = "OUT_SIDE_FACTORY", "Outside factory"
    IN_TANK = "IN_TANK", "In tank"
    IN_WAREHOUSE = "IN_WAREHOUSE", "In warehouse"
    COMPLETED = "COMPLETED", "Completed"
    # EXIM defines these and its screens hide them. Kept so a copied lot that
    # carries one is still valid.
    DELIVERED = "DELIVERED", "Delivered"
    IN_TRANSIT = "IN_TRANSIT", "In transit"
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"


#: Where a lot can rest: dispatching from one of these makes it the parent of
#: what was dispatched. EXIM's ``STORAGE_STATUSES``.
STORAGE_STATUSES = frozenset(
    {
        LotStatus.IN_CONTRACT,
        LotStatus.AT_REFINERY,
        LotStatus.KANDLA_STORAGE,
        LotStatus.MUNDRA_PORT,
        LotStatus.IN_TANK,
        LotStatus.OUT_SIDE_FACTORY,
    }
)


class PaymentStatus(models.TextChoices):
    PAID = "PAID", "Paid"
    UNPAID = "UNPAID", "Unpaid"


class OilLot(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_lots")
    item = models.ForeignKey(TankItem, on_delete=models.PROTECT, related_name="lots")
    status = models.CharField(max_length=30, choices=LotStatus.choices)

    #: The SAP vendor code, or a temporary one (``TemporaryVendor``) for a
    #: vendor SAP does not have yet. The name is kept beside it as it was when
    #: the lot was entered.
    vendor_code = models.CharField(max_length=50)
    vendor_name = models.CharField(max_length=255, blank=True, default="")

    #: Per kilogram.
    rate = models.DecimalField(max_digits=12, decimal_places=3)
    #: Kilograms.
    quantity = models.DecimalField(max_digits=14, decimal_places=2)
    #: ``quantity`` x ``rate``; worked out on save.
    total = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    #: Worked out on save at ``DENSITY``.
    rate_per_litre = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    quantity_litres = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    #: The refinery that processes the lot on job work, named as it arrives there.
    job_work = models.CharField(max_length=255, blank=True, default="")
    vehicle_number = models.CharField(max_length=50, blank=True, default="")
    transporter = models.CharField(max_length=255, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    eta = models.DateField(null=True, blank=True)
    arrival_date = models.DateField(null=True, blank=True)

    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children",
    )
    is_accumulator = models.BooleanField(default=False)

    #: EXIM's ``bility_number``: the transporter's consignment note.
    bilty_number = models.CharField(max_length=100, blank=True, default="")
    grpo_number = models.CharField(max_length=100, blank=True, default="")
    payment_status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID,
    )
    contract_start = models.DateField(null=True, blank=True)
    contract_end = models.DateField(null=True, blank=True)

    #: A removed lot stays for its history and its splits; every list skips it.
    #: A lot whose quantity reaches nothing is removed on save, as in EXIM.
    deleted = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    #: Who entered it, as EXIM wrote it (an email) for a copied lot.
    created_by_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    #: EXIM's id for a lot copied from it.
    exim_id = models.BigIntegerField(null=True, blank=True, unique=True)
    copied_from_exim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["-id"]
        indexes = [models.Index(fields=["company", "status", "deleted"])]

    def __str__(self) -> str:
        return f"Lot #{self.pk} {self.item_id} {self.status}"

    @property
    def can_be_parent(self) -> bool:
        return self.status in STORAGE_STATUSES


class LotShortage(models.Model):
    """What was lost between loading and the tank, and what the supplier is debited.

    EXIM's ``DebitEntry``, written when a lot is weighed into the tank at less
    than it was loaded. The first 0.25% of the loaded quantity is allowed; the
    rest is debited at the lot's rate.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_shortages")
    lot = models.ForeignKey(
        OilLot, on_delete=models.SET_NULL, null=True, blank=True, related_name="shortages",
    )
    item_code = models.CharField(max_length=50, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    supplier_code = models.CharField(max_length=50, blank=True, default="")
    supplier = models.CharField(max_length=255, blank=True, default="")
    vehicle_number = models.CharField(max_length=50, blank=True, default="")
    transporter = models.CharField(max_length=255, blank=True, default="")
    bilty_number = models.CharField(max_length=100, blank=True, default="")
    grpo_number = models.CharField(max_length=100, blank=True, default="")

    #: Per metric tonne.
    rate = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    load_qty_mt = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    unload_qty_mt = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    shortage_mt = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    allowed_mt = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    deducted_mt = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    deduction_amount = models.DecimalField(max_digits=20, decimal_places=3, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_by_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    exim_id = models.BigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["-created_at", "-id"]


class LotChangeAction(models.TextChoices):
    CREATE = "CREATE", "Created"
    UPDATE = "UPDATE", "Changed"


class LotChange(models.Model):
    """One save of a lot: who, when, why. Its field changes hang off it."""

    lot = models.ForeignKey(OilLot, on_delete=models.CASCADE, related_name="changes")
    action = models.CharField(max_length=10, choices=LotChangeAction.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    changed_by_label = models.CharField(max_length=255, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    timestamp = models.DateTimeField(default=timezone.now)

    #: EXIM's session uuid for a copied change.
    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["-timestamp", "-id"]


class LotFieldChange(models.Model):
    change = models.ForeignKey(LotChange, on_delete=models.CASCADE, related_name="field_changes")
    #: A field name, or "__create__" for the lot's first values.
    field_name = models.CharField(max_length=100)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["id"]


class ContractHistory(models.Model):
    """A contract rate agreed with a vendor for an oil, and the period it ran.

    EXIM's ``ContractualHistory``. EXIM stopped writing it in May 2026 (the code
    that did is commented out there), so it arrives as a register of past
    contracts, read-only.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_contract_history")
    item_code = models.CharField(max_length=50, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    vendor_code = models.CharField(max_length=50, blank=True, default="")
    vendor_name = models.CharField(max_length=255, blank=True, default="")
    rate = models.DecimalField(max_digits=12, decimal_places=3)
    contract_start = models.DateField(null=True, blank=True)
    contract_end = models.DateField(null=True, blank=True)
    created_by_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    exim_id = models.BigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["-created_at", "-id"]


class StockDashboardRow(models.Model):
    """Where an oil sits on the stock dashboard. One order, shared by everybody."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_dashboard_rows")
    item = models.ForeignKey(TankItem, on_delete=models.CASCADE, related_name="+")
    position = models.IntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "item"], name="exim_dashboardrow_unique_item"),
        ]


class TemporaryVendor(models.Model):
    """A vendor a lot is bought from before SAP has a code for it.

    Numbered TEMP0001, TEMP0002 ... as in EXIM. Real vendors are read from SAP
    as the lot is entered; nothing else of SAP's vendor master is kept here.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_temporary_vendors")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="exim_tempvendor_unique_code"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
