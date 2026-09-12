"""Pre-flight checks for a dismantle, run before SAP is called.

A dismantle is three documents deep and the app can withdraw none of them, so
every rule that can be checked here is checked here rather than discovered
halfway through a posting run. Each was read out of
``SBO_SP_TRANSACTIONNOTIFICATION`` in ``JIVO_OIL_HANADB`` (and confirmed present
in the Mart and Beverages copies where noted), or from the live documents the
SAP users posted by hand. The error numbers are kept in the messages so a refusal
pasted to the SAP team can be traced back to its rule.

THE ONE THAT DICTATES EVERYTHING
--------------------------------
``20206 Cannot add Goods Issue: no Goods Receipt posted for Disassembly Order N``
-- present in all three companies. The Receipt from Production must be in SAP
before the Goods Issue is attempted. It is the reason the posting order in
``services.py`` is not an implementation detail, and the reason a half-posted
dismantle is finished rather than restarted.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional


class DismantleGuardError(ValueError):
    """A dismantle SAP would refuse, refused earlier and in words."""


def check_posting_date(posting_date) -> None:
    """A goods issue may not be dated in the future (error 600006).

    The rule is spelled ``DAYS_BETWEEN(CURRENT_DATE, "DocDate") > 0`` and titled
    "Back Date Entry not Allowed", but what it actually blocks is a document
    dated AHEAD of today -- the title is a misnomer in SAP, not here. All three
    companies carry it.
    """
    if posting_date is None:
        return
    import datetime

    if posting_date > datetime.date.today():
        raise DismantleGuardError(
            f"SAP will not accept a goods issue dated {posting_date:%d %b %Y}, "
            f"which is in the future (error 600006). Post it with today's date."
        )


def check_parent(item_code: str, parent: Optional[dict], components: list) -> None:
    """The parent must exist, be stock, and have something to explode into."""
    if not (item_code or "").strip():
        raise DismantleGuardError("A dismantle needs the item being taken apart.")
    if parent is None:
        raise DismantleGuardError(f"{item_code} is not an item in SAP.")
    if not parent.get("is_inventory_item"):
        raise DismantleGuardError(
            f"{item_code} is not an inventory item, so there is no stock of it to "
            f"take apart."
        )
    if not components:
        raise DismantleGuardError(
            f"{item_code} has no production BOM in SAP, so SAP cannot say what it "
            f"is made of. A disassembly order can only be raised for an item with "
            f"one — ask the SAP team to add the recipe."
        )


def check_quantity(quantity, available: Optional[Decimal] = None, *, batch: str = "") -> None:
    """A positive quantity, and not more than the warehouse actually holds."""
    qty = Decimal(str(quantity or 0))
    if qty <= 0:
        raise DismantleGuardError(
            "The quantity to dismantle must be more than zero, and is counted in "
            "pieces rather than boxes."
        )
    if available is not None and qty > Decimal(str(available)):
        where = f"batch {batch}" if batch else "this warehouse"
        raise DismantleGuardError(
            f"Only {available} is in {where}, so {qty} cannot be dismantled."
        )


def check_batch(item_code: str, is_batch_managed: bool, batch_number: str) -> None:
    """A batch-managed parent has to name the batch being consumed."""
    if is_batch_managed and not (batch_number or "").strip():
        raise DismantleGuardError(
            f"{item_code} is batch-managed in SAP, so the goods issue has to name "
            f"the batch being taken apart. Pick the batch the stock came in on."
        )


def check_variety(item_code: str, variety_code: str) -> None:
    """Every goods issue line needs a Variety (error 60003).

    Unconditional in all three companies -- it is not waived for a document based
    on a production order -- and it is the last thing that would be discovered if
    it were left to SAP, because by then the receipt is already posted and the
    dismantle is half done.
    """
    if not (variety_code or "").strip():
        raise DismantleGuardError(
            f"{item_code} has no Variety mapped in SAP, and a goods issue line "
            f"cannot be posted without one (error 60003). Its item master needs a "
            f"Sub Group that matches a Dimension 1 profit centre."
        )


def check_components(components: Iterable[dict]) -> list:
    """The recovered lines, checked for what SAP demands of a receipt.

    Each component is a dict with ``item_code``, ``quantity`` and ``recovered``.
    """
    recovered = [c for c in components if c.get("recovered", True)]
    if not recovered:
        raise DismantleGuardError(
            "Nothing is marked as recovered, so this dismantle would receive "
            "nothing back. Tick at least one component."
        )

    seen: set[str] = set()
    for component in recovered:
        item = (component.get("item_code") or "").strip()
        quantity = Decimal(str(component.get("quantity") or 0))

        if not item:
            raise DismantleGuardError("A component line has no item code.")
        if item in seen:
            raise DismantleGuardError(
                f"{item} appears on more than one component line. Combine them "
                f"into a single line before posting."
            )
        seen.add(item)

        if quantity <= 0:
            raise DismantleGuardError(
                f"{item} is marked as recovered but its quantity is {quantity}. "
                f"Either give it a quantity or un-tick it."
            )
    return recovered


def check_component_batches(components: Iterable[dict], existing: set) -> None:
    """A received batch number must not already exist (error 590001).

    ``existing`` is the set of ``(item_code, batch_number)`` pairs SAP already
    knows, read company-wide rather than per warehouse because that is how the
    rule is written: "Duplicate Batch not Allowed, Batch No Must be Unique".
    """
    for component in components:
        batch = (component.get("batch_number") or "").strip()
        item = (component.get("item_code") or "").strip()
        if not batch:
            if component.get("is_batch_managed"):
                raise DismantleGuardError(
                    f"{item} is batch-managed, so the material coming back needs a "
                    f"batch number of its own."
                )
            continue
        if (item, batch) in existing:
            raise DismantleGuardError(
                f"Batch {batch} of {item} already exists in SAP, and a receipt "
                f"from production cannot reuse one (error 590001). Rebuild the "
                f"components so a fresh batch is minted."
            )


def batch_number_for(entry_no: str, component_id: int) -> str:
    """A fresh batch number for a component coming back off a dismantle.

    Derived from the app's own entry number so it is unique company-wide, which
    is what error 590001 demands, and so a batch found in the warehouse months
    later can be traced to the dismantle that created it. The component's own id
    rather than its position, for the same reason the returns module uses the
    line id: positions repeat across records, ids do not.
    """
    return f"{entry_no}-{component_id}".upper()[:36]


def check_warehouse(warehouse_code: str) -> str:
    if not (warehouse_code or "").strip():
        raise DismantleGuardError("Select the warehouse the stock is in.")
    return warehouse_code.strip()


def bom_inflation_warning(
    item_code: str, pieces_per_box, bom_batch_size
) -> Optional[str]:
    """A warning when the recipe's batch size is not the item's box size.

    Components are exploded per piece as ``ITT1."Quantity" / OITT."Qauntity"``,
    so a recipe written per box against a batch size of 1 yields a box's worth of
    components for every single piece -- 24 Oil recipes are in that state. SAP's
    own disassembly screen divides by the same field, so posting through the app
    reproduces SAP exactly; this warns rather than blocks, because refusing would
    stop work over a master-data fault the operator cannot fix.

    Returns None when the two agree.
    """
    if not bom_batch_size or not pieces_per_box:
        return None
    if Decimal(str(bom_batch_size)) == Decimal(str(pieces_per_box)):
        return None
    return (
        f"{item_code}'s recipe in SAP is written for a batch of {bom_batch_size} "
        f"while a box holds {pieces_per_box}. Every component quantity below is "
        f"off by that ratio — SAP's own disassembly screen would be wrong in the "
        f"same way. Check the quantities before posting, and ask the SAP team to "
        f"correct the BOM's batch size."
    )
