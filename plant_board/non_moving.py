"""
plant_board/non_moving.py

Non-moving stock in the packaging stores, as the Non-Moving dashboard shows it.

THE FILTER IS THE POINT, AGAIN
------------------------------
This must agree with `/dashboards/non-moving`, so it runs that page's own
pipeline rather than a simpler one that looks equivalent and is not:

1. **Fetch with no minimum idle age.** The page does the same (`age = 0`) so
   that its own status split is computed over every row rather than over a
   pre-trimmed set. HANA computes the movement age either way; the age
   parameter only trims what it hands back.

2. **Fold to one line per SKU, keeping the FRESHEST movement of the group.**
   This is the counter-intuitive one and it is deliberate on that page: an item
   being consumed in one store must not read as dead because a pallet of it
   sits untouched in another. Keeping the oldest instead — the obvious choice —
   both inflates the count and ages every line.

3. **Then drop the recently moved rows.** The page's default status filter
   shows slow-moving and non-moving only. Counting the recent ones too is how
   this tile came to report several times the number that page shows.

WHAT THE TILE SHOWS, AND WHAT IT DOES NOT
-----------------------------------------
A count of SKUs and what they are worth. No percentage: with no age floor on
the fetch almost every SKU has an idle day, so a share reads near 100% every
day and says nothing — and the endpoint returns no total-stock denominator to
divide by, so a percentage would need a second, differently-computed read
presented as one ratio.
"""

from typing import Any, Dict, List, Sequence

from non_moving_rm.services import NonMovingRMService

from .constants import (
    MAX_LISTED_ROWS,
    NON_MOVING_DEAD_DAYS,
    NON_MOVING_SLOW_DAYS,
    PACKAGING_ITEM_GROUP,
)

#: No minimum idle age on the fetch — see step 1 above.
NO_AGE_FLOOR = 0


def movement_status(days: int) -> str:
    """`non-moving`, `slow-moving` or `recent`, on the dashboard's thresholds."""
    if days > NON_MOVING_DEAD_DAYS:
        return "non-moving"
    if days >= NON_MOVING_SLOW_DAYS:
        return "slow-moving"
    return "recent"


def non_moving_snapshot(
    company_code: str,
    warehouses: Sequence[str],
    item_group: int = PACKAGING_ITEM_GROUP,
    service: NonMovingRMService = None,
) -> Dict[str, Any]:
    """Idle stock in the given stores: how many SKUs, and what it is worth."""
    service = service or NonMovingRMService(company_code)
    report = service.get_report(age=NO_AGE_FLOOR, item_group=item_group)

    scope = {code.strip().upper() for code in warehouses}
    rows = [
        row
        for row in (report.get("data") or [])
        if (row.get("warehouse") or "").strip().upper() in scope
    ]

    # One line per SKU. Quantity and value add up; the movement age does NOT —
    # the freshest of the group wins, for the reason in the module docstring.
    by_item: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = row.get("item_code")
        if not code:
            continue
        days = int(row.get("days_since_last_movement") or 0)
        slot = by_item.get(code)
        if slot is None:
            by_item[code] = {
                "item_code": code,
                "item_name": row.get("item_name") or "",
                "days": days,
                "value": float(row.get("value") or 0),
                "quantity": float(row.get("quantity") or 0),
                "warehouses": {(row.get("warehouse") or "").strip().upper()},
            }
            continue
        slot["value"] += float(row.get("value") or 0)
        slot["quantity"] += float(row.get("quantity") or 0)
        slot["warehouses"].add((row.get("warehouse") or "").strip().upper())
        if days < slot["days"]:
            slot["days"] = days

    # Now the status filter, on the folded lines — the same order the page uses,
    # because a SKU's status depends on the age the fold settled on.
    idle: List[Dict[str, Any]] = []
    buckets = {"slow-moving": 0, "non-moving": 0}
    bucket_value = {"slow-moving": 0.0, "non-moving": 0.0}
    recent_count = 0

    for slot in by_item.values():
        status = movement_status(slot["days"])
        if status == "recent":
            recent_count += 1
            continue
        buckets[status] += 1
        bucket_value[status] += slot["value"]
        idle.append(
            {
                "item_code": slot["item_code"],
                "item_name": slot["item_name"],
                "days": slot["days"],
                "status": status,
                "value": round(slot["value"], 2),
                "quantity": round(slot["quantity"], 2),
                "warehouses": sorted(w for w in slot["warehouses"] if w),
            }
        )

    # Oldest first, then by money, so the expensive one of two equally idle
    # items is on top.
    idle.sort(key=lambda row: (-row["days"], -row["value"]))

    return {
        "item_count": len(idle),
        "total_value": round(sum(row["value"] for row in idle), 2),
        "slow_moving_count": buckets["slow-moving"],
        "non_moving_count": buckets["non-moving"],
        "slow_moving_value": round(bucket_value["slow-moving"], 2),
        "non_moving_value": round(bucket_value["non-moving"], 2),
        "oldest_days": idle[0]["days"] if idle else 0,
        # Folded SKUs the page also sets aside, so the tile's own scope is
        # visible rather than implied.
        "recent_count": recent_count,
        "warehouses": sorted(scope),
        "items": idle[:MAX_LISTED_ROWS],
        "basis": (
            f"Non-Moving dashboard rules: over {NON_MOVING_DEAD_DAYS} idle days "
            f"is non-moving, {NON_MOVING_SLOW_DAYS}-{NON_MOVING_DEAD_DAYS} is "
            "slow-moving, and a SKU in several stores keeps its freshest "
            "movement."
        ),
    }
