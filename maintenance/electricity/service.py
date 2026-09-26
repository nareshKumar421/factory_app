"""Who used how much electricity, and what it cost — the question every screen asks.

:func:`allocate` runs the engine over a span. :func:`report` turns the result
into the payload for the Split tab on Daily Electricity++. :func:`party_totals`
and :func:`company_breakdown` are the shapes the cost boards will read. Nothing
calls them yet, because the boards keep their old logic until the user moves
them onto the tree. All of them answer from the same allocation, so once the
boards move no two screens can split a meter two different ways.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from django.utils import timezone

from . import engine, sources
from .engine import ZERO, IssueKind

TWO_PLACES = Decimal("0.01")
ONE_PLACE = Decimal("0.1")


def money(value) -> str:
    return str(Decimal(value or 0).quantize(TWO_PLACES, ROUND_HALF_UP))


def _plain(value: Decimal) -> str:
    """50.000 -> "50", 33.330 -> "33.33"; never "5E+1"."""
    text = format(Decimal(value).normalize(), "f")
    return text


def _pct(part, whole) -> Optional[str]:
    if not whole:
        return None
    return str((Decimal(part) * 100 / Decimal(whole)).quantize(ONE_PLACE, ROUND_HALF_UP))


def allocate(
    date_from: date, date_to: date, *, now=None
) -> Tuple[engine.Allocation, sources.Snapshot, engine.Allocator]:
    """The allocation for a span, with what it was built from."""
    allocator, snapshot = sources.load(date_from, date_to, now=now)
    return allocator.allocate(date_from, date_to), snapshot, allocator


# ---------------------------------------------------------------------------
# The tree, as it stood at the end of a span
# ---------------------------------------------------------------------------


def placement(allocator: engine.Allocator, meter_id: int, on: date) -> Optional[engine.Setup]:
    """The latest in-service version of a meter's setup on or before ``on``."""
    found = None
    for setup in allocator.setups.get(meter_id, ()):
        if setup.effective_from > on:
            break
        if setup.in_service:
            found = setup
    return found


def tree_order(
    allocator: engine.Allocator,
    meter_ids: Iterable[int],
    on: date,
) -> List[Tuple[int, int, Optional[int]]]:
    """``(meter_id, depth, parent_id)`` depth-first, mains first, siblings by name.

    A meter whose parent is not in the list is shown as a main — the tree is
    drawn from what exists, never a hole where a parent should be.
    """
    wanted = set(meter_ids)
    names = {mid: allocator.meters[mid].name.lower() for mid in wanted if mid in allocator.meters}
    parent_of: Dict[int, Optional[int]] = {}
    for meter_id in wanted:
        setup = placement(allocator, meter_id, on)
        parent_id = setup.parent_id if setup else None
        parent_of[meter_id] = parent_id if parent_id in wanted else None

    children: Dict[Optional[int], List[int]] = defaultdict(list)
    for meter_id, parent_id in parent_of.items():
        children[parent_id].append(meter_id)
    for siblings in children.values():
        siblings.sort(key=lambda mid: names.get(mid, ""))

    ordered: List[Tuple[int, int, Optional[int]]] = []
    seen = set()

    def walk(meter_id: int, depth: int) -> None:
        if meter_id in seen:
            return
        seen.add(meter_id)
        ordered.append((meter_id, depth, parent_of.get(meter_id)))
        for child_id in children.get(meter_id, ()):
            walk(child_id, depth + 1)

    for root in children.get(None, ()):
        walk(root, 0)
    # Anything a cycle kept from being reached is still listed.
    for meter_id in sorted(wanted - seen, key=lambda mid: names.get(mid, "")):
        walk(meter_id, 0)
    return ordered


