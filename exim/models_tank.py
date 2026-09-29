"""The tank farm: the oils it holds, the vessels, and what arrived into them.

Carried over from EXIM's ``tank`` app, where these were ``TankItem``,
``TankData`` and ``TankLog``.

UNITS, BECAUSE A WRONG GUESS IS A 1,000x ERROR
 - A tank's capacity and level are LITRES, as EXIM kept them (a 50 T tank reads
   50,000). ``admin_board`` divides by 1,000 for its tonnes.
 - A tank log's quantity is KILOGRAMS and its rate is per kilogram: it is the
   oil lot's own figure at the moment the lot went into the tank.

A tank's level is set by hand, as it was in EXIM: the store reads the dip and
enters it. Nothing here derives a level from the lots that went in, because the
oil also leaves the tank for the line and nothing records that leg.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from company.models import Company


class OilCategory(models.TextChoices):
    """EXIM's ``SUB_GROUP_CHOICES``. Its "OILVE" is spelled properly here."""

    SOYABEAN = "SOYABEAN", "Soyabean"
    OLIVE = "OLIVE", "Olive"
    CANOLA = "CANOLA", "Canola"
    MUSTARD = "MUSTARD", "Mustard"
    GROUNDNUT = "GROUNDNUT", "Groundnut"
    GHEE = "GHEE", "Ghee"
    SUNFLOWER = "SUNFLOWER", "Sunflower"
    RICE_BRAN = "RICE BRAN", "Rice bran"
    COCONUT = "COCONUT", "Coconut"
    SESAME = "SESAME", "Sesame"
    EXTRA_VIRGIN = "EXTRA VIRGIN", "Extra virgin"
    COTTON_SEED = "COTTON SEED", "Cotton seed"
    BLENDED = "BLENDED", "Blended"


class TankItem(models.Model):
    """An oil the farm holds and lots are bought as: "CANOLA 2B", "POMACE 3C".

    EXIM's own master, not SAP's item list: its codes (``RM00CN2``,
    ``SBTIN15KG``) are the tank farm's, and the colour is what the tank drawings
    are painted in.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_tank_items")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=20, choices=OilCategory.choices, blank=True, default="")
    #: A hex colour, "#d95c26".
    color = models.CharField(max_length=10, blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: EXIM's uuid for an item copied from it.
    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)
    copied_from_exim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="exim_tankitem_unique_code"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class TankKind(models.TextChoices):
    TANK = "TANK", "Tank"
    #: An IBC tote: holds the same oil, is not part of the farm's headline fill.
    TOTE = "TOTES", "Tote"


class Tank(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_tanks")
    #: TNK0001 for a tank, TOT001 for a tote: the lowest free number, as in EXIM.
    code = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=TankKind.choices, default=TankKind.TANK)
    #: What it holds now. None when empty.
    item = models.ForeignKey(
        TankItem, on_delete=models.PROTECT, null=True, blank=True, related_name="tanks",
    )
    capacity_l = models.DecimalField(max_digits=12, decimal_places=2)
    #: The level as last dipped. EXIM's ``current_capacity`` - the stock, not a capacity.
    level_l = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: EXIM's tank code for a tank copied from it.
    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)
    copied_from_exim_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="exim_tank_unique_code"),
        ]

    def __str__(self) -> str:
        return self.code


class TankLogKind(models.TextChoices):
    INWARD = "INWARD", "Inward"
    OUTWARD = "OUTWARD", "Outward"
    TRANSFER = "TRANSFER", "Transfer"


class TankLog(models.Model):
    """An oil lot going into the tank farm, written as the lot turns IN_TANK.

    A record of the arrival, not of a particular vessel: EXIM never said which
    tank a lot was pumped into, and the level is dipped by hand. The lot's
    figures are copied on, so the log still reads if the lot is later changed.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_tank_logs")
    kind = models.CharField(max_length=10, choices=TankLogKind.choices, default=TankLogKind.INWARD)
    lot = models.ForeignKey(
        "exim.OilLot", on_delete=models.SET_NULL, null=True, blank=True, related_name="tank_logs",
    )
    #: Kilograms.
    quantity_kg = models.DecimalField(max_digits=14, decimal_places=2)
    #: Per kilogram.
    rate = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    vehicle_number = models.CharField(max_length=50, blank=True, default="")
    party = models.CharField(max_length=255, blank=True, default="")
    item_code = models.CharField(max_length=50, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    #: The lot's expected arrival, as EXIM recorded it.
    arrival = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    #: Who, as EXIM wrote it (an email) for a copied row.
    created_by_label = models.CharField(max_length=255, blank=True, default="")
    #: Not auto_now_add: a copied row keeps the moment EXIM recorded it.
    created_at = models.DateTimeField(default=timezone.now)

    exim_ref = models.CharField(max_length=80, null=True, blank=True, unique=True)

    class Meta:
        default_permissions = ()
        ordering = ["-created_at", "-id"]
