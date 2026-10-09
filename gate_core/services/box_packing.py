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

# Packaging-material item-code prefix. PM lines (cartons, caps, labels) are not
# barcode-tracked -- no box label is ever printed for them -- so they are never scanned
# and must not be counted as goods the scanner owes: a PM line is never short, and a
# PM-only bill needs no scan at all. Same visible-prefix rule the BST scan gate
# (``warehouse.services.bst_service``) and the weighment rule
# (``gate_core.services.weighment_rules``) already use.
PM_ITEM_CODE_PREFIX = "PM"


def is_pm_item_code(item_code: Any) -> bool:
    """True when an item code identifies packaging material (``PM`` prefix)."""
    return str(item_code or "").strip().upper().startswith(PM_ITEM_CODE_PREFIX)


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


def pieces_per_box(
    sal_factor2: Any, item_name: Any = "", sal_factor3: Any = None
) -> Decimal | None:
    """Pieces in one countable box, or None when the item is not transacted in boxes.

    ``SalFactor3 > 1``  -> the billed unit IS a box (see below). One per box.
    ``SalFactor2 > 1``  -> that many pieces per box (SAP's divisor).
    ``SalFactor2 == 1`` -> CSD: one piece IS one box. Otherwise: loose, returns None.
    missing/zero        -> treated as 1 (unconfigured item, same branch as above), so an
                           item SAP never set up ships loose rather than inventing boxes.

    ``sal_factor3`` is opt-in, and only the bill summary passes it. It is how SAP
    itself marks a line billed in whole cartons -- its ``BoxInt`` tests
    ``SalFactor3 > 1`` before it looks at ``SalFactor2`` at all -- and it is
    strictly better than the CSD name token: every CSD SKU sold carries it, but
    three of the thirteen (FG0000013 REFINED OIL 1000 MLS, FG0000016 2 LTR,
    FG0000020 5 LTR -- the three the ``CSD_AR_INVOICE`` print special-cases by
    item code) have no CSD in their name and so fell through to "loose". A
    one-carton line of those printed 0 boxes and one loose piece, sending the
    floor to fetch a single bottle out of a 20-bottle carton.

    The scanning callers stay on the name token deliberately: they count physical
    boxes against a bill, and changing what a box means there would move the
    dispatch and BST quantity locks. This is the picking sheet's rule.
    """
    if to_decimal(sal_factor3) > 1:
        return Decimal("1")

    factor = to_decimal(sal_factor2)
    if factor > 1:
        return factor
    if is_csd_item(item_name):
        return Decimal("1")
    return None


def split_line(
    quantity: Any, sal_factor2: Any, item_name: Any = "", sal_factor3: Any = None
) -> LinePacking:
    """Split an invoiced quantity into full boxes + loose pieces, SAP's way.

    ``sal_factor3`` is passed only by the bill summary; see ``pieces_per_box``.
    """
    qty = to_decimal(quantity)
    if qty <= 0:
        return LinePacking(
            0, Decimal("0"), pieces_per_box(sal_factor2, item_name, sal_factor3)
        )

    return split_with_pieces_per_box(
        qty, pieces_per_box(sal_factor2, item_name, sal_factor3)
    )


def split_with_pieces_per_box(quantity: Any, per_box: Any) -> LinePacking:
    """The same split, from a divisor already worked out.

    For callers holding a stored ``pieces_per_box`` rather than SAP's factors —
    the bill summary snapshots the divisor onto its lines when the sheet is
    raised (master data edits ``SalFactor2``, and a sheet must keep footing up
    the same afterwards), so restating a quantity on one has to re-split against
    that snapshot rather than ask SAP again. ``None`` or zero means the item is
    not transacted in boxes.
    """
    qty = to_decimal(quantity)
    per_box = to_decimal(per_box) if per_box not in (None, "") else None
    if per_box is not None and per_box <= 0:
        per_box = None
    if qty <= 0:
        return LinePacking(0, Decimal("0"), per_box)
    if per_box is None:
        return LinePacking(0, qty, None)
    if per_box == 1:
        # One billed unit per box (CSD, or SalFactor3 > 1): a fractional unit
        # still needs its own box.
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


