"""
production_dispatch/services.py

Oil's production against its dispatch, SKU by SKU and day by day, in boxes,
litres, tons and pallets -- the workbook "Production & Dispatch- PALLET" made
live, for any range of days.

ONE READ, ADDED UP ON THE PAGE
------------------------------
The server sends the quantities per day and item, and each item's factors; the
page converts them and lays them out as its two sheets -- Summary, a row per
SKU per day, and Items, the same SKUs over the range -- so both are always
added up from the same read. Units follow the workbook:

    Box    = quantity / pieces per box      (``SalFactor2``)
    Liter  = quantity x litres per unit     (``SalPackUn``)
    Ton    = litres x 0.91 / 1000           (net oil)
    PALLET = litres / 800

Both factors are SAP's own. On 2026-10-09 they matched the workbook's on all
168 items it listed but two -- the 700 g and 770 g pouches, which the workbook
read as 0.70 / 0.77 L and SAP converts at the oil's density (0.769 / 0.846 L).
SAP's were chosen. ``NumInSale`` is NOT divided out: the one FG item where it
is not 1 (FG0000426, a 1 + 1 litre combo set) holds 2 L per set in
``SalPackUn`` and would read as 1 L.

GROUP COMPANIES ARE NOT DISPATCH
--------------------------------
Sales to Jivo Mart and the other group companies are left out of every figure
-- the reader's decision, against the workbook, which counted them. The page
still says how much was left out, from ``group_dispatch``.

FAST / SLOW
-----------
Over the 90 days ending on the To date, whatever range is read, so a SKU keeps
its label while the range moves. The workbook's rule, per item:

    Days to Dispatch = average monthly production / average daily dispatch
                     = (P / 3 months) / (D / 90 days) = 30 x P / D
    FAST when that is 30 or less -- a month's production clears in a month.

So FAST is "dispatched at least what was made in the window". Dispatched but
not produced is 0 days, FAST (it is moving out of opening stock); not dispatched
at all is SLOW.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from django.utils import timezone

from sap_client.context import CompanyContext

from .constants import (
    COMPANY_CODE,
    COMPANY_LABEL,
    FAST_DAYS,
    GROUP_CARD_CODES,
    MONTH_DAYS,
    MOVEMENT_WINDOW_DAYS,
    OIL_DENSITY,
    PACKING_TYPE_SPELLINGS,
    PALLET_LITRES,
)
from .hana_reader import DISPATCH, PRODUCTION, ProductionDispatchReader

FAST = "FAST"
SLOW = "SLOW"


def _num(value) -> float:
    return float(value or 0)


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def packing_type(raw: str) -> Optional[str]:
    """SAP's packing type, with its known misspelling read as what it means.

    Blank is ``None`` -- "not set in SAP" -- never a guess from the item name.
    """
    value = " ".join((raw or "").upper().split())
    if not value:
        return None
    return PACKING_TYPE_SPELLINGS.get(value, value)


def movement(production: float, dispatch: float) -> Dict[str, Any]:
    """FAST or SLOW from one item's window totals. See the module docstring."""
    if dispatch <= 0:
        return {"movement": SLOW, "days_to_dispatch": None}
    if production <= 0:
        return {"movement": FAST, "days_to_dispatch": 0.0}
    days = round(MONTH_DAYS * production / dispatch, 1)
    return {"movement": FAST if days <= FAST_DAYS else SLOW, "days_to_dispatch": days}