def meter_tree(on: date) -> Dict[int, dict]:
    """Every meter's place on ``on``, for the meter list and the day sheet.

    meter id -> ``{"order", "depth", "parent_id", "parent_name", "setup",
    "in_service", "unplaced", "register_of"}``. ``order`` runs depth-first with
    each meter's second registers straight after it; meters not in the tree on
    ``on`` (retired, not started yet) come last with ``in_service`` off.
    """
    setups, snapshot = sources.load_setups()
    allocator = engine.Allocator(
        [engine.Meter(m.id, m.name, m.register_of_id) for m in snapshot.meters.values()],
        setups,
        [],
    )
    setup_rows = {row.id: row for rows in snapshot.setups.values() for row in rows}
    in_service = {
        meter_id
        for meter_id in allocator.in_service_on(on)
        if meter_id in snapshot.meters
    }

    info: Dict[int, dict] = {}
    position = 0

    def name_of(meter_id):
        meter = snapshot.meters.get(meter_id)
        return meter.name if meter else None

    for meter_id, depth, parent_id in tree_order(allocator, in_service, on):
        setup = allocator.setup_on(meter_id, on)
        info[meter_id] = {
            "order": position,
            "depth": depth,
            "parent_id": parent_id,
            "parent_name": name_of(parent_id),
            "setup": setup_rows.get(setup.id) if setup and setup.id else None,
            "in_service": True,
            "unplaced": meter_id in snapshot.unplaced,
            "register_of": None,
        }
        position += 1
        for register in sorted(
            (m for m in snapshot.meters.values() if m.register_of_id == meter_id),
            key=lambda m: m.name.lower(),
        ):
            # One level under its meter, never beside it: shown at the meter's
            # own depth it reads as the parent of the meter's sub-meters.
            info[register.id] = {
                "order": position,
                "depth": depth + 1,
                "parent_id": meter_id,
                "parent_name": name_of(meter_id),
                "setup": None,
                "in_service": True,
                "unplaced": False,
                "register_of": meter_id,
            }
            position += 1

    for meter in sorted(snapshot.meters.values(), key=lambda m: m.name.lower()):
        if meter.id in info:
            continue
        rows = snapshot.setups.get(meter.id, [])
        latest = None
        for row in rows:
            if row.effective_from <= on:
                latest = row
        info[meter.id] = {
            "order": position,
            "depth": 0,
            "parent_id": None,
            "parent_name": None,
            "setup": latest or (rows[0] if rows else None),
            "in_service": False,
            "unplaced": not rows and meter.register_of_id is None,
            "register_of": meter.register_of_id,
        }
        position += 1
    return info


# ---------------------------------------------------------------------------
# Describing a rule
# ---------------------------------------------------------------------------


def describe_setup(setup_row, snapshot: Optional[sources.Snapshot] = None) -> dict:
    """A setup version as the screens show it."""
    from ..models import ElectricityAllocationBasis

    shares = []
    for share in setup_row.shares.all():
        party_row = share.company if share.company_id else share.consumer
        party = (
            sources.company_party(party_row.code)
            if share.company_id
            else sources.consumer_party(party_row.code)
        )
        shares.append(
            {
                "party": party,
                "party_name": party_row.name,
                "company": share.company.code if share.company_id else None,
                "consumer": share.consumer.code if share.consumer_id else None,
                "percent": _plain(share.percent),
            }
        )
    drivers = []
    for driver in setup_row.drivers.all():
        company = driver.party_company()
        kind = (
            "LINE"
            if driver.production_line_id
            else "BLOWING_MACHINE"
            if driver.blowing_machine_id
            else "METER"
        )
        source = driver.production_line or driver.blowing_machine or driver.meter
        drivers.append(
            {
                "kind": kind,
                "source": driver.source_key,
                "id": source.id,
                "name": source.name,
                "company": company.code if company else None,
                "company_name": company.name if company else None,
                "weight": _plain(driver.weight),
            }
        )
    return {
        "id": setup_row.id,
        "effective_from": setup_row.effective_from,
        "in_service": setup_row.in_service,
        "parent": setup_row.parent_id,
        "parent_name": setup_row.parent.name if setup_row.parent_id else None,
        "basis": setup_row.basis,
        "basis_label": ElectricityAllocationBasis(setup_row.basis).label,
        "shares": shares,
        "drivers": drivers,
        "summary": summarise_rule(setup_row.basis, shares, drivers),
        "note": setup_row.note,
    }


