"""Pre-flight checks for a warehouse transfer, run before anything is posted.

SAP will reject a bad transfer, but it rejects with a bare number from
`SBO_SP_TRANSACTIONNOTIFICATION` — `5900002`, `67081`, `670851` — which means
nothing to a warehouse operator. Every rule SAP enforces that we can evaluate
ourselves is checked here first, so the operator gets a sentence instead.

The error numbers in the messages are deliberate: when someone pastes a refusal
into a message to the SAP team, the number is what lets them find the rule.

Rules encoded here were read out of the notification procedure in
`JIVO_OIL_HANADB`. Mart and Beverages carry their own variants — notably
Beverages requires a request for `BH-LO` *or* `BH-PM` into `BH-PC`, and its rule
sits on the draft table so it does not fire on a direct post at all. Anything
this module misses still surfaces as a SAP rejection, just less legibly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

# Warehouses SAP treats as goods-in-transit. A cross-branch transfer must land
# in one of these, and a transfer leaving one must stay inside its own branch.
INTRANSIT_WAREHOUSES = ("BH-INT", "DL-INT", "PB-INT")

# Any transfer line leaving this warehouse must be based on a transfer request
# (SAP error 67081, Oil).
REQUEST_REQUIRED_SOURCES = ("BH-LO",)

# Transfers into this warehouse are refused unless the source is in-transit or
# the posting SAP user is 52 — and the app posts as B1i (user 2), so for us the
# exemption never applies (SAP error 670851, Oil).
RESTRICTED_DESTINATIONS = ("BH-GR",)

# Production wastage into BH-GR must carry this business partner, and the card is
# invalid on any other route (SAP errors 67082 / 67083).
WASTAGE_CARD_CODE = "CUSTA000940"
WASTAGE_ROUTE = ("BH-PC", "BH-GR")

# SAP refuses any transfer dated before this (error 67001081).
EARLIEST_POSTING_DATE = "2025-08-18"

# Units that only exist in whole numbers. SAP will happily post 0.993 PCS — it
# accepted exactly that on transfer 826676784 — so nothing downstream catches a
# fractional pouch. Weighed and measured units (LTR, KGS, GMS, MTR, MTS) are
# legitimately fractional and are deliberately absent.
DISCRETE_UOMS = frozenset({"PCS", "NOS", "SET", "DRM"})


class TransferGuardError(ValueError):
    """A transfer that SAP would refuse, refused earlier and in words."""


def is_whole_unit(uom: str) -> bool:
    return (uom or "").strip().upper() in DISCRETE_UOMS


def check_whole_units(item_code: str, quantity, uom: str) -> None:
    """Refuse a fractional quantity of something that only comes in whole units."""
    if not is_whole_unit(uom):
        return
    value = Decimal(str(quantity))
    if value != value.to_integral_value():
        raise TransferGuardError(
            f"{item_code} is measured in {uom.strip().upper()}, so it moves in whole "
            f"units — {value} is not one. SAP would accept it silently, which is why "
            f"it is refused here."
        )


@dataclass
class RouteDecision:
    """What kind of move this is, and how many SAP documents it takes."""
    is_cross_branch: bool
    from_branch_id: Optional[int]
    to_branch_id: Optional[int]
    intransit_warehouse: str = ""
    notes: list[str] = field(default_factory=list)


def resolve_route(
    *,
    from_warehouse: str,
    to_warehouse: str,
    branch_of: dict[str, Optional[int]],
) -> RouteDecision:
    """Classify the route and pick the in-transit warehouse when crossing branches.

    `branch_of` maps warehouse code -> OWHS.BPLid. Read it once per request and
    store the result, so posting never has to re-derive it.
    """
    if not from_warehouse or not to_warehouse:
        raise TransferGuardError("A transfer needs both a source and a destination warehouse.")

    if from_warehouse == to_warehouse:
        raise TransferGuardError(
            f"Source and destination are both {from_warehouse}. SAP refuses a "
            f"transfer that does not move anything (error 5900002)."
        )

    from_branch = branch_of.get(from_warehouse)
    to_branch = branch_of.get(to_warehouse)

    if from_branch is None or to_branch is None:
        missing = from_warehouse if from_branch is None else to_warehouse
        raise TransferGuardError(
            f"{missing} has no branch set in SAP, so we cannot tell whether this "
            f"move crosses branches. Ask the SAP team to set BPLid on the warehouse."
        )

    if from_branch == to_branch:
        return RouteDecision(False, from_branch, to_branch)

    intransit = _intransit_for_branch(to_branch, branch_of)
    if not intransit:
        raise TransferGuardError(
            f"{to_warehouse} is in another branch, so the stock has to move through "
            f"that branch's in-transit warehouse — and none of "
            f"{', '.join(INTRANSIT_WAREHOUSES)} belongs to it. SAP would refuse "
            f"this with error 5900002."
        )

    return RouteDecision(
        True, from_branch, to_branch, intransit,
        notes=[
            f"Crosses branch {from_branch} → {to_branch}, so it ships via "
            f"{intransit} and the receiving side posts the second leg."
        ],
    )


def _intransit_for_branch(
    branch_id: int, branch_of: dict[str, Optional[int]]
) -> str:
    for code in INTRANSIT_WAREHOUSES:
        if branch_of.get(code) == branch_id:
            return code
    return ""


def check_route(
    *,
    from_warehouse: str,
    to_warehouse: str,
    route: RouteDecision,
    is_second_leg: bool = False,
) -> None:
    """Guards that depend only on the warehouse pair."""
    if to_warehouse in RESTRICTED_DESTINATIONS:
        if from_warehouse not in INTRANSIT_WAREHOUSES:
            raise TransferGuardError(
                f"SAP does not allow transfers into {to_warehouse} unless they come "
                f"from an in-transit warehouse (error 670851). Route returns and "
                f"written-note stock through the goods-return flow instead."
            )

    if is_second_leg:
        if from_warehouse not in INTRANSIT_WAREHOUSES:
            raise TransferGuardError(
                f"A second leg has to start from an in-transit warehouse, not "
                f"{from_warehouse}."
            )
        if route.from_branch_id != route.to_branch_id:
            raise TransferGuardError(
                f"The second leg leaves {from_warehouse} and must stay inside its "
                f"own branch (error 6700001), but {to_warehouse} is in another one."
            )
    elif route.is_cross_branch and to_warehouse not in INTRANSIT_WAREHOUSES:
        raise TransferGuardError(
            f"A cross-branch transfer can only ship into an in-transit warehouse "
            f"(error 5900002); {to_warehouse} is not one."
        )


def check_lines(
    *,
    lines: Iterable[dict],
    batch_flags: dict[str, bool],
    has_transfer_request: bool,
) -> None:
    """Guards that depend on the lines: sources, batches and quantities.

    Each line is a dict with `item_code`, `quantity`, `from_warehouse`,
    `to_warehouse` and optionally `batches`.
    """
    lines = list(lines)
    if not lines:
        raise TransferGuardError("A transfer needs at least one line.")

    for line in lines:
        item = line.get("item_code") or "(no item)"
        qty = Decimal(str(line.get("quantity") or 0))

        if qty <= 0:
            raise TransferGuardError(f"{item} has a quantity of {qty}; it must be positive.")

        # Only checked when the caller knows the unit; a line without one is
        # left alone rather than guessed at.
        check_whole_units(item, qty, line.get("uom", ""))

        source = line.get("from_warehouse") or ""
        if source in REQUEST_REQUIRED_SOURCES and not has_transfer_request:
            raise TransferGuardError(
                f"SAP will not move {item} out of {source} unless the transfer is "
                f"based on a transfer request (error 67081). Raise the request first."
            )

        if batch_flags.get(item):
            batches = line.get("batches") or []
            if not batches:
                raise TransferGuardError(
                    f"{item} is batch-managed in SAP, so the transfer must say which "
                    f"batches move. None were allocated."
                )
            allocated = sum(Decimal(str(b.get("Quantity") or 0)) for b in batches)
            if allocated != qty:
                raise TransferGuardError(
                    f"{item}: the batch split adds up to {allocated} but the line "
                    f"moves {qty}. SAP requires them to match exactly."
                )


def check_posting_date(posting_date) -> None:
    """SAP refuses a transfer dated before 2025-08-18 (error 67001081)."""
    if posting_date and posting_date.isoformat() < EARLIEST_POSTING_DATE:
        raise TransferGuardError(
            f"SAP will not accept a transfer dated {posting_date:%d %b %Y} — nothing "
            f"before {EARLIEST_POSTING_DATE} is allowed (error 67001081)."
        )


def card_code_for_route(from_warehouse: str, to_warehouse: str) -> str:
    """The business partner SAP demands on a route, if any.

    Only production wastage needs one, and using it anywhere else is itself a
    rejection — so this returns it per route rather than letting a caller set it.
    """
    if (from_warehouse, to_warehouse) == WASTAGE_ROUTE:
        return WASTAGE_CARD_CODE
    return ""
