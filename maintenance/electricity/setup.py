"""Changing the meter tree safely: validating a setup version and saving it.

The database can hold any parent and any shares; these are the rules it cannot
express on its own:

* **No meter is its own ancestor — on any day.** Versions are dated, so a
  change that is harmless on the day it starts can close a loop a month later,
  when some other meter's version kicks in. Every date on which the tree could
  change is checked, not only the new version's own.
* **A parent is a tree meter that is in service.** A second register is never
  in the tree, so nothing can hang off it.
* **Fixed shares add up to 100**, and a proportional split follows something:
  lines or machines for run hours, other meters for a meter ratio.

:func:`save_setup` applies all of that inside one transaction. It never touches
the meter's own fields — ``is_main``, ``companies`` and the rest belong to the
Daily Electricity page, and the tree keeps its own account beside them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from django.db import transaction
from rest_framework.exceptions import ValidationError

from ..models import (
    ElectricityAllocationBasis,
    ElectricityMeter,
    ElectricityMeterDriver,
    ElectricityMeterSetup,
    ElectricityMeterShare,
)

HUNDRED = Decimal("100")
#: Shares are entered to three decimals; a set within this of 100 is 100.
SHARE_TOLERANCE = Decimal("0.01")

Basis = ElectricityAllocationBasis


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _versions_by_meter(exclude_id: Optional[int] = None) -> Dict[int, List[ElectricityMeterSetup]]:
    rows = ElectricityMeterSetup.objects.filter(is_active=True).order_by("meter_id", "effective_from")
    if exclude_id:
        rows = rows.exclude(pk=exclude_id)
    versions: Dict[int, List[ElectricityMeterSetup]] = {}
    for row in rows.only("id", "meter_id", "effective_from", "in_service", "parent_id"):
        versions.setdefault(row.meter_id, []).append(row)
    return versions


def _in_force(versions: Iterable, on: date):
    current = None
    for version in versions:
        if version.effective_from > on:
            break
        current = version
    return current


def validate_placement(
    meter: ElectricityMeter,
    parent: Optional[ElectricityMeter],
    effective_from: date,
    in_service: bool,
    *,
    replacing: Optional[ElectricityMeterSetup] = None,
) -> None:
    """Refuse a version that would put the tree in an impossible state."""
    if meter.register_of_id:
        raise ValidationError(
            {
                "meter": (
                    f"{meter.name} is a second register of {meter.register_of.name}. "
                    "It is read beside that meter and never sits in the tree."
                )
            }
        )
    if not in_service:
        _refuse_orphans(meter, effective_from, replacing)
        return
    if parent is None:
        return
    if parent.pk == meter.pk:
        raise ValidationError({"parent": "A meter cannot be a sub-meter of itself."})
    if parent.register_of_id:
        raise ValidationError(
            {
                "parent": (
                    f"{parent.name} is a second register of {parent.register_of.name}; "
                    f"put {meter.name} under {parent.register_of.name} instead."
                )
            }
        )

    versions = _versions_by_meter(exclude_id=replacing.pk if replacing else None)
    proposed = ElectricityMeterSetup(
        meter_id=meter.pk,
        effective_from=effective_from,
        in_service=in_service,
        parent_id=parent.pk,
    )
    mine = [v for v in versions.get(meter.pk, []) if v.effective_from != effective_from]
    mine.append(proposed)
    mine.sort(key=lambda v: v.effective_from)
    versions[meter.pk] = mine

    parent_version = _in_force(versions.get(parent.pk, []), effective_from)
    if parent_version is None or not parent_version.in_service:
        raise ValidationError(
            {
                "parent": (
                    f"{parent.name} is not in the meter tree on {effective_from:%d %b %Y}. "
                    f"Put it in service from that date first, or start {meter.name} later."
                )
            }
        )

    # Every date the tree can change on, from this version onwards.
    change_days = sorted(
        {effective_from}
        | {
            v.effective_from
            for rows in versions.values()
            for v in rows
            if v.effective_from > effective_from
        }
    )
    next_mine = [v.effective_from for v in mine if v.effective_from > effective_from]
    stop = next_mine[0] if next_mine else None
    for day in change_days:
        if stop and day >= stop:
            break
        parents = {}
        for meter_id, rows in versions.items():
            version = _in_force(rows, day)
            if version and version.in_service:
                parents[meter_id] = version.parent_id
        seen = {meter.pk}
        cursor = parents.get(meter.pk)
        while cursor is not None:
            if cursor in seen:
                raise ValidationError(
                    {
                        "parent": (
                            f"On {day:%d %b %Y} this would make {meter.name} a "
                            f"sub-meter of one of its own sub-meters."
                        )
                    }
                )
            seen.add(cursor)
            cursor = parents.get(cursor)


def _refuse_orphans(
    meter: ElectricityMeter, effective_from: date, replacing: Optional[ElectricityMeterSetup]
) -> None:
    """A meter cannot leave the tree while sub-meters still hang off it."""
    versions = _versions_by_meter(exclude_id=replacing.pk if replacing else None)
    later = [v.effective_from for v in versions.get(meter.pk, []) if v.effective_from > effective_from]
    until = later[0] if later else None
    stranded = set()
    for meter_id, rows in versions.items():
        if meter_id == meter.pk:
            continue
        for index, version in enumerate(rows):
            if not version.in_service or version.parent_id != meter.pk:
                continue
            ends = rows[index + 1].effective_from if index + 1 < len(rows) else None
            starts_before_until = until is None or version.effective_from < until
            ends_after_start = ends is None or ends > effective_from
            if starts_before_until and ends_after_start:
                stranded.add(meter_id)
    if stranded:
        names = ", ".join(
            sorted(ElectricityMeter.objects.filter(pk__in=stranded).values_list("name", flat=True))
        )
        raise ValidationError(
            {
                "in_service": (
                    f"{names} still {'sits' if len(stranded) == 1 else 'sit'} under "
                    f"{meter.name} after {effective_from:%d %b %Y}. Move "
                    f"{'it' if len(stranded) == 1 else 'them'} first."
                )
            }
        )


def validate_rule(meter: ElectricityMeter, basis: str, shares: List[dict], drivers: List[dict]) -> None:
    """Refuse a split that cannot be worked out."""
    total = sum((Decimal(share["percent"]) for share in shares), Decimal("0"))
    if shares and abs(total - HUNDRED) > SHARE_TOLERANCE:
        raise ValidationError({"shares": f"Shares must add up to 100%; these add up to {total.normalize()}%."})

    parties = [share.get("company") or share.get("consumer") for share in shares]
    if len(parties) != len(set(parties)):
        raise ValidationError({"shares": "Each company can appear only once."})

    if basis == Basis.FIXED and not shares:
        raise ValidationError({"shares": "A fixed split needs at least one share."})
    if basis == Basis.RUN_HOURS:
        if not drivers:
            raise ValidationError({"drivers": "Pick the lines or machines whose run hours this meter follows."})
        if any(driver.get("meter") for driver in drivers):
            raise ValidationError({"drivers": "A run-hours split follows lines and machines, not meters."})
    if basis == Basis.METER_RATIO:
        if not drivers:
            raise ValidationError({"drivers": "Pick the meters this meter's split follows."})
        for driver in drivers:
            followed = driver.get("meter")
            if followed is None:
                raise ValidationError({"drivers": "A meter-ratio split follows meters, not lines."})
            if followed.pk == meter.pk:
                raise ValidationError({"drivers": "A meter cannot follow its own reading."})
            if driver.get("company") is None:
                raise ValidationError({"drivers": f"Say which company {followed.name} stands for."})
    if basis in (Basis.UNASSIGNED, Basis.FIXED) and drivers:
        raise ValidationError({"drivers": "Only a run-hours or meter-ratio split follows anything."})


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


@transaction.atomic
def save_setup(
    meter: ElectricityMeter,
    data: dict,
    *,
    user=None,
    instance: Optional[ElectricityMeterSetup] = None,
) -> ElectricityMeterSetup:
    """Create a version, or correct ``instance``, with its shares and drivers.

    ``data`` is the setup serializer's validated data: ``effective_from``,
    ``in_service``, ``parent``, ``basis``, ``note``, ``shares`` and ``drivers``.
    Omitted keys keep the instance's values.
    """
    def current(key, default=None):
        if key in data:
            return data[key]
        if instance is not None:
            return getattr(instance, key)
        return default

    effective_from = current("effective_from")
    in_service = current("in_service", True)
    parent = current("parent")
    basis = current("basis", Basis.UNASSIGNED)
    if not in_service:
        parent, basis = None, Basis.UNASSIGNED

    if "shares" in data:
        shares = data["shares"]
    elif instance is not None:
        shares = [
            {"company": s.company, "consumer": s.consumer, "percent": s.percent}
            for s in instance.shares.select_related("company", "consumer")
        ]
    else:
        shares = []
    if "drivers" in data:
        drivers = data["drivers"]
    elif instance is not None:
        drivers = [
            {
                "production_line": d.production_line,
                "blowing_machine": d.blowing_machine,
                "meter": d.meter,
                "company": d.company,
                "weight": d.weight,
            }
            for d in instance.drivers.select_related("production_line", "blowing_machine", "meter", "company")
        ]
    else:
        drivers = []
    if not in_service or basis == Basis.UNASSIGNED:
        shares, drivers = [], []

    clash = ElectricityMeterSetup.objects.filter(meter=meter, effective_from=effective_from)
    if instance is not None:
        clash = clash.exclude(pk=instance.pk)
    if clash.exists():
        raise ValidationError(
            {
                "effective_from": (
                    f"{meter.name} already has a version from {effective_from:%d %b %Y}. "
                    "Correct that one instead."
                )
            }
        )

    validate_placement(meter, parent, effective_from, in_service, replacing=instance)
    validate_rule(meter, basis, shares, drivers)

    setup = instance or ElectricityMeterSetup(meter=meter, created_by=user)
    setup.effective_from = effective_from
    setup.in_service = in_service
    setup.parent = parent
    setup.basis = basis
    setup.note = current("note", "") or ""
    setup.updated_by = user
    setup.save()

    setup.shares.all().delete()
    ElectricityMeterShare.objects.bulk_create(
        ElectricityMeterShare(
            setup=setup,
            company=share.get("company"),
            consumer=share.get("consumer"),
            percent=share["percent"],
        )
        for share in shares
    )
    setup.drivers.all().delete()
    ElectricityMeterDriver.objects.bulk_create(
        ElectricityMeterDriver(
            setup=setup,
            production_line=driver.get("production_line"),
            blowing_machine=driver.get("blowing_machine"),
            meter=driver.get("meter"),
            # A line or machine brings its own company; only a followed meter
            # needs one named.
            company=driver.get("company") if driver.get("meter") else None,
            weight=driver.get("weight") or Decimal("1"),
        )
        for driver in drivers
    )
    return setup


@transaction.atomic
def delete_setup(setup: ElectricityMeterSetup) -> None:
    """Remove a version. The meter's other versions close the gap it leaves."""
    meter = setup.meter
    if not ElectricityMeterSetup.objects.filter(meter=meter).exclude(pk=setup.pk).exists():
        raise ValidationError(
            {
                "detail": (
                    f"This is {meter.name}'s only version. To take the meter out of the "
                    "tree, add a version with 'in service' off from the day it was removed."
                )
            }
        )
    # Deleting a version can re-parent the meter onto whatever the previous
    # version said, so the loop check runs as if that version were being saved
    # again on the deleted one's date.
    previous = (
        ElectricityMeterSetup.objects.filter(meter=meter, effective_from__lt=setup.effective_from)
        .order_by("-effective_from")
        .first()
    )
    if previous is not None and previous.in_service and previous.parent_id:
        validate_placement(
            meter,
            previous.parent,
            previous.effective_from,
            True,
            replacing=setup,
        )
    setup.delete()
