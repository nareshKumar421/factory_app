"""
plant_board/workforce.py

The plant's departments: the ones the factory is built around, and the ones an
operator adds.

TWO SOURCES, ONE LIST, AND THE DIFFERENCE MATTERS
-------------------------------------------------
``WORKFORCE_DEPARTMENTS`` in ``constants`` is the factory as it was described
when this board was built: six departments, each with the band it reports under
and whether its people are on the payroll or hired in. That mapping is a
statement about the plant rather than a figure somebody types, which is why it
lives in code and why nothing on the settings page can re-band it. An operator
who mistypes a head count has mistyped a head count; an operator who could drag
Oil Production into the Purchase band would have quietly changed what the board
means.

A plant grows a department that nobody anticipated, though, and waiting for a
deploy to count its people is not a reasonable answer. So a department can also
be ADDED, and an added one carries its own label, band and kind in the database
beside its figures. The two kinds are never confused: a built-in cannot be
deleted or re-banded, and an added one says so, so the guarantee above still
holds for every department the board was designed around.

WHY THE MERGE LIVES HERE AND NOT IN EITHER CALLER
-------------------------------------------------
The board reads this list to build its workforce strips and the settings page
reads it to draw its table. If they resolved the catalogue separately, a
department added on one would eventually not exist on the other -- and the
failure would look like a missing row rather than like two functions disagreeing.
One resolver, both callers.
"""

import re
from typing import Any, Dict, List

from stock_dashboard.models import PlantBoardWorkforce

from .constants import WORKFORCE_BANDS, WORKFORCE_DEPARTMENTS

#: Staff on the payroll, or labour hired in. The two halves of a band's strip.
WORKFORCE_KINDS = ["employee", "labour"]

BUILT_IN = {entry["key"]: entry for entry in WORKFORCE_DEPARTMENTS}


def slugify_key(label: str) -> str:
    """A storage key from a typed label.

    Lower case, underscores, nothing else -- the same shape as the built-in
    keys, so a reader of the table cannot tell which rows were typed and which
    were deployed, and neither can a query.
    """
    key = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return key[:60]


def unique_key(label: str, taken) -> str:
    """``slugify_key`` with a numeric tail if that key is already in use."""
    base = slugify_key(label) or "department"
    key = base
    n = 2
    while key in taken:
        key = f"{base[:56]}_{n}"
        n += 1
    return key


def departments(company_code: str) -> List[Dict[str, Any]]:
    """Every department for one company: the built-in six, then any added.

    Built-ins come first and in their declared order, because both the strip and
    the settings table are read down the same rows every day and a list that
    reorders itself is a list nobody trusts. Added departments follow in the
    order they were created.

    A saved row whose key is neither a built-in nor carries a band of its own is
    SKIPPED rather than guessed at: that is a built-in retired from the
    catalogue since the figure was typed, and it has nowhere on the board to go.
    The row is left in the table so nothing is destroyed by a deploy.
    """
    saved = {
        row.department: row
        for row in PlantBoardWorkforce.objects.filter(
            company_code=company_code
        ).order_by("id")
    }

    out: List[Dict[str, Any]] = []
    for entry in WORKFORCE_DEPARTMENTS:
        out.append(_row(entry["key"], entry, saved.get(entry["key"]), custom=False))

    for key, row in saved.items():
        if key in BUILT_IN:
            continue
        if not row.band or row.band not in WORKFORCE_BANDS:
            continue
        entry = {
            "key": key,
            "label": row.label or key,
            "band": row.band,
            "kind": row.kind if row.kind in WORKFORCE_KINDS else "employee",
        }
        out.append(_row(key, entry, row, custom=True))
    return out


def _row(key: str, entry: Dict[str, Any], saved, custom: bool) -> Dict[str, Any]:
    return {
        "key": key,
        "label": entry["label"],
        "band": entry["band"],
        "kind": entry["kind"],
        # An added department can be removed; a built-in is the factory's own
        # shape and the settings page must not offer to delete it.
        "is_custom": custom,
        "employees": None if saved is None else saved.employees,
        "salary_monthly": (
            None
            if saved is None or saved.salary_monthly is None
            else float(saved.salary_monthly)
        ),
        "updated_at": saved.updated_at.isoformat() if saved is not None else None,
    }
