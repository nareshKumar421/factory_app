"""What an item is, for the machines: its pack, its container and its oil.

Litres per piece always come from SAP (``OITM.SalPackUn``, gated by
``U_IsLitre``) — the one source the whole app takes volume from. The name is
read only for what SAP does not hold in a usable field: whether the piece is a
combo, a pouch or a tin, and the floor's word for its oil.
"""

import re
from dataclasses import dataclass
from typing import Optional

from .. import constants as C

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(ML|MLS|LTR|LTRS|LT|L|KG|KGS|GM|GMS)\b")


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    lp: float                 # litres in one piece (a combo of two 1 L bottles is 2)
    type: str                 # bottle / combo / pouch / tin / glass bottle / drum
    pack: str                 # the bottle size a machine sees: "1 L", "200 ml", "pouch"
    bottles: int              # bottles in one piece: 2 for a combo
    oil: str                  # the floor's word: mustard, pomace, cold press ...
    cold_press: bool

    @property
    def size_key(self) -> str:
        """The key the speed table is written in: "1 L", "15 L", "pouch"."""
        return self.pack


def size_label(litres: float) -> str:
    if litres < 1:
        return f"{round(litres * 1000)} ml"
    if abs(litres - round(litres)) < 0.01:
        return f"{int(round(litres))} L"
    return f"{litres:.2f}".rstrip("0").rstrip(".") + " L"


def sheet_size_litres(name: str) -> Optional[float]:
    """The size a planning-sheet NAME states ("GROUNDNUT 200ML" -> 0.2), if any.

    Used only as a guard: a sheet line whose name says 200 ml under a code that
    is a 2 L item in Oil's book is somebody else's code, and planning it as
    written would fill the wrong bottle.
    """
    m = _SIZE.search((name or "").upper())
    if not m:
        return None
    qty, unit = float(m.group(1)), m.group(2)
    if unit.startswith("K") or unit.startswith("G"):
        return None          # a weight: 15 KGS is 16.48 L, not a size to compare
    if unit.startswith("M"):
        return qty / 1000
    return qty


def oil_of(name: str) -> str:
    low = " ".join((name or "").lower().split())
    for word, family in C.OIL_WORDS:
        if word in low:
            return family
    if "cold press" in low:
        return "cold press"
    return "other"


def describe(code: str, name: str, lp: Optional[float]) -> Optional[Item]:
    """The machine's view of one item, or None when SAP holds no volume for it."""
    if not lp or lp <= 0:
        return None
    upper = (name or "").upper()
    combo = "COMBO" in upper or bool(re.search(r"\d\s*(?:LTR|L|ML|MLS)\s*\+\s*\d", upper))
    bottles = upper.count("+") + 1 if combo else 1
    bottles = max(bottles, 2) if combo else 1
    pouch = "POUCH" in upper
    glass = "GLASS" in upper
    drum = lp >= 100
    tin = bool(re.search(r"\bTIN\b", upper)) or (10 <= lp < 100 and not pouch)

    if pouch:
        kind, pack = "pouch", "pouch"
    elif combo:
        kind, pack = "combo", size_label(lp / bottles)
    elif drum:
        kind, pack = "drum", size_label(lp)
    elif tin:
        kind, pack = "tin", size_label(lp)
    else:
        kind, pack = ("glass bottle" if glass else "bottle"), size_label(lp)

    return Item(
        code=code,
        name=name or code,
        lp=float(lp),
        type=kind,
        pack=pack,
        bottles=bottles,
        oil=oil_of(name),
        cold_press="COLD PRESS" in upper,
    )


# ---------------------------------------------------------------------------
# The two filters, the speed and the change
# ---------------------------------------------------------------------------


def takes_pack(machine: str, item: Item) -> bool:
    return item.type in C.PACK_TYPES[machine] and item.size_key in C.SPEEDS[machine]


def runs_oil(machine: str, item: Item) -> bool:
    oils = C.MACHINE_OILS[machine]
    if oils is None:
        return True
    return item.oil in oils or ("cold press" in oils and item.cold_press)


def speed(machine: str, item: Item) -> Optional[float]:
    """Pieces an hour: a combo is two bottles, so it runs at half the bottle speed."""
    per_hour = C.SPEEDS[machine].get(item.size_key)
    if not per_hour:
        return None
    return per_hour / item.bottles


def machines_for(item: Item):
    """Every machine that passes both filters and has a speed on the board."""
    return [
        m for m in C.MACHINES
        if takes_pack(m, item) and runs_oil(m, item) and speed(m, item)
    ]


def no_machine_reason(item: Item) -> str:
    packs = [m for m in C.MACHINES if takes_pack(m, item)]
    if not packs:
        return f"no machine makes this pack ({item.pack}{' ' + item.type if item.type not in ('bottle', 'combo') else ''})"
    timed = [m for m in packs if runs_oil(m, item)]
    if not timed:
        return f"no machine on the board runs {item.oil} in {item.pack}"
    return f"{' / '.join(timed)} takes {item.pack} but its speed for it is not on the board"


def same_sku(a_code: str, b_code: str) -> bool:
    if a_code == b_code:
        return True
    for first, second, _name, _when in C.CARTON_TWINS:
        if {a_code, b_code} == {first, second}:
            return True
    return False


def change(machine: str, last: Optional[Item], item: Item):
    """(hours, label) to change ``machine`` from ``last`` to ``item``.

    Nothing logged on the machine means a change is counted: the plan cannot
    assume it is already on the SKU.
    """
    if last is not None and same_sku(last.code, item.code):
        return 0.0, "already on this SKU"
    if machine == "JP":
        return C.CHANGE_H["JP"], "JP change 5–6 h (6 taken)"
    if machine == "Clear Pack":
        if last is not None and last.oil == "mustard" and item.oil != "mustard":
            return C.CLEAR_PACK_FROM_MUSTARD_H, "from mustard 2 h"
        if last is not None and last.oil == "rice bran" and item.cold_press and not last.cold_press:
            return C.CLEAR_PACK_RICE_BRAN_TO_COLD_PRESS_H, "rice bran → cold press 1.25 h (assumed)"
        return C.CHANGE_H["Clear Pack"], "normal change 1 h"
    if machine in ("10 Head", "6 Head"):
        if last is not None and last.oil in C.HEADS_SLOW_FROM:
            return C.HEADS_SLOW_H, f"from {last.oil}: more than 1 h (1.5 taken)"
        if last is not None and last.cold_press and item.oil == "sunflower" and not item.cold_press:
            return C.CHANGE_H[machine], "cold press → sunflower 0.5 h"
        return C.CHANGE_H[machine], "normal change 0.5 h"
    return C.CHANGE_H[machine], "45 min per change"


def packaging_word(name: str) -> str:
    low = (name or "").lower()
    for word, reason in C.PACKAGING_WORDS:
        if word in low:
            return reason
    return "no packaging"
