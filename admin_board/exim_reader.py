"""
admin_board/exim_reader.py

The tank farm's own system, read for capacity and level.

WHY THIS EXISTS
---------------
SAP records no tank capacity anywhere. The oil tile has always been able to
show a rating (``capacity_tons``/``used_pct`` are already on its payload) and
has always had to say "The tanks carry no rated capacity in any system",
because nothing in FactoryFlow or SAP holds one. EXIM does: it runs the tank
farm and holds every vessel's rated capacity and current level.

WHAT THIS IS NOT
----------------
It is not a sync, a model, or a second copy of the tank register. One SELECT,
issued when the tile is built, against a connection alias that only exists if
the deployment configured it. Nothing here writes, and nothing caches: a wall
board showing a stale tank level is worse than one saying it could not read the
farm.

⚠️ ``current_capacity`` IS THE STOCK, NOT A CAPACITY.
The single worst trap in this table. ``tank_data`` carries ``tank_capacity``
(what the vessel holds when full) and ``current_capacity`` (what is in it right
now). Reading the second as a capacity gives a farm that is always exactly
100% full, which looks plausible on a wall and is completely wrong. The column
is aliased to ``stock`` the moment it leaves the query so the name cannot
spread.

THE VESSELS ARE NOT ALL TANKS, AND THE HEADLINE COUNTS ONLY TANKS.
``tank_type`` is ``TANK`` (28 of them, 12,96,000 L) or ``TOTES`` (4, 55,500 L).
Totes are IBC containers that happen to hold the same oil; the tile answers
"how full is the tank farm", so ``capacity_tons``/``stock_tons`` cover
``HEADLINE_TYPES`` only and the totes are reported separately in ``by_type``
and ``excluded``.

They are excluded rather than dropped: 4 mostly-empty totes against 28 tanks
drag the percentage down by 2.4 points (73.1% including them, 75.5% without),
which is a real distortion of a question about tanks — but 9.6 T of oil is
still oil, and a board that silently forgot it would be lying by omission.
Every vessel is still in ``tanks``, carrying its own ``type``.

``is_active`` IS LOAD-BEARING. An inactive vessel is one taken out of service;
counting its rating inflates the denominator and makes the farm look emptier
than it is.

FAILURE IS A REPORTED STATE, NOT AN EXCEPTION
---------------------------------------------
Every path returns a ``TankReading`` — including "not configured" and "the
server did not answer". The tile renders the reason; it never renders a
confident zero for a farm it could not reach. Same posture as
``AdminBoardReader`` and ``_section``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import DatabaseError, connections
from django.db.utils import ConnectionDoesNotExist

from .constants import LITRES_PER_TON

logger = logging.getLogger(__name__)

#: The alias declared in ``config.settings``. Absent unless EXIM_DB_NAME is set.
ALIAS = "exim"

#: Which vessel types the headline capacity and level cover. Fixed tanks only —
#: the tile is the tank farm's fill, and four IBC totes are not that farm.
#: Everything outside this set is still read, still listed per vessel, and
#: totalled separately into ``TankReading.excluded``.
HEADLINE_TYPES = ("TANK",)

#: Verified against the live EXIM database on 2026-09-15: 32 active vessels,
#: 13,51,500 L rated, 9,88,200 L held.
#:
#: ``tank_item`` is LEFT joined for the item's display name and category —
#: ``item_code_id`` is a code (``RM00SB``), and four active vessels carry no
#: item at all because they are empty. An inner join would silently drop them
#: and shrink the farm's rated capacity by 91,500 L.
TANK_SQL = """
    SELECT d.tank_code,
           d.tank_type,
           d.item_code_id,
           i.tank_item_name,
           i.category,
           d.tank_capacity,
           COALESCE(d.current_capacity, 0) AS stock
    FROM public.tank_data d
    LEFT JOIN public.tank_item i ON i.tank_item_code = d.item_code_id
    WHERE d.is_active
    ORDER BY d.tank_code
