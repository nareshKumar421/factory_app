"""Copy EXIM's price history: its daily commodity prices and its Jivo rates.

EXIM read the same price sheet into ``daily_prices`` and ``jivo_rates`` (from
December 2025 and March 2026). Their rows are copied as they are, marked as
EXIM's. A day this project already holds for a commodity or pack - read from
the sheet here - is left alone, so a re-run only fills the days still missing
(EXIM keeps reading the sheet until it is switched off).
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models_price import CommodityPrice, PackRate, PriceSource

SQL = {
    "prices": (
        "SELECT commodity_name, date, factory_price, packing_cost_kg, with_gst_kg, with_gst_ltr FROM daily_prices"
    ),
    "rates": "SELECT pack_type, commodity, rate, date FROM jivo_rates WHERE rate IS NOT NULL",
}


def _rows(cursor, sql):
    cursor.execute(sql)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def read_exim(cursor) -> dict:
    """Everything the copy needs from EXIM. SELECTs only."""
    return {name: _rows(cursor, sql) for name, sql in SQL.items()}


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


@dataclass
class ImportReport:
    counts: dict = field(default_factory=lambda: {"prices": Counter(), "rates": Counter()})


@transaction.atomic
def import_prices(snapshot: dict, *, company) -> ImportReport:
    report = ImportReport()
    now = timezone.now()

    have = set(CommodityPrice.objects.filter(company=company).values_list("date", "commodity"))
    new = []
    for row in snapshot["prices"]:
        key = (row["date"], _text(row["commodity_name"]))
        if not key[1]:
            continue
        if key in have:
            report.counts["prices"]["kept"] += 1
            continue
        have.add(key)
        new.append(CommodityPrice(
            company=company, date=key[0], commodity=key[1],
            factory_price_kg=_dec(row["factory_price"]), packed_price_kg=_dec(row["packing_cost_kg"]),
            with_gst_kg=_dec(row["with_gst_kg"]), with_gst_litre=_dec(row["with_gst_ltr"]),
            source=PriceSource.EXIM, fetched_at=now,
        ))
    CommodityPrice.objects.bulk_create(new, batch_size=1000)
    report.counts["prices"]["create"] += len(new)

    have = set(PackRate.objects.filter(company=company).values_list("date", "pack_type", "commodity"))
    new = []
    for row in snapshot["rates"]:
        key = (row["date"], _text(row["pack_type"]), _text(row["commodity"]))
        if not key[1] or not key[2]:
            continue
        if key in have:
            report.counts["rates"]["kept"] += 1
            continue
        have.add(key)
        new.append(PackRate(company=company, date=key[0], pack_type=key[1], commodity=key[2],
                            rate=_dec(row["rate"]), source=PriceSource.EXIM, fetched_at=now))
    PackRate.objects.bulk_create(new, batch_size=1000)
    report.counts["rates"]["create"] += len(new)
    return report
