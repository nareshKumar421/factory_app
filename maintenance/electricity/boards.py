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


def main_meter_names():
    """The supply meters (KWH, KVAH, ...): never on a board, never in a total.

    A main measures the electricity coming in, which its sub-meters then
    measure again in parts. What a main reads beyond its sub-meters is the
    supply nobody's meter saw, owed by no company, so the boards leave the
    mains out altogether and show the meters the factory actually draws on.
    """
    from maintenance.models import ElectricityMeter

    return set(ElectricityMeter.objects.filter(is_main=True).values_list("name", flat=True))


def parties_of(codes: Iterable[str]):
    return {company_party(code) for code in codes}


def for_parties(result, parties, mains=None) -> Dict:
    """The span's units and cost for ``parties``, in total, by day and by meter.

    ``parties`` is a set of party keys (``company:JIVO_OIL``…), or ``None`` for
    everybody — every company, every consumer and what nobody was set to pay
    for. Main meters are left out (:func:`main_meter_names`), so the figure is
    the meters the factory draws on, each unit once. Everything is added up
    from the per-day, per-meter split, so the total, the days and the meters
    always agree.
    """
    mains = main_meter_names() if mains is None else mains

    def wanted(party):
        return parties is None or party in parties

    total = _blank()
    by_day = defaultdict(_blank)
    by_meter = defaultdict(_blank)
    by_day_meter = defaultdict(lambda: defaultdict(_blank))

    for day, parts in result["by_day_meter"].items():
        for party, meters in parts.items():
            if not wanted(party):
                continue
            for name, part in meters.items():
                if name in mains:
                    continue
                for bucket in (total, by_day[day], by_meter[name], by_day_meter[day][name]):
                    bucket["units"] += part["units"]
                    bucket["cost"] += part["cost"]

    return {
        "units": total["units"],
        "cost": total["cost"],
        "by_day": dict(by_day),
        "by_meter": dict(by_meter),
        "by_day_meter": {day: dict(meters) for day, meters in by_day_meter.items()},
    }


def company(result, code: str, mains=None) -> Dict:
    """One company's share of the span (see :func:`for_parties`)."""
    return for_parties(result, {company_party(code)}, mains)


def meter_tree(date_from, date_to, code: str = "", mains=None):
    """Each meter as reading − sub-meters = own, in tree order, mains left out.

    The Electricity dashboard's sum, one row a meter: what the meter read, what
    its sub-meters read of that, and the remainder, which is the meter's own
    load. With ``code``, also the company's part of that own load. A meter
    under a main is shown at the top, as the main is not shown at all; a
    register that re-reads another meter (KVAH re-reading KWH) is left out too.
    """
    mains = main_meter_names() if mains is None else mains
    report = service.report(date_from, date_to)
    party = company_party(code) if code else None
    hidden = {m["id"] for m in report["meters"]
              if m["name"] in mains or m.get("register_of") or m.get("unplaced")}
    by_id = {m["id"]: m for m in report["meters"]}
    rows = []
    for meter in report["meters"]:
        if meter["id"] in hidden:
            continue
        # One level up for every hidden meter above it, anywhere up the chain;
        # its parent on screen is the nearest one that is shown.
        depth, parent = meter["depth"], None
        above = meter.get("parent_id")
        while above is not None:
            if above in hidden:
                depth -= 1
            elif parent is None:
                parent = above
            above = by_id[above].get("parent_id")
        share = next((s for s in meter.get("split", []) if s["party"] == party), None)
        rows.append({
            "id": meter["id"],
            "name": meter["name"],
            "depth": max(depth, 0),
            "parent_id": parent,
            "units": meter.get("units"),
            "sub_metered_units": meter.get("sub_metered_units"),
            "own_units": meter.get("own_units"),
            "own_cost": meter.get("own_cost"),
            "company_units": share["units"] if share else (None if party else meter.get("own_units")),
            "company_cost": share["cost"] if share else (None if party else meter.get("own_cost")),
            "company_share_pct": share["share_pct"] if share else None,
        })
    return rows


def meter_share(result, party_units: Decimal, meter_name: str) -> Optional[Decimal]:
    """The fraction of a meter's own units a company's figure is; None if all of it."""
    own = (result.get("own_by_meter") or {}).get(meter_name, {}).get("units") or ZERO
    if not own or party_units >= own:
        return None
    return party_units / own
