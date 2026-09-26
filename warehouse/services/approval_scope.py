"""What actually has to go to the warehouse for approval — and what does not.

A production run's bill of materials is not one homogeneous list of things the
store has to hand over. These rules sort and narrow it, and they are here rather
than inlined in the request builder so the planning screen and the request itself
cannot drift apart: the screen tells the supervisor what will be requested, and
this is the thing that decides it.

**Raw material travels on its own document, and only for what is not already
at the line.** RM is a separate request from the packing material because the
two are settled against different evidence: RM against the store keeper's Raw
Material register, PM against SAP stock. One document mixing them could not be
approved coherently.

**Oil staged at the line is netted off the same way caps are.** A bill that
needs 24,000 litres of a loose oil consumed at `BH-PC`, with 22,834 already
standing there, is a request for 1,165 — not 24,000. The store keeper only ever
had 10,000 in the tank to release against, so the full-quantity request could
not be approved at all, and an approver reading `0.000` was told nothing about
the 22,834 litres that make the run perfectly runnable.

**Except out of the register's own warehouse.** If a bill consumes raw material
straight out of `BH-LO`, "what is already at the line" *is* the tank, and the
register is the evidence for it. Netting it would cancel every oil request
against the very figure the approval is checked against, so a line consumed from
the register's warehouse is still asked for in full.

**Packing material is requested only when it has to be fetched.** A production
consumption warehouse holds material already pulled to the line. What is sitting
there needs nobody's permission to use. Only the part that must come out of a
main godown (`BH-PM`, `BH-BS`, ...) is a real request on the store's time.

**Which warehouse counts as "at the line" is the company's own, not a constant.**
It is the RM or PM warehouse in the company's production settings
(`production_execution.services.settings_service`), whatever warehouse a bill
line happens to name. The plant does not use one warehouse: Oil stages at
`BH-PC`, and *every* Beverages line consumes from `BH-PP`. Netting a global
`BH-PC` off a Beverages line subtracts a warehouse that holds nothing and
ignores the one holding millions of already-staged pieces, so the request goes
out at full quantity and the store is asked to fetch what is already at the
line — which is why Beverages' settings start on `BH-PP`.

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
    """The fallback consumption warehouse, for a bill that names none."""
    return str(
        getattr(
            settings,
            "PRODUCTION_CONSUMPTION_WAREHOUSE",
            DEFAULT_PRODUCTION_CONSUMPTION_WAREHOUSE,
        )
    ).strip().upper()


def register_warehouse() -> str:
    """The warehouse the Raw Material register itself covers.

    Read from the register's own service rather than copied, so the one place
    that knows which store keepers type counts against stays the one place.
    Imported inside the function: the register service reaches into SAP readers
    at import time and this module is imported by the planning screen.
    """
    from .rm_stock_service import register_warehouse as _register_warehouse

    return _register_warehouse()


def consumption_warehouse_for_line(line_warehouse: Optional[str] = None) -> str:
    """Where *this* line's material is consumed from, hence already at the line.

    The bill's own warehouse wins. Only a line that names none falls back to the
    configured default — guessing `BH-PC` for a line consumed at `BH-PP` nets out
    the wrong godown in both directions.
    """
    named = (line_warehouse or "").strip().upper()
    return named or production_consumption_warehouse()


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
    consumption_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Whether one BOM line goes to the warehouse, and for how much.

    `OTHER` — a component SAP groups as neither raw nor packaging — is treated
    like packing material rather than like RM. It is the catch-all bucket, so
    the conservative reading is that it still passes through the store; dropping
    it from the request because it failed to classify would silently stop
    someone being asked for material they have to hand over.
    """
    split = split_pick(required, at_production_consumption)
    pc_code = consumption_warehouse_for_line(consumption_code)
    is_raw = (material_type or "").upper() == MATERIAL_RAW

    if is_raw and pc_code == register_warehouse():
        # The bill consumes straight out of the tank. What is "already at the
        # line" is the register's own warehouse, and netting the approval's own
        # evidence off the requirement would cancel every oil request.
        return {
            "required": True,
            "qty": split["required_qty"],
            "reason": (
                f"{pc_code} is the Raw Material register's own warehouse — the "
                f"whole quantity is released against the register."
            ),
            **split,
        }

    # Where the balance has to come from, in the words of the document that
    # settles it: oil is released against the keeper's register, everything else
    # is fetched out of a godown.
    fetched_from = "the Raw Material register" if is_raw else "another godown"

    if split["from_other_warehouses"] <= ZERO:
        covered = split["from_production_consumption"]
        return {
            "required": False,
            "qty": ZERO,
            "reason": (
                f"All {covered:,.3f} is already at {pc_code}, so nothing has to "
                f"come from {fetched_from}."
                if covered > ZERO
                else "Nothing to fetch."
            ),
            **split,
        }

    reason = f"To be fetched from a godown other than {pc_code}."
    if is_raw:
        reason = "To be released against the Raw Material register."
    if split["from_production_consumption"] > ZERO:
        reason = (
            f"{split['from_production_consumption']:,.3f} is already at {pc_code}; "
            f"the remaining {split['from_other_warehouses']:,.3f} must come from "
            f"{fetched_from}."
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