class ProductionDispatchService:
    """Builds the report for one range. The reader is injectable for tests."""

    def __init__(
        self,
        reader_factory: Callable[[], ProductionDispatchReader] | None = None,
        today: Optional[date] = None,
    ):
        self._reader_factory = reader_factory or (
            lambda: ProductionDispatchReader(CompanyContext(COMPANY_CODE))
        )
        self.today = today or timezone.localdate()

    # ------------------------------------------------------------------
    # The report
    # ------------------------------------------------------------------

    def build(self, date_from: date, date_to: date) -> Dict[str, Any]:
        window_from = date_to - timedelta(days=MOVEMENT_WINDOW_DAYS - 1)
        read_from = min(date_from, window_from)

        reader = self._reader_factory()
        items = {row["ItemCode"]: row for row in reader.items()}
        rows = reader.daily(read_from, date_to, GROUP_CARD_CODES)

        # (day, item) -> its three figures; the window's totals per item.
        days: Dict[tuple, Dict[str, float]] = defaultdict(
            lambda: {"production": 0.0, "dispatch": 0.0, "group_dispatch": 0.0}
        )
        window: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"production": 0.0, "dispatch": 0.0}
        )
        for row in rows:
            day = date.fromisoformat(_iso(row["Day"]))
            code = row["ItemCode"]
            qty = _num(row["Qty"])
            is_group = bool(row["IsGroup"])
            kind = row["Kind"]

            if day >= window_from and not is_group:
                window[code][kind.lower()] += qty

            if day < date_from:
                continue
            figures = days[(day, code)]
            if kind == PRODUCTION:
                figures["production"] += qty
            elif kind == DISPATCH:
                figures["group_dispatch" if is_group else "dispatch"] += qty

        day_rows = [
            {"date": day.isoformat(), "item_code": code, **{k: round(v, 3) for k, v in figures.items()}}
            for (day, code), figures in sorted(days.items())
            if any(figures.values())
        ]

        seen = {row["item_code"] for row in day_rows} | set(window)
        item_rows = [self._item(items[code], window.get(code)) for code in sorted(seen) if code in items]

        return {
            "company": {"code": COMPANY_CODE, "name": COMPANY_LABEL},
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "settings": {
                "pallet_litres": PALLET_LITRES,
                "oil_density": OIL_DENSITY,
                "fast_days": FAST_DAYS,
                "month_days": MONTH_DAYS,
                "movement_window_days": MOVEMENT_WINDOW_DAYS,
            },
            "movement_window": {"from": window_from.isoformat(), "to": date_to.isoformat()},
            "items": item_rows,
            "days": day_rows,
            "meta": {"read_at": timezone.now().isoformat()},
        }

    def _item(self, row: Dict[str, Any], window: Optional[Dict[str, float]]) -> Dict[str, Any]:
        totals = window or {"production": 0.0, "dispatch": 0.0}
        pieces = _num(row["PiecesPerBox"])
        return {
            "item_code": row["ItemCode"],
            "item_name": row["ItemName"],
            "variety": row["Variety"] or None,
            "subgroup": row["Subgroup"] or None,
            "sku": row["Sku"] or None,
            "packing_type": packing_type(row["PackingType"]),
            # A single unit -- a tin, a drum -- is its own box.
            "pieces_per_box": pieces if pieces > 0 else 1.0,
            "litres_per_unit": _num(row["LitresPerUnit"]),
            "window_production": round(totals["production"], 3),
            "window_dispatch": round(totals["dispatch"], 3),
            **movement(totals["production"], totals["dispatch"]),
        }

    # ------------------------------------------------------------------
    # The lines behind it
    # ------------------------------------------------------------------

    def documents(
        self, date_from: date, date_to: date, item_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """Each production and dispatch line in the range: the workbook's Data sheet."""
        reader = self._reader_factory()
        lines = [
            {
                "kind": row["Kind"],
                "doc_type": row["DocType"],
                "date": _iso(row["Day"]),
                "doc_num": row["DocNum"],
                "card_code": row["CardCode"] or None,
                "card_name": row["CardName"] or None,
                "is_group": bool(row["IsGroup"]),
                "warehouse": row["Warehouse"],
                "item_code": row["ItemCode"],
                "item_name": row["ItemName"],
                "quantity": round(_num(row["Qty"]), 3),
            }
            for row in reader.documents(date_from, date_to, GROUP_CARD_CODES, item_code)
        ]
        return {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "item_code": item_code,
            "lines": lines,
        }
