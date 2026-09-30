"""Reading the price sheet: the Google Sheet the purchase team publishes as CSV.

Two tables are read from it, found by their labels rather than by cell, since
the sheet is edited by hand and other working tables sit around them:

 - "Commodities" heads the day's commodity prices: each row below it names a
   commodity, then (every second column) the factory price per kg, the price
   with packing, with GST per kg and with GST per litre. The column between each
   pair holds a working figure (the GST amount) and is skipped. The table ends
   at the first row with no commodity.
 - "JIVO RATE" heads Jivo's own rates: the commodities across that row, the pack
   types down its column (a 1 L pouch, a 15 kg tin ...), a rate where they meet.
   The block ends at the first row with no pack. The same commodity names recur
   in other tables further along the row, so each is taken from its first
   appearance after the label, as EXIM did.

This is EXIM's reader (``daily_price.services``) with its fixed row count and
pack list dropped, so a commodity or pack added to the sheet is not missed.
"""

import csv
import io
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: EXIM's published CSV of the sheet; override with EXIM_PRICE_SHEET_URL.
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vR2LwtfXKkkDiVzOc_T591-4KWwUvKW-ZaJokeixIzHkOyHNSjGv5Il"
    "h3597ZgaMA/pub?gid=655973128&single=true&output=csv"
)

#: The commodities the JIVO RATE block quotes, as the sheet heads them.
RATE_COMMODITIES = ("SOYA", "Mustard", "Sunflower", "Cotton Refined", "Ricebran Refined")


class PriceSheetError(Exception):
    """The sheet could not be read, or the table was not where it should be."""


def _text(cell) -> str:
    return " ".join(str(cell or "").split())


def _number(cell):
    text = _text(cell).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def sheet_url() -> str:
    """The published CSV the prices are read from."""
    return getattr(settings, "EXIM_PRICE_SHEET_URL", "") or DEFAULT_SHEET_URL


def sheet_view_url() -> str:
    """The same published sheet as a page a person can read (Google's
    ``pubhtml`` rather than its CSV download)."""
    url = sheet_url()
    if "/pub?" in url and "output=csv" in url:
        url = url.replace("/pub?", "/pubhtml?").replace("&output=csv", "").replace("output=csv&", "")
    return url


def fetch_rows(url: str = None) -> list:
    url = url or sheet_url()
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("exim: price sheet fetch failed: %s", exc)
        raise PriceSheetError("The price sheet could not be read from Google. Try again shortly.") from exc
    response.encoding = "utf-8"
    return list(csv.reader(io.StringIO(response.text)))


def _find(rows, label, *, exact=False):
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            text = _text(cell)
            if (text == label) if exact else (label in text):
                return r, c
    return None


def _cell(row, col):
    return row[col] if col < len(row) else ""


def commodity_prices(rows) -> list:
    """The "Commodities" table: [{commodity, factory_price_kg, packed_price_kg,
    with_gst_kg, with_gst_litre}]."""
    found = _find(rows, "Commodities")
    if found is None:
        raise PriceSheetError('The sheet has no "Commodities" table.')
    top, col = found
    prices = []
    for row in rows[top + 1:]:
        name = _text(_cell(row, col))
        if not name:
            break
        figures = [_number(_cell(row, col + offset)) for offset in (2, 4, 6, 8)]
        if figures[0] is None:
            continue  # a note under the table, not a price
        factory, packed, gst_kg, gst_litre = (value if value is not None else Decimal("0") for value in figures)
        prices.append({
            "commodity": name,
            "factory_price_kg": factory,
            "packed_price_kg": packed,
            "with_gst_kg": gst_kg,
            "with_gst_litre": gst_litre,
        })
    if not prices:
        raise PriceSheetError('The "Commodities" table on the sheet is empty.')
    return prices


def pack_rates(rows) -> list:
    """The "JIVO RATE" block: [{pack_type, commodity, rate}]."""
    found = _find(rows, "JIVO RATE", exact=True)
    if found is None:
        raise PriceSheetError('The sheet has no "JIVO RATE" table.')
    top, col = found
    header = rows[top]
    columns = {}
    for c in range(col + 1, len(header)):
        name = _text(header[c])
        if name in RATE_COMMODITIES and name not in columns:
            columns[name] = c
    if not columns:
        raise PriceSheetError('The "JIVO RATE" table names none of its commodities.')
    rates = []
    for row in rows[top + 1:]:
        pack = _text(_cell(row, col))
        if not pack:
            break
        for commodity, c in columns.items():
            rate = _number(_cell(row, c))
            if rate is not None:
                rates.append({"pack_type": pack, "commodity": commodity, "rate": rate})
    return rates


def read_sheet(url: str = None) -> dict:
    """Both tables, read once. Raises PriceSheetError."""
    rows = fetch_rows(url)
    return {"prices": commodity_prices(rows), "rates": pack_rates(rows)}
