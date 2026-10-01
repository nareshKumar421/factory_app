"""Electricity++ cut the way the boards read it: one company, a span, by day and meter.

Every board that shows electricity — the Electricity dashboard, Admin Control,
the factory expense wall and the company expense matrix — reads its figure
from here, so they all agree with each other and with Daily Electricity++.
Before, each kept its own sum of the register: one split a shared meter in
half, one added the mains to the sub-meters that measure the same supply, one
mapped meters to companies by a list in code.

Electricity++ (:mod:`.service`) is the only place that knows who a meter's
units belong to: the meter tree, the fixed shares, the run-hour splits, and a
reading chain that notices a dial that jumped. These helpers only select from
its answer; they never weigh anything themselves.
"""
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Iterable, Optional

from . import service
from .sources import company_party

ZERO = Decimal("0")


def _blank():
    return {"units": ZERO, "cost": ZERO}


def breakdown(date_from, date_to):
    """Electricity++'s allocation for a span (``service.company_breakdown``)."""
    return service.company_breakdown(date_from, date_to)


def warnings(result, limit: Optional[int] = None):
    """What Electricity++ says is wrong with the readings behind the figure."""
    messages = [p.get("message") for p in result.get("problems", []) if p.get("message")]
    unplaced = result.get("unplaced") or []
    if unplaced:
        # Electricity++ counts such a meter as a supply of its own that nobody
        # pays for, so its units are in the campus total but on no company.
        messages.append(
            "Not placed in the meter tree, so charged to no company yet: "
            + ", ".join(sorted(unplaced)) + "."
        )
    return messages[:limit] if limit else messages


def parties_of(codes: Iterable[str]):
    return {company_party(code) for code in codes}


def for_parties(result, parties) -> Dict:
    """The span's units and cost for ``parties``, in total, by day and by meter.

    ``parties`` is a set of party keys (``company:JIVO_OIL``…), or ``None`` for
    everybody — every company, every consumer and what nobody was set to pay
    for — which is the whole metered supply, each unit once.
    """
    def wanted(party):
        return parties is None or party in parties

    total = _blank()
    by_day = defaultdict(_blank)
    by_meter = defaultdict(_blank)
    by_day_meter = defaultdict(lambda: defaultdict(_blank))

    for party, part in result["by_party"].items():
        if wanted(party):
            total["units"] += part["units"]
            total["cost"] += part["cost"]
    for day, parts in result["by_day"].items():
        for party, part in parts.items():
            if wanted(party):
                by_day[day]["units"] += part["units"]
                by_day[day]["cost"] += part["cost"]
    for party, meters in result["by_meter"].items():
        if wanted(party):
            for name, part in meters.items():
                by_meter[name]["units"] += part["units"]
                by_meter[name]["cost"] += part["cost"]
    for day, parts in result["by_day_meter"].items():
        for party, meters in parts.items():
            if wanted(party):
                for name, part in meters.items():
                    by_day_meter[day][name]["units"] += part["units"]
                    by_day_meter[day][name]["cost"] += part["cost"]

    return {
        "units": total["units"],
        "cost": total["cost"],
        "by_day": dict(by_day),
        "by_meter": dict(by_meter),
        "by_day_meter": {day: dict(meters) for day, meters in by_day_meter.items()},
    }


def company(result, code: str) -> Dict:
    """One company's share of the span (see :func:`for_parties`)."""
    return for_parties(result, {company_party(code)})


def meter_share(result, party_units: Decimal, meter_name: str) -> Optional[Decimal]:
    """The fraction of a meter's own units a company's figure is; None if all of it."""
    own = (result.get("own_by_meter") or {}).get(meter_name, {}).get("units") or ZERO
    if not own or party_units >= own:
        return None
    return party_units / own
