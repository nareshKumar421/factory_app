"""Everything the allocation engine needs, read out of the database.

One call, a handful of queries, whatever the span: meters, every setup
version, the readings around the span, and the run hours of whatever the
proportional rules follow. The engine itself never touches the ORM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from django.db.models import Min

from . import engine
from .run_hours import run_hours

#: How far either side of a span readings are loaded. A reading after the span
#: can cover skipped days inside it, and the reading before the span's first
#: one decides where that first one starts from.
READING_MARGIN_DAYS = 62


def company_party(code: str) -> str:
    return f"company:{code}"


def consumer_party(code: str) -> str:
    return f"consumer:{code}"


@dataclass
class Party:
    key: str
    code: str
    name: str
    kind: str  # COMPANY / CONSUMER / UNASSIGNED

    def as_dict(self) -> dict:
        return {"key": self.key, "code": self.code, "name": self.name, "kind": self.kind}


UNASSIGNED_PARTY = Party(engine.UNASSIGNED, "", "Unassigned", "UNASSIGNED")


@dataclass
class Snapshot:
    """What was loaded, kept so the report can name things."""

    meters: Dict[int, object] = field(default_factory=dict)
    setups: Dict[int, List[object]] = field(default_factory=dict)
    parties: Dict[str, Party] = field(default_factory=dict)
    #: Meters nobody has placed in the tree; the loader stood each one up as a
    #: main meter of its own with nobody paying, so its units still show.
    unplaced: List[int] = field(default_factory=list)
    #: "line:8" -> "Sidel", for naming what a split followed.
    source_names: Dict[str, str] = field(default_factory=dict)

    def party(self, key: str) -> Party:
        return self.parties.get(key) or Party(key, key, key, "UNKNOWN")


def _setup_to_engine(setup, snapshot: Snapshot) -> engine.Setup:
    shares = []
    for share in setup.shares.all():
        if share.company_id:
            key = company_party(share.company.code)
            snapshot.parties.setdefault(
                key, Party(key, share.company.code, share.company.name, "COMPANY")
            )
        else:
            key = consumer_party(share.consumer.code)
            snapshot.parties.setdefault(
                key, Party(key, share.consumer.code, share.consumer.name, "CONSUMER")
            )
        shares.append(engine.Share(key, share.percent))

    drivers = []
    for driver in setup.drivers.all():
        company = driver.party_company()
        if company is None:
            continue
        key = company_party(company.code)
        snapshot.parties.setdefault(key, Party(key, company.code, company.name, "COMPANY"))
        source = driver.source_key
        label = driver.production_line or driver.blowing_machine or driver.meter
        snapshot.source_names[source] = getattr(label, "name", source)
        drivers.append(engine.Driver(source, key, driver.weight))

    return engine.Setup(
        meter_id=setup.meter_id,
        effective_from=setup.effective_from,
        in_service=setup.in_service,
        parent_id=setup.parent_id,
        basis=setup.basis,
        shares=tuple(shares),
        drivers=tuple(drivers),
        id=setup.id,
    )


def load_setups(snapshot: Optional[Snapshot] = None) -> Tuple[List[engine.Setup], Snapshot]:
    """Every meter and every setup version, as the engine wants them."""
    from company.models import Company

    from ..models import DailyElectricityReading, ElectricityMeter, ElectricityMeterSetup

    snapshot = snapshot or Snapshot()
    snapshot.meters = {meter.id: meter for meter in ElectricityMeter.objects.all()}
    # Every company is nameable in the report, not only those a rule names
    # today, so a party that drops out of every rule still reads as itself.
    for company in Company.objects.all():
        key = company_party(company.code)
        snapshot.parties.setdefault(key, Party(key, company.code, company.name, "COMPANY"))
    snapshot.parties[engine.UNASSIGNED] = UNASSIGNED_PARTY

    rows = (
        ElectricityMeterSetup.objects.filter(is_active=True)
        .select_related("meter", "parent")
        .prefetch_related(
            "shares__company",
            "shares__consumer",
            "drivers__production_line__company",
            "drivers__blowing_machine__company",
            "drivers__company",
            "drivers__meter",
        )
        .order_by("meter_id", "effective_from")
    )
    setups: List[engine.Setup] = []
    placed = set()
    for row in rows:
        setups.append(_setup_to_engine(row, snapshot))
        snapshot.setups.setdefault(row.meter_id, []).append(row)
        placed.add(row.meter_id)

    # A meter nobody has placed yet is not dropped: it stands as a main meter
    # of its own, from its first reading, with nobody paying for it. That is
    # what the register looked like before the tree, and the report names it
    # so somebody places it.
    first_read = dict(
        DailyElectricityReading.objects.filter(is_active=True)
        .values_list("meter_id")
        .annotate(first=Min("date"))
        .values_list("meter_id", "first")
    )
    for meter_id, meter in snapshot.meters.items():
        if meter_id in placed or meter.register_of_id is not None:
            continue
        if meter_id not in first_read:
            continue
        snapshot.unplaced.append(meter_id)
        setups.append(engine.Setup(meter_id=meter_id, effective_from=first_read[meter_id]))
    return setups, snapshot


def load(
    date_from: date, date_to: date, *, now=None
) -> Tuple[engine.Allocator, Snapshot]:
    """An engine ready to allocate ``date_from``..``date_to``, and what it was built from."""
    from ..models import DailyElectricityReading

    setups, snapshot = load_setups()

    meters = [
        engine.Meter(meter.id, meter.name, meter.register_of_id)
        for meter in snapshot.meters.values()
    ]

    margin = timedelta(days=READING_MARGIN_DAYS)
    readings = [
        engine.Reading(
            meter_id=row.meter_id,
            date=row.date,
            opening=row.opening_reading,
            closing=row.closing_reading,
            units=row.units_consumed,
            rate=row.rate_per_unit,
            multiplying_factor=row.multiplying_factor,
            meter_reset=row.meter_reset,
            id=row.id,
        )
        for row in DailyElectricityReading.objects.filter(
            is_active=True,
            date__gte=date_from - margin,
            date__lte=date_to + margin,
        ).only(
            "id",
            "meter_id",
            "date",
            "opening_reading",
            "closing_reading",
            "units_consumed",
            "rate_per_unit",
            "multiplying_factor",
            "meter_reset",
        )
    ]

    sources = {
        driver.source
        for setup in setups
        if setup.basis == engine.Basis.RUN_HOURS
        for driver in setup.drivers
    }
    hours = run_hours(sources, date_from, date_to, now=now) if sources else {}
    return engine.Allocator(meters, setups, readings, hours), snapshot