def summarise_rule(basis: str, shares: List[dict], drivers: List[dict]) -> str:
    """One line a person can check at a glance: "Oil 50% · Beverages 50%"."""
    def share_text(items):
        return " · ".join(f"{item['party_name']} {item['percent']}%" for item in items)

    if basis == engine.Basis.FIXED:
        return share_text(shares) or "No shares set"
    if basis in engine.Basis.PROPORTIONAL:
        what = "run hours of" if basis == engine.Basis.RUN_HOURS else "readings of"
        followed = ", ".join(
            f"{item['name']} ({item['company_name'] or '—'})" for item in drivers
        ) or "nothing yet"
        text = f"By {what} {followed}"
        if shares:
            text += f"; otherwise {share_text(shares)}"
        return text
    return "Not decided yet"


# ---------------------------------------------------------------------------
# Issues, grouped so a month of one problem reads as one line
# ---------------------------------------------------------------------------

SEVERITY = {
    IssueKind.BREAK: "error",
    IssueKind.OVER_READ: "error",
    IssueKind.UNASSIGNED: "error",
    IssueKind.NO_BASIS: "error",
    IssueKind.READ_OUT_OF_SERVICE: "error",
    IssueKind.PARENT_OUT_OF_SERVICE: "error",
    IssueKind.NOT_READ: "warning",
    IssueKind.FALLBACK: "warning",
    IssueKind.DRIVER_NOT_READ: "warning",
    IssueKind.SPREAD: "info",
    IssueKind.DAY_NOT_ENTERED: "info",
}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def group_issues(
    issues: Iterable[engine.Issue], snapshot: sources.Snapshot, allocator: engine.Allocator, on: date
) -> List[dict]:
    groups: Dict[Tuple, dict] = {}
    for issue in issues:
        extra = None
        if issue.kind == IssueKind.DRIVER_NOT_READ:
            extra = issue.detail.get("driver_meter_id")
        key = (issue.kind, issue.meter_id, extra)
        group = groups.setdefault(
            key,
            {
                "kind": issue.kind,
                "severity": SEVERITY.get(issue.kind, "warning"),
                "meter_id": issue.meter_id,
                "meter_name": _meter_name(snapshot, issue.meter_id),
                "days": [],
                "units": ZERO,
                "detail": dict(issue.detail),
                "_extra": extra,
            },
        )
        group["days"].append(issue.day)
        if issue.units is not None:
            group["units"] += issue.units
            # A break can leave a gap (units in no reading) or an overlap (the
            # same units in two); netting one against the other would hide both.
            if issue.kind == IssueKind.BREAK:
                side = "gap" if issue.units > 0 else "overlap"
                group.setdefault(f"_{side}", ZERO)
                group[f"_{side}"] += abs(issue.units)
                if "previous_date" in issue.detail:
                    group.setdefault("_since", issue.detail["previous_date"])

    rendered = []
    for group in groups.values():
        group["days"] = sorted(set(group["days"]))
        group["count"] = len(group["days"])
        group["message"] = _issue_message(group, snapshot, allocator, on)
        group["units"] = money(group["units"])
        for private in ("_extra", "_gap", "_overlap", "_since"):
            group.pop(private, None)
        group["detail"] = {
            key: (str(value) if isinstance(value, (Decimal, date)) else value)
            for key, value in group["detail"].items()
            if not isinstance(value, tuple)
        }
        rendered.append(group)
    rendered.sort(
        key=lambda item: (
            SEVERITY_ORDER.get(item["severity"], 9),
            item["meter_name"] or "",
            item["kind"],
        )
    )
    return rendered


def _meter_name(snapshot: sources.Snapshot, meter_id: Optional[int]) -> Optional[str]:
    if meter_id is None:
        return None
    meter = snapshot.meters.get(meter_id)
    return meter.name if meter else f"Meter #{meter_id}"


