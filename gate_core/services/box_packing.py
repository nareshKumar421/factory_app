"""Canonical box/loose split for an invoiced line — the rule SAP's own bill prints.

SAP's A/R invoice layout does not count boxes in the report: the HANA procedure
``CRYSTAL_AR_INVOICE_ITEMS`` (one per company schema) feeds it ready-made
``BoxInt``/``LooseQty`` columns::

    BoxInt   = CASE WHEN SalFactor3 > 1 THEN Quantity
                    WHEN SalFactor2 = 1 THEN 0
                    ELSE INT(Quantity / SalFactor2) END
    LooseQty = CASE WHEN SalFactor2 != 1 THEN Quantity - INT(Quantity / SalFactor2) * SalFactor2
                    ...
                    ELSE Quantity END

So ``SalFactor2 = 1`` means "this item is not transacted in boxes" — it ships loose,
per piece — which is why a 500-piece line of FG0000381 (EXTRA VIRGIN OLIVE OIL 10ML)
prints as ``0 Box  500.00 PCS`` while our own screens counted 500 boxes.

The one exception is CSD stock. A CSD SKU also carries ``SalFactor2 = 1``, but there the
1 means "one box IS the billed piece" (the carton is the sellable unit), so a 500-piece
CSD line really is 500 boxes and must stay box-counted for scanning. CSD items are
identified by a ``CSD`` token in the item name, which is how the master data marks them
(every CSD-named finished good in JIVO_OIL and JIVO_MART carries SalFactor2 = 1).

Kept free of Django imports so the SQL builder, the gate services and the barcode
adapter can all share one definition of the rule.
"""

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

# A CSD SKU names itself: "... 1 LTR 16 PCS ( CSD )", "MUSTARD OIL 100 MLS 20 PCS(CSD)".
# Word-bounded so an item that merely contains the letters (none today) can't match.
CSD_PATTERN = re.compile(r"\bCSD\b", re.IGNORECASE)

# SQL mirror of the same test, for the HANA readers that must compute the split in the
# query. ``{name}`` is the item-name column expression.
CSD_SQL_PREDICATE = (
    "UPPER({name}) LIKE '%CSD%'"
)


class LinePacking(NamedTuple):
    """How one invoiced line breaks down for counting and scanning.

    ``boxes``  -- full boxes to scan (0 when the item ships loose).
    ``loose``  -- pieces that are not in a countable box (the whole line for a loose
                  item; the remainder of an uneven division for a boxed one).
    ``pieces_per_box`` -- the divisor used, or None when the item is not boxed.
    """

    boxes: int
    loose: Decimal
    pieces_per_box: Decimal | None

    @property
    def is_loose(self) -> bool:
        """True when the line carries no countable boxes at all — count it in pieces."""
        return self.pieces_per_box is None


def is_csd_item(item_name: Any) -> bool:
    return bool(CSD_PATTERN.search(str(item_name or "")))


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def pieces_per_box(sal_factor2: Any, item_name: Any = "") -> Decimal | None:
    """Pieces in one countable box, or None when the item is not transacted in boxes.

    ``SalFactor2 > 1``  -> that many pieces per box (SAP's divisor).
    ``SalFactor2 == 1`` -> CSD: one piece IS one box. Otherwise: loose, returns None.
    missing/zero        -> treated as 1 (unconfigured item, same branch as above), so an
                           item SAP never set up ships loose rather than inventing boxes.
    """
    factor = to_decimal(sal_factor2)
    if factor > 1:
        return factor
    if is_csd_item(item_name):
        return Decimal("1")
    return None


def split_line(quantity: Any, sal_factor2: Any, item_name: Any = "") -> LinePacking:
    """Split an invoiced quantity into full boxes + loose pieces, SAP's way."""
    qty = to_decimal(quantity)
    if qty <= 0:
        return LinePacking(0, Decimal("0"), pieces_per_box(sal_factor2, item_name))

    per_box = pieces_per_box(sal_factor2, item_name)
    if per_box is None:
        return LinePacking(0, qty, None)

    if per_box == 1:
        # One piece per box (CSD): a fractional piece still needs its own box.
        return LinePacking(int(math.ceil(qty)), Decimal("0"), per_box)

    boxes = int(qty // per_box)
    return LinePacking(boxes, qty - (Decimal(boxes) * per_box), per_box)


def box_invoice_units(box_pieces: Any, sal_factor2: Any, item_name: Any = "") -> Decimal:
    """How much of a bill's invoiced quantity ONE physical box covers.

    The invoice's unit is not always a piece. For CSD stock the bill counts BOXES — a
    line reading 4 means four cartons, even though each carton physically holds 20
    bottles and the box label declares ``qty = 20``. Comparing that 20 against the
    invoiced 4 is comparing cartons with bottles: it rejected the scan outright
    ("would exceed the invoiced quantity ... only 4 PCS remain") and, where it did get
    through, marked a 4-carton line complete after one carton.

    So a CSD box counts as exactly 1 against the invoice regardless of what it holds,
    and every other item counts its pieces (a 20-piece box covers 20 of a piece-counted
    line). ``Box.qty`` stays untouched and factual — it is what the box physically
    declares, and the screens still show it per box.
    """
    if pieces_per_box(sal_factor2, item_name) == 1:
        return Decimal("1")
    return to_decimal(box_pieces)
