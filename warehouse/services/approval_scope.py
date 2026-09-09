"""What actually has to go to the warehouse for approval — and what does not.

A production run's bill of materials is not one homogeneous list of things the
store has to hand over. These rules sort and narrow it, and they are here rather
than inlined in the request builder so the planning screen and the request itself
cannot drift apart: the screen tells the supervisor what will be requested, and
this is the thing that decides it.

**Raw material is always requested, and on its own document.** Every RM line
goes, at its full quantity, whatever the register says is in the tank and
whatever is staged at the line. It travels as a separate request from the
packing material because the two are settled against different evidence: RM
against the store keeper's Raw Material register, PM against SAP stock. One
document mixing them could not be approved coherently.

**Packing material is requested only when it has to be fetched.** `BH-PC` is
Production Consumption — material already pulled to the line. What is sitting
there needs nobody's permission to use. Only the part that must come out of a
main godown (`BH-PS`, `BH-PM`, ...) is a real request on the store's time.

**And only the shortfall of it.** If the line needs 4,000 caps and 3,000 are
already at BH-PC, the request is for 1,000 — not 4,000. Requesting the full
quantity would have the store issue material that is already at the line.

A plan where every line falls away under the packing rule, with no raw material
on the bill at all, needs no approval.
That is a normal outcome, not an error, and the caller marks the run
`NOT_REQUIRED` so it can start rather than waiting forever on a request nobody
was ever asked to make.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from django.conf import settings

ZERO = Decimal("0")

# Production Consumption — the staging area at the line. Overridable because
# the code is company data, not a law of nature.
DEFAULT_PRODUCTION_CONSUMPTION_WAREHOUSE = "BH-PC"

# SAP item-group classification, as `planning_purchase.hana_reader` returns it.
MATERIAL_RAW = "RAW"
MATERIAL_PACKAGING = "PACKAGING"
MATERIAL_OTHER = "OTHER"


def production_consumption_warehouse() -> str:
    return str(
        getattr(
            settings,
            "PRODUCTION_CONSUMPTION_WAREHOUSE",
            DEFAULT_PRODUCTION_CONSUMPTION_WAREHOUSE,
        )
    ).strip().upper()


def _dec(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def split_pick(required, at_production_consumption) -> Dict[str, Decimal]:
    """How much of a line comes off the line's own staging, and how much is fetched.

    Negative or missing BH-PC stock is treated as nothing there — SAP can report
    a negative on-hand, and "minus 40 caps are already at the line" is not a
    sentence that should reduce a request.
    """
    need = max(ZERO, _dec(required))
    available = max(ZERO, _dec(at_production_consumption))
    from_pc = min(need, available)
    return {
        # Named `required_qty`, not `required`: callers spread this dict beside
        # a boolean `required` flag, and a quantity silently overwriting that
        # flag turns "no approval needed" into a truthy 2,000.
        "required_qty": need,
        "from_production_consumption": from_pc,
        "from_other_warehouses": need - from_pc,
    }


def line_approval(
    material_type: str,
    required,
    at_production_consumption,
) -> Dict[str, Any]:
    """Whether one BOM line goes to the warehouse, and for how much.

    `OTHER` — a component SAP groups as neither raw nor packaging — is treated
    like packing material rather than like RM. It is the catch-all bucket, so
    the conservative reading is that it still passes through the store; dropping
    it from the request because it failed to classify would silently stop
    someone being asked for material they have to hand over.
    """
    split = split_pick(required, at_production_consumption)
    pc_code = production_consumption_warehouse()

    if (material_type or "").upper() == MATERIAL_RAW:
        # Always asked for, in full. BH-PC staging does not reduce a raw-material
        # request the way it reduces a packing one — the oil still has to be
        # released against the register.
        return {
            "required": True,
            "qty": split["required_qty"],
            "reason": "Raw material is always requested, on its own document, "
                      "and settled against the Raw Material register.",
            **split,
        }

    if split["from_other_warehouses"] <= ZERO:
        covered = split["from_production_consumption"]
        return {
            "required": False,
            "qty": ZERO,
            "reason": (
                f"All {covered:,.3f} is already at {pc_code}, so nothing has to be "
                f"fetched from another godown."
                if covered > ZERO
                else "Nothing to fetch."
            ),
            **split,
        }

    reason = f"To be fetched from a godown other than {pc_code}."
    if split["from_production_consumption"] > ZERO:
        reason = (
            f"{split['from_production_consumption']:,.3f} is already at {pc_code}; "
            f"the remaining {split['from_other_warehouses']:,.3f} must be fetched "
            f"from another godown."
        )

    return {
        "required": True,
        "qty": split["from_other_warehouses"],
        "reason": reason,
        **split,
    }


def production_consumption_qty(warehouse_rows, code: Optional[str] = None) -> Decimal:
    """Pull the BH-PC on-hand out of a per-warehouse breakdown.

    Accepts either shape the two callers hold: the plan check's
    ``[{"warehouse": ..., "on_hand": ...}]`` and the warehouse service's
    ``[{"WhsCode": ..., "OnHand": ...}]``.
    """
    target = (code or production_consumption_warehouse()).upper()
    for row in warehouse_rows or []:
        whs = (row.get("warehouse") or row.get("WhsCode") or "").upper()
        if whs == target:
            value = row.get("on_hand")
            if value is None:
                value = row.get("OnHand")
            return _dec(value)
    return ZERO