def _issue_message(group: dict, snapshot, allocator, on: date) -> str:
    kind = group["kind"]
    name = group["meter_name"]
    days = _plural(group["count"], "day")
    units = money(group["units"])
    if kind == IssueKind.DAY_NOT_ENTERED:
        return f"Nothing was entered on {days}."
    if kind == IssueKind.NOT_READ:
        principal = group["detail"].get("register_of")
        if principal:
            return (
                f"{name} ({_meter_name(snapshot, principal)}'s second register) "
                f"was not read on {days}."
            )
        setup = placement(allocator, group["meter_id"], on)
        if setup and setup.parent_id:
            return (
                f"{name} was not read on {days}. Its load on those days is counted "
                f"in the rest of {_meter_name(snapshot, setup.parent_id)}."
            )
        return f"{name} was not read on {days}, so what it measured on those days is not split."
    if kind == IssueKind.SPREAD:
        count = group["count"]
        return (
            f"{name}: {_plural(count, 'reading')} came after skipped days and "
            f"{'was' if count == 1 else 'were'} spread evenly over the days "
            f"{'it' if count == 1 else 'they'} covered."
        )
    if kind == IssueKind.BREAK:
        parts = []
        if group.get("_gap"):
            where = ""
            if group["count"] == 1 and group.get("_since"):
                where = f" between {group['_since']:%d %b} and {group['days'][0]:%d %b}"
            parts.append(f"{money(group['_gap'])} units fell{where} into no reading at all")
        if group.get("_overlap"):
            parts.append(f"{money(group['_overlap'])} units were counted twice")
        what = " and ".join(parts) or "the chain of readings broke"
        return (
            f"{name}: {_plural(group['count'], 'reading')} did not start from the "
            f"previous closing, so {what}. Correct the reading, or mark it as a "
            f"meter reset."
        )
    if kind == IssueKind.OVER_READ:
        return (
            f"{name}'s sub-meters read {units} units more than it did, over {days}. "
            f"Its rest is taken as 0 on those days. A sub-meter may be under the "
            f"wrong meter, or a dial was misread."
        )
    if kind == IssueKind.UNASSIGNED:
        return f"{name}: nobody is set to pay for it. {units} units are unassigned."
    if kind == IssueKind.FALLBACK:
        return (
            f"{name}: nothing it follows ran on {days}, so its fixed split was used "
            f"for {units} units."
        )
    if kind == IssueKind.NO_BASIS:
        return (
            f"{name}: nothing it follows ran on {days}, and it has no fixed split to "
            f"fall back on. {units} units are unassigned."
        )
    if kind == IssueKind.DRIVER_NOT_READ:
        driver = _meter_name(snapshot, group["detail"].get("driver_meter_id"))
        return (
            f"{name} is split by {driver}'s reading, and {driver} was not read on "
            f"{days}. It counted as 0 in the split."
        )
    if kind == IssueKind.PARENT_OUT_OF_SERVICE:
        parent = _meter_name(snapshot, group["detail"].get("parent_id"))
        return (
            f"{name} is set under {parent}, which is not in service on {days}. "
            f"It was treated as a main meter on those days."
        )
    if kind == IssueKind.READ_OUT_OF_SERVICE:
        return (
            f"{name} was read on {days} when it is not in the meter tree. "
            f"{units} units were not counted."
        )
    return f"{name}: {kind} on {days}."


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def report(date_from: date, date_to: date, *, now=None) -> dict:
    """The Electricity Split page: totals, the tree, the days, the problems."""
    result, snapshot, allocator = allocate(date_from, date_to, now=now)
    date_from, date_to = result.date_from, result.date_to

    # --- per meter -------------------------------------------------------
    per_meter: Dict[int, dict] = defaultdict(
        lambda: {
            "units": ZERO,
            "cost": ZERO,
            "sub_metered": ZERO,
            "own_units": ZERO,
            "own_cost": ZERO,
            "over_read": ZERO,
            "split": defaultdict(lambda: {"units": ZERO, "cost": ZERO}),
            "drivers": defaultdict(lambda: ZERO),
            "days_in_service": 0,
            "days_read": 0,
            "spread_days": 0,
            "fallback_days": 0,
            "setups": set(),
        }
    )
    # meter -> party -> day -> units, for boards that chart a party's meters day
    # by day (the Electricity dashboard's trend).
    meter_daily: Dict[int, Dict[str, Dict[date, Decimal]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: ZERO))
    )
    entered_days = set(result.entered_days)
    for node in result.nodes:
        for party, units in node.split.items():
            meter_daily[node.meter_id][party][node.day] += units
        row = per_meter[node.meter_id]
        # A day nobody entered at all (today, a skipped Sunday) is the
        # register's gap, not this meter's; it is reported once, on its own.
        if node.day in entered_days:
            row["days_in_service"] += 1
        row["setups"].add(node.setup.id)
        if node.measured is None:
            continue
        row["days_read"] += 1
        row["units"] += node.measured
        row["cost"] += node.measured * node.rate
        row["sub_metered"] += node.sub_metered
        row["own_units"] += node.own or ZERO
        row["own_cost"] += node.cost
        row["over_read"] += node.over_read
        if node.spread_over > 1:
            row["spread_days"] += 1
        if node.fallback_used:
            row["fallback_days"] += 1
        for party, units in node.split.items():
            row["split"][party]["units"] += units
            row["split"][party]["cost"] += units * node.rate
        for party, amount in node.drivers.items():
            row["drivers"][party] += amount

    register_rows: Dict[int, dict] = defaultdict(
        lambda: {"units": ZERO, "principal_units": ZERO, "days_read": 0, "principal_id": None}
    )
    for day in result.registers:
        row = register_rows[day.meter_id]
        row["principal_id"] = day.principal_id
        if day.measured is not None and day.principal_measured is not None:
            row["days_read"] += 1
            row["units"] += day.measured
            row["principal_units"] += day.principal_measured

    in_tree = set(per_meter)
    for meter_id in allocator.setups:
        if placement(allocator, meter_id, date_to):
            in_tree.add(meter_id)
    in_tree = {mid for mid in in_tree if mid in snapshot.meters}

    setup_rows = {row.id: row for rows in snapshot.setups.values() for row in rows}
    ordered = tree_order(allocator, in_tree, date_to)
    children_of: Dict[int, List[int]] = defaultdict(list)
    for meter_id, _, parent_id in ordered:
        if parent_id is not None:
            children_of[parent_id].append(meter_id)
    meters_out = []
    for meter_id, depth, parent_id in ordered:
        meter = snapshot.meters[meter_id]
        row = per_meter.get(meter_id)
        setup = placement(allocator, meter_id, date_to)
        setup_row = setup_rows.get(setup.id) if setup and setup.id else None
        rule = describe_setup(setup_row, snapshot) if setup_row else None
        child_ids = children_of.get(meter_id, [])
        entry = {
            "id": meter_id,
            "name": meter.name,
            "location": meter.location,
            "depth": depth,
            "parent_id": parent_id,
            "children": child_ids,
            "is_register": False,
            "register_of": None,
            "unplaced": meter_id in snapshot.unplaced,
            "rule": rule,
            "rule_changed_in_span": bool(row and len(row["setups"]) > 1),
            "units": money(row["units"]) if row else money(0),
            "cost": money(row["cost"]) if row else money(0),
            "sub_metered_units": money(row["sub_metered"]) if row else money(0),
            "own_units": money(row["own_units"]) if row else money(0),
            "own_cost": money(row["own_cost"]) if row else money(0),
            "over_read_units": money(row["over_read"]) if row else money(0),
            "days_in_service": row["days_in_service"] if row else 0,
            "days_read": row["days_read"] if row else 0,
            "spread_days": row["spread_days"] if row else 0,
            "fallback_days": row["fallback_days"] if row else 0,
            "split": [
                {
                    "party": party,
                    "units": money(values["units"]),
                    "cost": money(values["cost"]),
                    "share_pct": _pct(values["units"], row["own_units"]),
                }
                for party, values in sorted(
                    (row["split"] if row else {}).items(),
                    key=lambda item: -item[1]["units"],
                )
            ],
            "drivers": [
                {"party": party, "amount": str(amount.quantize(ONE_PLACE, ROUND_HALF_UP))}
                for party, amount in sorted(
                    (row["drivers"] if row else {}).items(), key=lambda item: -item[1]
                )
            ],
        }
        meters_out.append(entry)
        for register in sorted(
            (m for m in snapshot.meters.values() if m.register_of_id == meter_id),
            key=lambda m: m.name.lower(),
        ):
            reg = register_rows.get(register.id)
            ratio = None
            if reg and reg["units"]:
                ratio = str((reg["principal_units"] / reg["units"]).quantize(Decimal("0.001")))
            meters_out.append(
                {
                    "id": register.id,
                    "name": register.name,
                    "location": register.location,
                    "depth": depth + 1,
                    "parent_id": meter_id,
                    "children": [],
                    "is_register": True,
                    "register_of": meter_id,
                    "register_of_name": meter.name,
                    "units": money(reg["units"]) if reg else money(0),
                    "principal_units": money(reg["principal_units"]) if reg else money(0),
                    "days_read": reg["days_read"] if reg else 0,
                    # KWH over KVAH is the power factor; for any other pair it
                    # is simply how the two registers compare.
                    "ratio": ratio,
                }
            )

    # --- totals ----------------------------------------------------------
    totals = result.party_totals()
    supply = result.supply()
    allocated_units = sum((values["units"] for values in totals.values()), ZERO)
    allocated_cost = sum((values["cost"] for values in totals.values()), ZERO)
    over_read = sum((node.over_read for node in result.nodes), ZERO)

    party_keys = [key for key in totals if key != engine.UNASSIGNED]
    party_keys.sort(key=lambda key: (-totals[key]["units"], snapshot.party(key).name))
    if engine.UNASSIGNED in totals:
        party_keys.append(engine.UNASSIGNED)

    # --- per day ---------------------------------------------------------
    daily: Dict[date, dict] = {
        day: {
            "supply_units": ZERO,
            "supply_read": False,
            "by_party": defaultdict(lambda: ZERO),
            "cost_by_party": defaultdict(lambda: ZERO),
        }
        for day in result.days
    }
    for node in result.nodes:
        day = daily[node.day]
        if node.parent_id is None and node.measured is not None:
            day["supply_units"] += node.measured
            day["supply_read"] = True
        for party, units in node.split.items():
            day["by_party"][party] += units
            day["cost_by_party"][party] += units * node.rate
    entered = set(result.entered_days)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "days": len(result.days),
        "entered_days": len(entered),
        "parties": [snapshot.party(key).as_dict() for key in party_keys],
        "totals": {
            "supply_units": money(supply["units"]),
            "supply_cost": money(supply["cost"]),
            "allocated_units": money(allocated_units),
            "allocated_cost": money(allocated_cost),
            "over_read_units": money(over_read),
            "by_party": [
                {
                    "party": key,
                    "units": money(totals[key]["units"]),
                    "cost": money(totals[key]["cost"]),
                    "share_pct": _pct(totals[key]["units"], allocated_units),
                }
                for key in party_keys
            ],
        },
        "meters": meters_out,
        "daily": [
            {
                "date": day,
                "entered": day in entered,
                "supply_units": money(values["supply_units"]) if values["supply_read"] else None,
                "by_party": {key: money(values["by_party"].get(key, ZERO)) for key in party_keys},
                "cost_by_party": {
                    key: money(values["cost_by_party"].get(key, ZERO)) for key in party_keys
                },
            }
            for day, values in sorted(daily.items())
        ],
        "meter_daily": [
            {
                "meter_id": meter_id,
                "party": party,
                "units": [money(days.get(day, ZERO)) for day in result.days],
            }
            for meter_id, parties in meter_daily.items()
            for party, days in parties.items()
            if any(days.values())
        ],
        "issues": group_issues(result.issues, snapshot, allocator, date_to),
        "unplaced_meters": [
            {"id": mid, "name": snapshot.meters[mid].name} for mid in snapshot.unplaced
        ],
        "generated_at": timezone.now(),
    }


