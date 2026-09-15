"""
admin_board/tonnage.py

How this board turns stock into tonnes — the SAME way the Logistics board does.

WHY THIS MODULE EXISTS
----------------------
The first cut of this board weighed stock as ``litres / 1000``, borrowing the
Plant board's rule. That produced 444.5 T for BH-BT while the Logistics board,
two screens away in the same room, said 439 T for what a reader reasonably takes
to be the same number. It was not the same number, for TWO independent reasons,
and both had to be fixed:

1. **Basis.** ``litres / 1000`` is a density of 1.0. Edible oil is about 0.91,
   and the figure ignores packaging entirely. The Logistics board instead uses
   the real recorded weight — ``OITM.U_Gross_Weight``, the GROSS kg of one sales
   case, packaging included — which is what ties to a weighbridge.
2. **Scope.** The Logistics board counts **item group 102 (finished goods)
   only**. BH-BT also holds 230 T of group 105 and some group 106, which the
   first cut was silently folding into a tile labelled "FG storage".

Of those two, the scope error was the larger and the more misleading: it put
non-finished stock into a finished-goods total.

THE RULES, ALL THREE LOAD-BEARING
---------------------------------
* **Only piece units.** On a row stocked in KG or LTR the on-hand figure is
  already a mass or a volume, and dividing it by a pack factor means nothing.
  Those rows are counted and disclosed, never weighed.
* **Gross case weight over pieces-per-case.** ``U_Gross_Weight`` is per CASE
  while ``on_hand`` is a piece count, so the case count is on-hand over
  ``SalFactor2`` and the weight follows. Multiplying a case weight by a piece
  count — which SAP's own procedure does — overstates by the pack factor.
* **An unweighable row is disclosed, not zeroed.** A row with no recorded weight
  returns None and increments ``unweighed_items``. Adding "unknown" into a total
  as though it were "nothing" is how a half-weighed warehouse renders as a
  confident, wrong figure that looks exactly like a correct one.

Mirrors `logistics-control/utils/tonnage.ts` and `stock_dashboard.services`.
If those change, this changes with them — three implementations of one rule is
three tonnages.
"""

from typing import Any, Dict, Iterable, Optional

#: Inventory units that are a mass or a volume rather than a count of things.
#:
#: A deny-list, matching ``stock_dashboard.services._MASS_OR_VOLUME_UOMS``,
#: because the countable units are open-ended — SAP holds ``PCS`` for almost
#: everything and ``DRM`` for the one drum SKU, and a drum is still a thing you
#: count.
MASS_OR_VOLUME_UOMS = frozenset(
    {
        "KG", "KGS", "KGM", "GM", "GMS", "GRM", "MT", "TON", "TONNE",
        "L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3",
    }
)

#: Finished goods. The only group a finished-goods tile may count.
FINISHED_ITEM_GROUP = 102


def is_piece_uom(uom: Optional[str]) -> bool:
    """True where an on-hand quantity in ``uom`` is a count of pieces.

    Unknown and blank answer False. SAP leaves ``InvntryUom`` empty often
    enough that guessing "piece" would fold unweighable rows into a total
    silently; answering False pushes them into the disclosure instead, where
    somebody can see them.
    """
    trimmed = (uom or "").strip()
    return bool(trimmed) and trimmed.upper() not in MASS_OR_VOLUME_UOMS


def row_kilograms(row: Dict[str, Any]) -> Optional[float]:
    """Kilograms held by one occupancy row, or None where it cannot be weighed.

    Returns None — never 0 — so a caller cannot add "unknown" into a total as
    though it were "nothing".
    """
    if not is_piece_uom(row.get("uom")):
        return None

    per_case = row.get("gross_weight_per_case")
    if per_case is None or per_case <= 0:
        return None

    # A missing or zero pack factor would divide the warehouse by nothing. A
    # factor of exactly 1 is NOT an error: it means the SKU is sold by the
    # piece, so one piece is one case and the weight applies directly.
    pieces_per_case = row.get("pieces_per_box")
    if pieces_per_case is None or pieces_per_case <= 0:
        return None

    return (float(row.get("on_hand") or 0) * float(per_case)) / float(pieces_per_case)


def roll_up(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Total a warehouse in tonnes, carrying its own incompleteness with it.

    Negative on-hand rows are included as-is rather than clamped: SAP does carry
    negatives, and hiding them would make this board disagree with the stock
    screen for a reason nobody could see.
    """
    kg = 0.0
    weighed = 0
    unweighed = 0
    non_piece = 0
    total = 0

    for row in rows:
        total += 1
        if not is_piece_uom(row.get("uom")):
            non_piece += 1
            continue

        row_kg = row_kilograms(row)
        if row_kg is None:
            unweighed += 1
            continue

        kg += row_kg
        weighed += 1

    return {
        "tonnes": round(kg / 1000, 2),
        "weighed_items": weighed,
        "unweighed_items": unweighed,
        "non_piece_items": non_piece,
        "item_count": total,
        # What share of the rows the tonnage actually speaks for. A board
        # showing tonnes must be able to show this beside it.
        "coverage": round(weighed / total, 3) if total else None,
    }


#: Units that ARE a volume, as opposed to a mass.
#:
#: A subset of the deny-list above, needed because "not a piece" is not the same
#: question as "is a volume". The tank farm holds a few kilograms of rosemary
#: and walnut alongside the oil, and adding a kilogram to a litre total is the
#: same class of error as multiplying a case weight by a piece count.
VOLUME_UOMS = frozenset({"L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3"})


def litres(rows: Iterable[Dict[str, Any]]) -> float:
    """Total litres across rows, for stock that IS a volume.

    The loose-oil tanks are the case this exists for: they are stocked in LTR,
    which is not a piece unit, so ``roll_up`` correctly refuses to weigh them
    and would report zero tonnes for a full tank farm. There the on-hand figure
    is already the volume and the board's ``1,000 L = 1 T`` rule applies
    directly — no pack factor and no case weight involved.

    Three row shapes, three treatments:

    * **A volume unit** — the on-hand figure is already litres. Taken as-is.
    * **A piece unit** — converted through ``litres_per_piece``, which is what
      tells a 200-litre drum from a 15-litre tin. A piece row with no volume
      recorded contributes nothing rather than being counted as one litre each.
    * **A mass unit** — SKIPPED. Kilograms are not litres, and the few
      kilograms of flavouring stored beside the oil would otherwise be added
      into the tank total as though they were.
    """
    total = 0.0
    for row in rows:
        on_hand = float(row.get("on_hand") or 0)
        uom = (row.get("uom") or "").strip().upper()

        if is_piece_uom(uom):
            per_piece = row.get("litres_per_piece")
            total += on_hand * float(per_piece) if per_piece else 0.0
        elif uom in VOLUME_UOMS:
            total += on_hand
        # else: a mass, deliberately not counted.
    return total
