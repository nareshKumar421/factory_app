"""Oil prices: the commodity market prices and Jivo's own pack rates, day by day.

Both come from one published Google Sheet the purchase team keeps
(``exim.price_sheet``), read every night and on demand. EXIM kept the same two
tables (``daily_price.DailyPrice``, ``daily_price.JivoRates``) from the same
sheet; its history is copied across by ``import_exim_prices``.

UNITS
 - A commodity price is rupees per kilogram of loose oil at the factory, then
   with the packing cost added (the sheet adds 14), then with 5% GST, per kg and
   per litre (at 1.0989 L to the kg). All four are the sheet's own figures.
 - A pack rate is rupees per pack (a 1 L pouch, a 15 kg tin ...).
"""

from django.db import models
from django.utils import timezone

from company.models import Company


class PriceSource(models.TextChoices):
    SHEET = "SHEET", "The price sheet"
    #: Copied from EXIM's history, which read the same sheet.
    EXIM = "EXIM", "EXIM"


class CommodityPrice(models.Model):
    """One commodity's market price on one day."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_commodity_prices")
    date = models.DateField()
    commodity = models.CharField(max_length=125)
    factory_price_kg = models.DecimalField(max_digits=10, decimal_places=2)
    packed_price_kg = models.DecimalField(max_digits=10, decimal_places=2)
    with_gst_kg = models.DecimalField(max_digits=10, decimal_places=2)
    with_gst_litre = models.DecimalField(max_digits=10, decimal_places=2)
    source = models.CharField(max_length=5, choices=PriceSource.choices, default=PriceSource.SHEET)
    #: When the sheet was last read for this row (a re-read that day updates it).
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()
        ordering = ["-date", "commodity"]
        constraints = [
            models.UniqueConstraint(fields=["company", "date", "commodity"], name="exim_commodityprice_unique_day"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.commodity} {self.factory_price_kg}"


class PackRate(models.Model):
    """Jivo's rate for one pack of one commodity on one day ("JIVO RATE")."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="exim_pack_rates")
    date = models.DateField()
    pack_type = models.CharField(max_length=125)
    commodity = models.CharField(max_length=125)
    rate = models.DecimalField(max_digits=12, decimal_places=3)
    source = models.CharField(max_length=5, choices=PriceSource.choices, default=PriceSource.SHEET)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()
        ordering = ["-date", "pack_type", "commodity"]
        constraints = [
            models.UniqueConstraint(fields=["company", "date", "pack_type", "commodity"],
                                    name="exim_packrate_unique_day"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.pack_type} {self.commodity} {self.rate}"