# ---------------------------------------------------------------------------
# What the cost boards will need. Not called yet: the admin board, the Company
# Expense matrix, the Factory Expense wall and the Electricity dashboard keep
# their old logic until they are moved onto the tree.
# ---------------------------------------------------------------------------


def party_totals(
    date_from: date, date_to: date, *, now=None
) -> Dict[str, Dict[str, Decimal]]:
    """party key -> {"units", "cost"} over the span, exact."""
    result, _, _ = allocate(date_from, date_to, now=now)
    return result.party_totals()


def company_breakdown(date_from: date, date_to: date, *, now=None) -> dict:
    """The allocation cut the way the cost boards read it.

    ``by_party``      party -> {"units", "cost"} for the span.
    ``by_day``        day -> party -> {"units", "cost"}.
    ``by_meter``      party -> meter name -> {"units", "cost"}: which meters a
                      party's figure is made of, at the party's share.
    ``by_day_meter``  day -> party -> meter name -> {"units", "cost"}.
    ``own_by_meter``  meter name -> {"units", "cost", "has_sub_meters"}: each
                      meter's own units (its reading less its sub-meters'),
                      whoever pays for them — what a party's share is OF.
    ``supply``        what the mains read — the bill's side of the reconciliation.
    ``supply_by_meter`` main meter name -> {"units", "cost"}.
    ``parties``       party key -> {"key", "code", "name", "kind"}.
    ``problems``      the grouped issues, most serious first.
    """
    result, snapshot, allocator = allocate(date_from, date_to, now=now)

    def blank():
        return {"units": ZERO, "cost": ZERO}

    by_party: Dict[str, dict] = defaultdict(blank)
    by_day: Dict[date, Dict[str, dict]] = defaultdict(lambda: defaultdict(blank))
    by_meter: Dict[str, Dict[str, dict]] = defaultdict(lambda: defaultdict(blank))
    by_day_meter: Dict[date, Dict[str, Dict[str, dict]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(blank))
    )
    own_by_meter: Dict[str, dict] = defaultdict(
        lambda: {"units": ZERO, "cost": ZERO, "has_sub_meters": False}
    )
    supply_by_meter: Dict[str, dict] = defaultdict(blank)
    parents = {node.parent_id for node in result.nodes if node.parent_id is not None}
    for node in result.nodes:
        name = snapshot.meters[node.meter_id].name if node.meter_id in snapshot.meters else str(node.meter_id)
        if node.parent_id is None and node.measured is not None:
            supply_by_meter[name]["units"] += node.measured
            supply_by_meter[name]["cost"] += node.measured * node.rate
        own = own_by_meter[name]
        own["units"] += node.own or ZERO
        own["cost"] += node.cost
        own["has_sub_meters"] = own["has_sub_meters"] or node.meter_id in parents
        for party, units in node.split.items():
            cost = units * node.rate
            for bucket in (
                by_party[party],
                by_day[node.day][party],
                by_meter[party][name],
                by_day_meter[node.day][party][name],
            ):
                bucket["units"] += units
                bucket["cost"] += cost

    return {
        "by_party": dict(by_party),
        "by_day": {day: dict(parties) for day, parties in by_day.items()},
        "by_meter": {party: dict(meters) for party, meters in by_meter.items()},
        "by_day_meter": {
            day: {party: dict(meters) for party, meters in parties.items()}
            for day, parties in by_day_meter.items()
        },
        "own_by_meter": dict(own_by_meter),
        "supply": result.supply(),
        "supply_by_meter": dict(supply_by_meter),
        "entered_days": list(result.entered_days),
        "parties": {key: party.as_dict() for key, party in snapshot.parties.items()},
        "problems": group_issues(result.issues, snapshot, allocator, result.date_to),
        "unplaced": [snapshot.meters[mid].name for mid in snapshot.unplaced],
    }
