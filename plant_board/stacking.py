"""
plant_board/stacking.py

How many pieces of a packaging item fit on one pallet.

WHY THIS IS A DATA FILE AND NOT A TABLE
---------------------------------------
It is a measurement of the material, not a record of the business: a carton
holds what it holds, and the figure changes only when somebody re-measures the
range. Nobody edits it on a screen. Keeping it as a checked-in file means the
board's floor-occupancy figure is reproducible from the repository, moves under
review like code, and needs no migration when the factory re-measures -- the
Excel is re-imported and the diff is readable.

Authored in ``excel/Oil Stacking 11.09.2026.xlsx`` by the factory and imported
with ``manage.py import_stacking``. The Excel stays the source of truth.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
The sheet also carries a quantity column. It is ignored: it is a snapshot of
one morning and is already wrong by the time it is read -- several rows differ
from live stock by orders of magnitude. Only the pieces-per-pallet factor is
taken, and it is applied to live SAP stock.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent / "data" / "stacking.json"


@lru_cache(maxsize=1)
def _loaded() -> Dict:
    """The file, read once per process.

    A missing or unreadable file is logged and returns nothing rather than
    raising: the board then reports the floor and the stock separately and says
    it cannot measure occupancy, which is the same honest state as never having
    been given the sheet.
    """
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a wall board degrades, never 500s
        logger.warning("plant_board: stacking data unavailable: %s", exc)
        return {}


def pieces_per_pallet() -> Dict[str, float]:
    """Item code -> pieces on one pallet."""
    return {
        code: float(value)
        for code, value in (_loaded().get("items") or {}).items()
        if float(value) > 0
    }


def measured_on() -> str:
    """The day the factory measured the range, for the board to disclose."""
    return str(_loaded().get("measured_on") or "")