def is_full_box(box_pieces: Any, sal_factor2: Any, item_name: Any = "") -> bool:
    """True when one physical box carries a whole pack of the item.

    A box packed short -- or dismantled, keeping its barcode while pieces were pulled
    out as loose stock -- declares fewer pieces than ``SalFactor2``. Such a box covers
    the bill's printed LOOSE remainder, not one of its BOXES.

    Counting it as a full box is what let 115 full boxes plus one 4-piece box read as
    "116 / 116 boxes" against a line invoicing 1,860 PCS of a 16-PCS item (116 boxes +
    4 loose): the box count looked complete, 16 pieces were still on the floor, and the
    box-count cap then refused the very box that would have finished the bill.

    Items SAP does not transact in boxes (no pack size) and CSD stock (where one box IS
    the billed piece) have no part-box notion, so every box of theirs counts as full.
    """
    per_box = pieces_per_box(sal_factor2, item_name)
    if per_box is None or per_box <= 1:
        return True
    return to_decimal(box_pieces) >= per_box



# Units a line can be counted in one by one. Anything else (LTR, KGS, GMS, MTR) is bulk
# -- loose ghee by the litre, oil by the tanker -- and has no box count at all.
PIECE_UNITS = frozenset({"PCS", "PC", "NOS", "NO", "UNT", "UNIT", "SET", "EA"})

# A unit this big travels on its own: a 5 LTR bottle, a 15 LTR or 15 KGS tin, a 200 LTR
# drum, a 5 + 1 LTR set. Smaller goods SAP gives no box size to -- a 10 ML sample
# bottle, a 100 GMS spice pack, a gift box -- are packed into cartons by the floor, so
# one of them is never one box.
SINGLE_UNIT_MIN_LITRES = Decimal("5")


def load_box_count(lines) -> int:
    """Boxes ONE bill puts on the truck -- the figure the dispatch plan export carries.

    Built the way the docking scan builds its target (``sales_dispatch_gatepass.
    split_lines_target``): lines are grouped per item and split once, since two lines of
    13 and 3 pieces of a 16-PCS item are one box on the floor, not two; and packaging
    material is left out, because it carries no box label and nobody scans it.

    Where the scan target stops at full boxes, this counts every box the goods actually
    travel in, so no bill of goods reads 0 just because they are not whole cartons:

    * A part box is a box. The remainder of an uneven split is repacked into one more
      carton -- "116 boxes + 4 loose is loaded as at least 117 boxes, the last holding
      just the 4 loose pieces" (``sales_dispatch_box_match.remaining_expected_boxes``).
      So a Mart bill of 3 pieces of a 12-PCS item is 1 box, not 0.
    * ``SalFactor3 > 1`` marks a line billed in whole cartons (``pieces_per_box``), so
      FG0000013, invoiced as 1, is the one 20-bottle carton it is.
    * An item SAP does not box (SalFactor2 = 1, not CSD), billed in pieces, is one box
      per unit when the unit holds :data:`SINGLE_UNIT_MIN_LITRES` or more -- each 15 LTR
      tin is its own scan on the dock. A smaller one has no pack size anywhere, so all
      that is known is that the item fills at least one box: it counts 1. That is a
      floor, not a count -- maintaining SalFactor2 on the item makes it exact.
    * Billed in LTR or KGS it is bulk and adds nothing.

    ``lines`` are dicts carrying ``item_code``, ``item_name``, ``quantity``, ``uom``,
    ``litres`` (the line's total), ``sal_factor2`` and ``sal_factor3`` -- the picking
    sheet's lines (``HanaDispatchBillReader.list_pickable_lines``).
    """
    grouped: dict = {}
    for line in lines:
        code = str(line.get("item_code") or "").strip().upper()
        # No item code is a service line (freight, IT support): nothing physical ships.
        if not code or is_pm_item_code(code):
            continue
        grouped.setdefault(code, []).append(line)

    total = 0
    for group in grouped.values():
        quantity = sum((to_decimal(line.get("quantity")) for line in group), Decimal("0"))
        if quantity <= 0:
            continue
        head = group[0]
        per_box = pieces_per_box(
            head.get("sal_factor2"), head.get("item_name"), head.get("sal_factor3")
        )
        if per_box is not None:
            total += math.ceil(quantity / per_box)
            continue
        if str(head.get("uom") or "").strip().upper() not in PIECE_UNITS:
            continue
        litres = sum((to_decimal(line.get("litres")) for line in group), Decimal("0"))
        if litres / quantity >= SINGLE_UNIT_MIN_LITRES:
            total += math.ceil(quantity)
        else:
            total += 1
    return total
