"""Who pays for a day's electricity, worked out down the meter tree.

The campus is metered as a tree. A main meter measures a supply as it comes in,
and every meter below it measures a slice of its parent: the ground floor is a
slice of the main incomer, the lab a slice of the ground floor. So a parent's
reading already CONTAINS its sub-meters', and adding the whole register up
counts the same electricity once per level. The old register did exactly that.

The rule applied here instead::

    a meter's own units = its reading - what its sub-meters read

Each meter then says who pays for its own units — "the rest of the ground floor
is Beverages", "the lab is half each", "the chillers follow the Sidels' run
hours" — and every unit on the incomer lands in exactly one account. The
company totals add back up to the supply.

Three ways a meter's own units can be split (:class:`Basis`):

* ``FIXED``       — fixed percentages.
* ``RUN_HOURS``   — in proportion to the hours the chosen production lines and
  blowing machines ran that day. Oil ran 24 h and Beverages 12 h, so Oil takes
  two thirds.
* ``METER_RATIO`` — in proportion to what other meters read that day: the
  chillers split the way the two Sidels drew power.

A proportional split that finds nothing to go on (a Sunday, a run nobody
logged) falls back to the setup's fixed shares. With none, the units are left
unassigned and reported as such.

Where the data is short, the engine says so rather than guessing
----------------------------------------------------------------
* A reading that carries on from one several days back (days were skipped)
  covers all of those days, and is **spread** evenly across them. The dial kept
  counting while nobody read it; putting it all on the day it was finally read
  would make that day look like a triple shift and the skipped days look free.
* A reading whose opening does not match the previous closing is a **break**:
  the units in between are in no reading at all.
* A parent that read lower than its sub-meters added up (timing, a misread
  dial, a meter on the wrong parent) has a rest of **zero**, never negative. A
  negative rest would hand some company a refund for a metering fault. The
  excess is reported, so that day's allocation adds to more than the parent
  read.
* An unread meter's own units cannot be allocated. An unread sub-meter's load is
  still inside its parent's reading, so that day it goes with the parent's rest,
  and the issue list says so.

Nothing here rounds. Figures stay exact Decimals until the service formats them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

#: Party key for units no rule claims.
UNASSIGNED = "unassigned"


class Basis:
    """How a meter's own units are split. Mirrored by the model's choices."""

    UNASSIGNED = "UNASSIGNED"
    FIXED = "FIXED"
    RUN_HOURS = "RUN_HOURS"
    METER_RATIO = "METER_RATIO"

    PROPORTIONAL = (RUN_HOURS, METER_RATIO)


class IssueKind:
    """What can be wrong with a day, from the allocation's point of view."""

    #: An in-service meter has no reading covering the day.
    NOT_READ = "NOT_READ"
    #: Nobody entered anything for the day: today, or a day skipped outright.
    DAY_NOT_ENTERED = "DAY_NOT_ENTERED"
    #: One reading covers several days and was spread across them.
    SPREAD = "SPREAD"
    #: A reading's opening does not follow on from the previous closing.
    BREAK = "BREAK"
    #: Sub-meters read more than their parent; the parent's rest is taken as 0.
    OVER_READ = "OVER_READ"
    #: The meter has no split rule yet.
    UNASSIGNED = "UNASSIGNED"
    #: A proportional rule found nothing to go on and used its fixed shares.
    FALLBACK = "FALLBACK"
    #: ...and had no fixed shares either, so the units are unassigned.
    NO_BASIS = "NO_BASIS"
    #: A meter a METER_RATIO rule is measured against was not read.
    DRIVER_NOT_READ = "DRIVER_NOT_READ"
    #: A setup names a parent that is not in service that day.
    PARENT_OUT_OF_SERVICE = "PARENT_OUT_OF_SERVICE"
    #: A meter was read on a day it is not in service, so the reading is unused.
    READ_OUT_OF_SERVICE = "READ_OUT_OF_SERVICE"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Meter:
    id: int
    name: str
    #: Set when this is a second register of another meter — KVAH on the KWH
    #: meter. Read and reported beside it, never part of the tree.
    register_of: Optional[int] = None


@dataclass(frozen=True)
class Share:
    party: str
    percent: Decimal


@dataclass(frozen=True)
class Driver:
    """Something a proportional split is measured against.

    ``source`` is ``"line:<id>"`` or ``"blowing:<id>"`` for run hours, and
    ``"meter:<id>"`` for a meter ratio. ``party`` is who that source stands for.
    """

    source: str
    party: str
    weight: Decimal = ONE


@dataclass(frozen=True)
class Setup:
    """Where a meter sits and who pays for its own units, from a date on."""

    meter_id: int
    effective_from: date
    in_service: bool = True
    parent_id: Optional[int] = None
    basis: str = Basis.UNASSIGNED
    shares: Tuple[Share, ...] = ()
    drivers: Tuple[Driver, ...] = ()
    id: Optional[int] = None