"""


@dataclass
class TankReading:
    """What the farm said, or why it said nothing.

    ``tanks`` is empty on every unhappy path; ``reason`` is non-empty on exactly
    those paths. Test ``reason`` rather than ``tanks``: a farm that genuinely
    holds no vessels is a different thing from one that could not be read, and
    the tile words them differently.
    """

    tanks: List[Dict[str, Any]] = field(default_factory=list)
    capacity_tons: Optional[float] = None
    stock_tons: Optional[float] = None
    by_type: Dict[str, Dict[str, float]] = field(default_factory=dict)
    #: Vessels outside ``HEADLINE_TYPES`` — counted, listed, but not in the
    #: headline figures above. Empty when every vessel is a headline type.
    excluded: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.reason is None and bool(self.tanks)


def _configured() -> bool:
    return ALIAS in settings.DATABASES


def _to_tons(value: Optional[Any]) -> float:
    """A source figure in tonnes, whatever unit the column is kept in.

    EXIM stores litres — 50,000 in a tank that holds 50 T. Getting this
    backwards is a 1,000x error in either direction, so the unit is read from a
    setting rather than inferred from how big the numbers look.
    """
    if value is None:
        return 0.0
    unit = getattr(settings, "EXIM_TANK_UNIT", "LITRES").upper()
    if unit == "TONNES":
        return round(float(value), 2)
    return round(float(value) / LITRES_PER_TON, 2)


def read_tanks() -> TankReading:
    """Every active vessel with its rated capacity and current level, in tonnes.

    Returns a reading whose ``reason`` is set on any unhappy path: the alias is
    not configured, or the server did not answer. Never raises.
    """
    if not _configured():
        # The reason travels to a factory wall: it is printed in the Admin
        # board's action centre, and was on the oil tile. So it says what is
        # wrong in the reader's terms and stops there. The four environment
        # variables that fix it go to the LOG, where the one person who can set
        # them is looking -- on a TV they are noise to everybody walking past
        # and actionable by none of them.
        logger.warning(
            "admin_board: EXIM tank database not configured - set EXIM_DB_NAME, "
            "EXIM_DB_HOST, EXIM_DB_USER and EXIM_DB_PASSWORD to read the tank farm."
        )
        return TankReading(reason="The tank farm register is not connected to this server.")

    try:
        with connections[ALIAS].cursor() as cursor:
            cursor.execute(TANK_SQL)
            rows = cursor.fetchall()
    except ConnectionDoesNotExist:
        return TankReading(reason="The EXIM database alias is not configured.")
    except DatabaseError as exc:
        # Named, not swallowed: "could not read the farm" with the server's own
        # complaint is actionable; a blank tile is not.
        # The server's own complaint is logged, not displayed: a raw driver
        # error on a wall board tells a passer-by nothing and can name hosts and
        # credentials. "Could not be read" is the part they can act on -- tell
        # somebody -- and the log carries the rest.
        logger.warning("admin_board: EXIM tank read failed: %s", exc)
        return TankReading(reason="The tank farm register could not be read.")

    tanks: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, float]] = {}
    capacity_total = 0.0
    stock_total = 0.0

    excluded_capacity = 0.0
    excluded_stock = 0.0
    excluded_vessels = 0

    for code, vessel_type, item_code, item_name, category, capacity, stock in rows:
        capacity_tons = _to_tons(capacity)
        stock_tons = _to_tons(stock)
        kind = vessel_type or "TANK"

        if kind in HEADLINE_TYPES:
            capacity_total += capacity_tons
            stock_total += stock_tons
        else:
            excluded_capacity += capacity_tons
            excluded_stock += stock_tons
            excluded_vessels += 1

        bucket = by_type.setdefault(kind, {"vessels": 0, "capacity_tons": 0.0, "stock_tons": 0.0})
        bucket["vessels"] += 1
        bucket["capacity_tons"] = round(bucket["capacity_tons"] + capacity_tons, 2)
        bucket["stock_tons"] = round(bucket["stock_tons"] + stock_tons, 2)

        tanks.append(
            {
                "code": code,
                "type": kind,
                "item_code": item_code,
                # An empty vessel carries no item, which is a fact worth showing
                # rather than blanking: "empty" is why it has no oil named.
                "item": item_name or ("empty" if not stock_tons else item_code),
                "category": category,
                "capacity_tons": capacity_tons,
                "stock_tons": stock_tons,
                # None, never 0, on a vessel with no rating — 0% reads as empty.
                "used_pct": (
                    round(stock_tons / capacity_tons * 100, 1) if capacity_tons else None
                ),
            }
        )

    if not tanks:
        return TankReading(reason="The EXIM tank table holds no active vessels.")

    return TankReading(
        tanks=sorted(tanks, key=lambda row: row["stock_tons"], reverse=True),
        capacity_tons=round(capacity_total, 2),
        stock_tons=round(stock_total, 2),
        by_type=by_type,
        excluded=(
            {
                "vessels": excluded_vessels,
                "capacity_tons": round(excluded_capacity, 2),
                "stock_tons": round(excluded_stock, 2),
                "types": sorted(set(by_type) - set(HEADLINE_TYPES)),
            }
            if excluded_vessels
            else {}
        ),
    )
