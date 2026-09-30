"""Oil prices: saving a day from the price sheet, and reading days back.

A day's figures are the sheet as read that day. Reading it again the same day
(the nightly run, or somebody pressing Fetch) replaces that day's figures, as
EXIM's update-or-create did; earlier days are never touched. A commodity or pack
the sheet stops quoting simply has no row for the days it was missing.
"""

from datetime import date
from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models_price import CommodityPrice, PackRate, PriceSource
from .services_licence import EximError

PRICE_FIELDS = ("factory_price_kg", "packed_price_kg", "with_gst_kg", "with_gst_litre")

#: A range longer than this is refused: the trends chart reads it in one go.
MAX_RANGE_DAYS = 400


@transaction.atomic
def save_prices(company, prices: list, *, day: Optional[date] = None) -> dict:
    """Upsert one day's commodity prices. Returns {"created": n, "updated": n}."""
    day = day or timezone.localdate()
    now = timezone.now()
    counts = {"created": 0, "updated": 0}
    for row in prices:
        _, created = CommodityPrice.objects.update_or_create(
            company=company, date=day, commodity=row["commodity"],
            defaults={**{f: row[f] for f in PRICE_FIELDS}, "source": PriceSource.SHEET, "fetched_at": now},
        )
        counts["created" if created else "updated"] += 1
    return counts


@transaction.atomic
def save_rates(company, rates: list, *, day: Optional[date] = None) -> dict:
    """Upsert one day's pack rates. Returns {"created": n, "updated": n}."""
    day = day or timezone.localdate()
    now = timezone.now()
    counts = {"created": 0, "updated": 0}
    for row in rates:
        _, created = PackRate.objects.update_or_create(
            company=company, date=day, pack_type=row["pack_type"], commodity=row["commodity"],
            defaults={"rate": row["rate"], "source": PriceSource.SHEET, "fetched_at": now},
        )
        counts["created" if created else "updated"] += 1
    return counts


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def _latest_on_or_before(model, company, day: Optional[date]) -> Optional[date]:
    rows = model.objects.filter(company=company)
    if day is not None:
        rows = rows.filter(date__lte=day)
    return rows.order_by("-date").values_list("date", flat=True).first()


def _previous(model, company, day: date) -> Optional[date]:
    return (
        model.objects.filter(company=company, date__lt=day)
        .order_by("-date").values_list("date", flat=True).first()
    )


def _next(model, company, day: date) -> Optional[date]:
    return (
        model.objects.filter(company=company, date__gt=day)
        .order_by("date").values_list("date", flat=True).first()
    )


def _span(model, company) -> dict:
    from .price_sheet import sheet_view_url

    first = model.objects.filter(company=company).order_by("date").values_list("date", flat=True).first()
    last = model.objects.filter(company=company).order_by("-date").values_list("date", flat=True).first()
    return {"first_date": first, "last_date": last, "sheet_url": sheet_view_url()}


def _price(row: CommodityPrice) -> dict:
    return {"commodity": row.commodity, **{f: getattr(row, f) for f in PRICE_FIELDS}, "source": row.source,
            "fetched_at": row.fetched_at}


def price_day(company, day: Optional[date] = None) -> dict:
    """The commodity prices of ``day`` (or the latest day on or before it),
    each beside the previous day the sheet was read."""
    found = _latest_on_or_before(CommodityPrice, company, day)
    result = {"asked": day, "date": found, "previous_date": None, "next_date": None, "prices": [],
              **_span(CommodityPrice, company)}
    if found is None:
        return result
    before = _previous(CommodityPrice, company, found)
    previous = {
        row.commodity: row for row in CommodityPrice.objects.filter(company=company, date=before)
    } if before else {}
    rows = []
    for row in CommodityPrice.objects.filter(company=company, date=found).order_by("commodity"):
        item = _price(row)
        prior = previous.get(row.commodity)
        item["previous"] = {f: getattr(prior, f) for f in PRICE_FIELDS} if prior else None
        rows.append(item)
    result.update(previous_date=before, next_date=_next(CommodityPrice, company, found), prices=rows)
    return result


def rate_day(company, day: Optional[date] = None) -> dict:
    """Jivo's pack rates of ``day`` (or the latest day on or before it), each
    beside the previous day's."""
    found = _latest_on_or_before(PackRate, company, day)
    result = {"asked": day, "date": found, "previous_date": None, "next_date": None, "rates": [], "packs": [],
              "commodities": [], **_span(PackRate, company)}
    if found is None:
        return result
    before = _previous(PackRate, company, found)
    previous = {
        (row.pack_type, row.commodity): row.rate for row in PackRate.objects.filter(company=company, date=before)
    } if before else {}
    rows = list(PackRate.objects.filter(company=company, date=found).order_by("pack_type", "commodity"))
    result.update(
        previous_date=before,
        next_date=_next(PackRate, company, found),
        rates=[{"pack_type": r.pack_type, "commodity": r.commodity, "rate": r.rate,
                "previous": previous.get((r.pack_type, r.commodity)), "source": r.source,
                "fetched_at": r.fetched_at} for r in rows],
        packs=sorted({r.pack_type for r in rows}),
        commodities=sorted({r.commodity for r in rows}),
    )
    return result


def _check_range(start: date, end: date) -> None:
    if start > end:
        raise EximError("The range starts after it ends.", "bad_range", {})
    if (end - start).days > MAX_RANGE_DAYS:
        raise EximError(f"Pick at most {MAX_RANGE_DAYS} days.", "range_too_long", {"max_days": MAX_RANGE_DAYS})


def price_range(company, start: date, end: date) -> dict:
    _check_range(start, end)
    rows = CommodityPrice.objects.filter(company=company, date__range=(start, end)).order_by("date", "commodity")
    return {"from": start, "to": end, "rows": [{"date": r.date, **_price(r)} for r in rows]}


def rate_range(company, start: date, end: date) -> dict:
    _check_range(start, end)
    rows = PackRate.objects.filter(company=company, date__range=(start, end)).order_by("date", "pack_type",
                                                                                         "commodity")
    return {"from": start, "to": end, "rows": [
        {"date": r.date, "pack_type": r.pack_type, "commodity": r.commodity, "rate": r.rate} for r in rows
    ]}