@dataclass(frozen=True)
class Reading:
    meter_id: int
    date: date
    opening: Decimal
    closing: Decimal
    #: Billed units: the dial difference times the multiplying factor.
    units: Decimal
    rate: Decimal
    multiplying_factor: Decimal = ONE
    #: The meter was replaced or its dial reset, so the opening is not expected
    #: to follow on from the previous closing.
    meter_reset: bool = False
    id: Optional[int] = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayUnits:
    """One meter's units on one day, and the reading they came from."""

    units: Decimal
    rate: Decimal
    reading: Reading
    #: How many days the reading covers. 1 unless days were skipped.
    spread_over: int = 1


@dataclass(frozen=True)
class Issue:
    kind: str
    day: date
    meter_id: Optional[int] = None
    units: Optional[Decimal] = None
    detail: Mapping = field(default_factory=dict)


@dataclass
class NodeDay:
    """One meter on one day: what it read, what is its own, and who pays."""

    meter_id: int
    day: date
    parent_id: Optional[int]
    setup: Setup
    #: None when the meter was not read that day.
    measured: Optional[Decimal] = None
    rate: Decimal = ZERO
    spread_over: int = 1
    #: What the sub-meters read, where they were read.
    sub_metered: Decimal = ZERO
    unread_children: Tuple[int, ...] = ()
    #: How far the sub-meters overshot this meter (the rest is then 0).
    over_read: Decimal = ZERO
    #: The meter's own units — its reading less its sub-meters'. None if unread.
    own: Optional[Decimal] = None
    #: party -> units of ``own``. Adds up to ``own`` exactly.
    split: Dict[str, Decimal] = field(default_factory=dict)
    #: The basis actually applied: FIXED when a proportional rule fell back.
    basis_used: str = ""
    fallback_used: bool = False
    #: party -> the hours (RUN_HOURS) or units (METER_RATIO) behind the split.
    drivers: Dict[str, Decimal] = field(default_factory=dict)

    @property
    def cost(self) -> Decimal:
        return (self.own or ZERO) * self.rate

    def cost_split(self) -> Dict[str, Decimal]:
        return {party: units * self.rate for party, units in self.split.items()}


@dataclass
class RegisterDay:
    """A second register (KVAH) against the meter it belongs to, on a day."""

    meter_id: int
    principal_id: int
    day: date
    measured: Optional[Decimal]
    principal_measured: Optional[Decimal]


@dataclass
class Allocation:
    date_from: date
    date_to: date
    days: List[date]
    nodes: List[NodeDay] = field(default_factory=list)
    registers: List[RegisterDay] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    #: Days on which at least one meter has units.
    entered_days: List[date] = field(default_factory=list)

    def party_totals(self) -> Dict[str, Dict[str, Decimal]]:
        """party -> {"units", "cost"} over the whole span."""
        totals: Dict[str, Dict[str, Decimal]] = defaultdict(
            lambda: {"units": ZERO, "cost": ZERO}
        )
        for node in self.nodes:
            for party, units in node.split.items():
                totals[party]["units"] += units
                totals[party]["cost"] += units * node.rate
        return dict(totals)

    def supply(self) -> Dict[str, Decimal]:
        """What the roots read — the supply the tree hangs off."""
        units = cost = ZERO
        for node in self.nodes:
            if node.parent_id is None and node.measured is not None:
                units += node.measured
                cost += node.measured * node.rate
        return {"units": units, "cost": cost}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def spread_readings(
    readings: Iterable[Reading],
) -> Tuple[Dict[int, Dict[date, DayUnits]], List[Issue]]:
    """Each meter's units per day, with skipped days filled from the reading
    that finally covered them.

    A reading covers the days since the meter's previous reading when its
    opening carries straight on from that reading's closing. Otherwise — the
    first reading of a meter, a reset, or a break — it covers its own day only,
    and a break is reported with the units that fell between the two readings.
    """
    by_meter: Dict[int, List[Reading]] = defaultdict(list)
    for reading in readings:
        by_meter[reading.meter_id].append(reading)

    daily: Dict[int, Dict[date, DayUnits]] = defaultdict(dict)
    issues: List[Issue] = []
    for meter_id, rows in by_meter.items():
        rows.sort(key=lambda row: row.date)
        previous: Optional[Reading] = None
        for reading in rows:
            covered = [reading.date]
            if previous is not None and not reading.meter_reset:
                if reading.opening == previous.closing:
                    span = (reading.date - previous.date).days
                    covered = [previous.date + timedelta(days=n) for n in range(1, span + 1)]
                else:
                    issues.append(
                        Issue(
                            IssueKind.BREAK,
                            reading.date,
                            meter_id,
                            units=(reading.opening - previous.closing)
                            * reading.multiplying_factor,
                            detail={
                                "previous_date": previous.date,
                                "previous_closing": previous.closing,
                                "opening": reading.opening,
                            },
                        )
                    )
            share = reading.units / len(covered)
            for day in covered:
                daily[meter_id][day] = DayUnits(share, reading.rate, reading, len(covered))
            previous = reading
    return daily, issues


