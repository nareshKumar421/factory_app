"""Warehouse Inventory: oil in SAP's warehouses, in litres, by category.

EXIM's Warehouse Inventory, read for the request's company. Each warehouse's
litres per category (the oil's sub-group, with EXIM's overrides), raw material
and finished goods apart; a warehouse's items on request. Kept two minutes in
the shared cache. A negative balance is SAP's own (stock issued before it was
received) and is shown, not made positive as EXIM did.
"""

import logging
from collections import defaultdict
from decimal import Decimal

from django.core.cache import caches
from django.utils import timezone

from .hana_reader import warehouse_litres

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120

#: The warehouses EXIM's screen showed: raw material, then finished goods.
DEFAULT_WAREHOUSES = ("BH-CRUDE", "BH-GJ", "BH-EX", "BH-PC", "BH-VA", "BH-PF", "BH-LO", "BH-EC", "GP-FG")


def _rows(company, refresh: bool) -> dict:
    cache = caches["shared"]
    key = f"exim:warehouse_litres:{company.code}"
    data = None
    if not refresh:
        try:
            data = cache.get(key)
        except Exception as exc:
            logger.warning("warehouse inventory: cache read failed: %s", exc)
    if data is None:
        data = {"rows": warehouse_litres(company.code), "read_at": timezone.now()}
        try:
            cache.set(key, data, CACHE_SECONDS)
        except Exception as exc:
            logger.warning("warehouse inventory: cache write failed: %s", exc)
    return data


def warehouse_inventory(company, *, warehouse: str = "", refresh=False) -> dict:
    """Litres by warehouse and category; with ``warehouse``, its items too."""
    data = _rows(company, refresh)
    groups = defaultdict(lambda: {"litres": Decimal("0"), "items": 0, "negative_items": 0})
    names = {}
    for row in data["rows"]:
        names[row["warehouse"]] = row["warehouse_name"]
        group = groups[(row["warehouse"], row["kind"], row["category"])]
        group["litres"] += row["litres"]
        group["items"] += 1
        if row["litres"] < 0:
            group["negative_items"] += 1
    warehouses = defaultdict(lambda: {"litres": Decimal("0"), "kinds": set(), "categories": [],
                                      "negative_items": 0})
    for (whs, kind, category), g in sorted(groups.items()):
        w = warehouses[whs]
        w["litres"] += g["litres"]
        w["kinds"].add(kind)
        w["negative_items"] += g["negative_items"]
        w["categories"].append({"kind": kind, "category": category, **g})
    out = []
    for whs, w in warehouses.items():
        w["categories"].sort(key=lambda c: c["litres"], reverse=True)
        out.append({"warehouse": whs, "warehouse_name": names.get(whs, ""), "litres": w["litres"],
                    "kinds": sorted(w["kinds"]), "negative_items": w["negative_items"],
                    "categories": w["categories"]})
    out.sort(key=lambda w: (w["warehouse"] not in DEFAULT_WAREHOUSES,
                            DEFAULT_WAREHOUSES.index(w["warehouse"]) if w["warehouse"] in DEFAULT_WAREHOUSES else 0,
                            w["warehouse"]))
    result = {
        "read_at": data["read_at"],
        "default_warehouses": [w for w in DEFAULT_WAREHOUSES if w in warehouses],
        "warehouses": out,
        "items": None,
    }
    if warehouse:
        result["items"] = sorted((r for r in data["rows"] if r["warehouse"] == warehouse),
                                 key=lambda r: (r["category"], -r["litres"]))
    return result