def fixed_split(shares: Iterable[Share], units: Decimal) -> Dict[str, Decimal]:
    """``units`` split by percentage.

    Divided by the percentages' own total rather than by 100, so a set that
    does not quite add to 100 still hands out every unit — the setup form
    refuses such a set, but history must not leak units if one got through.
    """
    shares = [share for share in shares if share.percent > ZERO]
    total = sum((share.percent for share in shares), ZERO)
    if not total:
        return {}
    split: Dict[str, Decimal] = defaultdict(lambda: ZERO)
    for share in shares:
        split[share.party] += units * share.percent / total
    return dict(split)


class Allocator:
    """Allocate a span of days. Build once per request; it holds no state
    between calls to :meth:`allocate` beyond its inputs."""

    def __init__(
        self,
        meters: Iterable[Meter],
        setups: Iterable[Setup],
        readings: Iterable[Reading],
        run_hours: Optional[Mapping[Tuple[str, date], Decimal]] = None,
    ):
        self.meters = {meter.id: meter for meter in meters}
        self.setups: Dict[int, List[Setup]] = defaultdict(list)
        for setup in setups:
            self.setups[setup.meter_id].append(setup)
        for versions in self.setups.values():
            versions.sort(key=lambda setup: setup.effective_from)
        self.daily, self._reading_issues = spread_readings(readings)
        self.run_hours = run_hours or {}
        self._covered_days = {
            day for per_day in self.daily.values() for day in per_day
        }

    # -- lookups ------------------------------------------------------------

    def setup_on(self, meter_id: int, day: date) -> Optional[Setup]:
        """The version of a meter's setup in force on ``day``."""
        current = None
        for setup in self.setups.get(meter_id, ()):
            if setup.effective_from > day:
                break
            current = setup
        return current

    def in_service_on(self, day: date) -> Dict[int, Setup]:
        """Tree meters in service on ``day`` — registers are never in the tree."""
        found = {}
        for meter_id in self.setups:
            meter = self.meters.get(meter_id)
            if meter is None or meter.register_of is not None:
                continue
            setup = self.setup_on(meter_id, day)
            if setup is not None and setup.in_service:
                found[meter_id] = setup
        return found

    # -- the allocation -----------------------------------------------------

    def allocate(self, date_from: date, date_to: date) -> Allocation:
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        days = [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]
        result = Allocation(date_from, date_to, days)
        result.issues.extend(
            issue for issue in self._reading_issues if date_from <= issue.day <= date_to
        )
        spread_reported = set()
        for day in days:
            self._allocate_day(day, result, spread_reported)
        return result

    def _allocate_day(self, day: date, out: Allocation, spread_reported: set) -> None:
        setups = self.in_service_on(day)
        entered = day in self._covered_days
        if entered:
            out.entered_days.append(day)
        else:
            out.issues.append(Issue(IssueKind.DAY_NOT_ENTERED, day))

        # A reading on a meter that is not in the tree that day is not lost
        # silently: it is reported, and it is the setup that needs fixing.
        for meter_id, per_day in self.daily.items():
            meter = self.meters.get(meter_id)
            if meter is None or meter.register_of is not None:
                continue
            if day in per_day and meter_id not in setups:
                reading = per_day[day].reading
                if reading.date == day:
                    out.issues.append(
                        Issue(
                            IssueKind.READ_OUT_OF_SERVICE,
                            day,
                            meter_id,
                            units=reading.units,
                        )
                    )

        parent: Dict[int, Optional[int]] = {}
        for meter_id, setup in setups.items():
            parent_id = setup.parent_id
            if parent_id is not None and parent_id not in setups:
                out.issues.append(
                    Issue(
                        IssueKind.PARENT_OUT_OF_SERVICE,
                        day,
                        meter_id,
                        detail={"parent_id": parent_id},
                    )
                )
                parent_id = None
            parent[meter_id] = parent_id

        children: Dict[int, List[int]] = defaultdict(list)
        for meter_id, parent_id in parent.items():
            if parent_id is not None:
                children[parent_id].append(meter_id)

        measured = {meter_id: self.daily.get(meter_id, {}).get(day) for meter_id in setups}

        for meter_id, setup in setups.items():
            node = NodeDay(meter_id=meter_id, day=day, parent_id=parent[meter_id], setup=setup)
            reading = measured[meter_id]
            if reading is None:
                if entered:
                    out.issues.append(
                        Issue(
                            IssueKind.NOT_READ,
                            day,
                            meter_id,
                            detail={"parent_id": parent[meter_id]},
                        )
                    )
                out.nodes.append(node)
                continue

            node.measured = reading.units
            node.rate = reading.rate
            node.spread_over = reading.spread_over
            if reading.spread_over > 1:
                key = (meter_id, reading.reading.id, reading.reading.date)
                if key not in spread_reported:
                    spread_reported.add(key)
                    out.issues.append(
                        Issue(
                            IssueKind.SPREAD,
                            reading.reading.date,
                            meter_id,
                            units=reading.reading.units,
                            detail={"days": reading.spread_over},
                        )
                    )

            sub_metered = ZERO
            unread = []
            for child_id in children.get(meter_id, ()):
                child = measured[child_id]
                if child is None:
                    unread.append(child_id)
                else:
                    sub_metered += child.units
            node.sub_metered = sub_metered
            node.unread_children = tuple(unread)

            own = reading.units - sub_metered
            if own < ZERO:
                node.over_read = -own
                out.issues.append(
                    Issue(
                        IssueKind.OVER_READ,
                        day,
                        meter_id,
                        units=-own,
                        detail={"children": tuple(children.get(meter_id, ()))},
                    )
                )
                own = ZERO
            node.own = own
            self._split(node, setup, measured, out)
            out.nodes.append(node)

        for meter_id, meter in self.meters.items():
            principal_id = meter.register_of
            if principal_id is None or principal_id not in setups:
                continue
            register = self.daily.get(meter_id, {}).get(day)
            principal = measured.get(principal_id)
            if register is None and entered:
                out.issues.append(
                    Issue(
                        IssueKind.NOT_READ,
                        day,
                        meter_id,
                        detail={"register_of": principal_id},
                    )
                )
            out.registers.append(
                RegisterDay(
                    meter_id=meter_id,
                    principal_id=principal_id,
                    day=day,
                    measured=register.units if register else None,
                    principal_measured=principal.units if principal else None,
                )
            )

    def _split(self, node: NodeDay, setup: Setup, measured, out: Allocation) -> None:
        units = node.own or ZERO
        basis = setup.basis
        node.basis_used = basis

        # Nothing of its own that day — every unit was a sub-meter's, or the
        # dial did not move. There is nothing to divide, and nobody to name.
        if not units:
            node.split = {}
            return

        if basis == Basis.FIXED and setup.shares:
            node.split = fixed_split(setup.shares, units)
            return

        if basis in Basis.PROPORTIONAL:
            weights: Dict[str, Decimal] = defaultdict(lambda: ZERO)
            for driver in setup.drivers:
                if basis == Basis.RUN_HOURS:
                    amount = self.run_hours.get((driver.source, node.day), ZERO)
                else:
                    amount = self._driver_meter_units(driver, node, measured, out)
                weights[driver.party] += amount * driver.weight
            node.drivers = {party: amount for party, amount in weights.items()}
            total = sum(weights.values(), ZERO)
            if total > ZERO:
                node.split = {
                    party: units * amount / total
                    for party, amount in weights.items()
                    if amount > ZERO
                }
                return
            if setup.shares:
                node.fallback_used = True
                node.basis_used = Basis.FIXED
                node.split = fixed_split(setup.shares, units)
                if units:
                    out.issues.append(
                        Issue(IssueKind.FALLBACK, node.day, node.meter_id, units=units)
                    )
                return
            node.split = {UNASSIGNED: units} if units else {}
            if units:
                out.issues.append(
                    Issue(IssueKind.NO_BASIS, node.day, node.meter_id, units=units)
                )
            return

        # UNASSIGNED, or a FIXED setup with no shares on it.
        node.basis_used = Basis.UNASSIGNED
        node.split = {UNASSIGNED: units} if units else {}
        if units:
            out.issues.append(Issue(IssueKind.UNASSIGNED, node.day, node.meter_id, units=units))

    def _driver_meter_units(self, driver: Driver, node: NodeDay, measured, out) -> Decimal:
        try:
            driver_id = int(driver.source.split(":", 1)[1])
        except (IndexError, ValueError):
            return ZERO
        reading = measured.get(driver_id) or self.daily.get(driver_id, {}).get(node.day)
        if reading is None:
            out.issues.append(
                Issue(
                    IssueKind.DRIVER_NOT_READ,
                    node.day,
                    node.meter_id,
                    detail={"driver_meter_id": driver_id},
                )
            )
            return ZERO
        return reading.units
