"""
dispatch_plans/freight_benchmark_service.py

Reads and writes the freight benchmark table (see `models_freight_benchmark`).

The one rule that is not a field check lives here: a destination may not hold
rates on two slabs whose capacity bands overlap. A vehicle has to fall into
exactly one of a destination's slabs for "the benchmark for this truck" to mean
anything, and overlap can arrive from either side -- a rate put on a second
slab, or a slab's band widened under destinations that already have rates on
its neighbours -- so both writers check.
"""

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from django.db import transaction
from django.db.models import Count, Prefetch

from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightSlab,
)

# How many destinations an overlap refusal names before it says "and N more".
MAX_NAMED_CONFLICTS = 3


class FreightBenchmarkError(Exception):
    """A write the table refuses, with the reason in words for the user."""


def normalise_text(value: Any) -> str:
    """Upper-case with the whitespace squeezed, as the workbook writes places."""
    return " ".join(str(value or "").split()).upper()


def _band(slab: FreightSlab) -> str:
    if slab.above_kg == 0:
        return f"up to {slab.up_to_kg:,} kg"
    return f"{slab.above_kg + 1:,}-{slab.up_to_kg:,} kg"


def _first_overlap(slabs: Iterable[FreightSlab]):
    ordered = sorted(slabs, key=lambda s: (s.above_kg, s.up_to_kg))
    for lower, upper in zip(ordered, ordered[1:]):
        if lower.overlaps(upper):
            return lower, upper
    return None


def _named(names: List[str]) -> str:
    shown = ", ".join(names[:MAX_NAMED_CONFLICTS])
    extra = len(names) - MAX_NAMED_CONFLICTS
    return f"{shown} and {extra} more" if extra > 0 else shown


# ---------------------------------------------------------------------- #
# read
# ---------------------------------------------------------------------- #
def benchmark_table() -> Dict[str, Any]:
    """Every slab and every destination with its rates, for the page."""
    # Ordered explicitly: an aggregate drops the model's default ordering.
    slabs = list(
        FreightSlab.objects.annotate(destination_count=Count("benchmarks")).order_by(
            *FreightSlab._meta.ordering
        )
    )
    destinations = list(
        FreightDestination.objects.select_related("updated_by").prefetch_related(
            Prefetch(
                "benchmarks",
                queryset=FreightBenchmark.objects.order_by("slab__sort_order"),
            )
        )
    )
    return {"slabs": slabs, "destinations": destinations}


# ---------------------------------------------------------------------- #
# destinations
# ---------------------------------------------------------------------- #
def save_destination(
    *,
    data: Dict[str, Any],
    user=None,
    destination: Optional[FreightDestination] = None,
) -> FreightDestination:
    """
    Create or replace one destination and its rates.

    `data["rates"]` is the destination's whole rate list -- `[{slab, basis,
    amount}]` with `slab` a FreightSlab -- and replaces what it had: a slab left
    out loses its rate. That is what the edit form means when a box is cleared.
    """
    state = normalise_text(data["state"])
    name = normalise_text(data["name"])
    if not state or not name:
        raise FreightBenchmarkError("A destination needs a state and a name.")

    clash = FreightDestination.objects.filter(state=state, name=name)
    if destination is not None:
        clash = clash.exclude(pk=destination.pk)
    if clash.exists():
        raise FreightBenchmarkError(f"{name} is already listed under {state}.")

    rates = list(data.get("rates") or [])
    slab_ids = [rate["slab"].pk for rate in rates]
    if len(slab_ids) != len(set(slab_ids)):
        raise FreightBenchmarkError("Each slab can carry only one rate.")
    overlap = _first_overlap(rate["slab"] for rate in rates)
    if overlap:
        lower, upper = overlap
        raise FreightBenchmarkError(
            f"{lower.label} ({_band(lower)}) and {upper.label} ({_band(upper)}) "
            "overlap, so a vehicle could fall into both. Keep a rate on one of them."
        )

    with transaction.atomic():
        if destination is None:
            destination = FreightDestination()
        destination.state = state
        destination.name = name
        destination.district = normalise_text(data.get("district"))
        destination.pin_code = str(data.get("pin_code") or "").strip()
        destination.distance_km = data.get("distance_km")
        destination.remarks = str(data.get("remarks") or "").strip()
        destination.is_active = data.get("is_active", True)
        destination.updated_by = user
        destination.save()

        existing = {b.slab_id: b for b in destination.benchmarks.all()}
        for rate in rates:
            slab = rate["slab"]
            amount = Decimal(rate["amount"])
            basis = rate["basis"]
            current = existing.pop(slab.pk, None)
            if current is None:
                FreightBenchmark.objects.create(
                    destination=destination,
                    slab=slab,
                    basis=basis,
                    amount=amount,
                    updated_by=user,
                )
            elif current.amount != amount or current.basis != basis:
                # Only a changed rate is re-stamped, so "last changed by" on a
                # rate names whoever last changed THAT rate.
                current.amount = amount
                current.basis = basis
                current.updated_by = user
                current.save()
        if existing:
            FreightBenchmark.objects.filter(
                pk__in=[b.pk for b in existing.values()]
            ).delete()

    return destination


def delete_destination(destination: FreightDestination) -> None:
    destination.delete()


# ---------------------------------------------------------------------- #
# slabs
# ---------------------------------------------------------------------- #
def save_slab(
    *, data: Dict[str, Any], slab: Optional[FreightSlab] = None
) -> FreightSlab:
    label = " ".join(str(data["label"] or "").split())
    above_kg = int(data.get("above_kg") or 0)
    up_to_kg = int(data["up_to_kg"])
    if not label:
        raise FreightBenchmarkError("A slab needs a label.")
    if up_to_kg <= above_kg:
        raise FreightBenchmarkError(
            "A slab's upper limit must be above its lower limit."
        )

    clash = FreightSlab.objects.filter(label__iexact=label)
    if slab is not None:
        clash = clash.exclude(pk=slab.pk)
    if clash.exists():
        raise FreightBenchmarkError(f"There is already a slab called {label}.")

    candidate = FreightSlab(
        pk=slab.pk if slab else None,
        label=label,
        above_kg=above_kg,
        up_to_kg=up_to_kg,
    )
    if slab is not None and (slab.above_kg, slab.up_to_kg) != (above_kg, up_to_kg):
        # A new band can run into a neighbour on a destination that already has
        # rates on both. Name those destinations rather than saving it.
        conflicts = []
        rated = FreightDestination.objects.filter(benchmarks__slab=slab).prefetch_related(
            "benchmarks__slab"
        )
        for destination in rated:
            others = [b.slab for b in destination.benchmarks.all() if b.slab_id != slab.pk]
            if any(candidate.overlaps(other) for other in others):
                conflicts.append(destination.name)
        if conflicts:
            raise FreightBenchmarkError(
                f"{label} as {_band(candidate)} would overlap another slab at "
                f"{_named(sorted(conflicts))}, which have rates on both."
            )

    if slab is None:
        slab = FreightSlab()
    slab.label = label
    slab.above_kg = above_kg
    slab.up_to_kg = up_to_kg
    slab.sort_order = int(data.get("sort_order") or 0)
    slab.is_active = data.get("is_active", True)
    slab.save()
    return slab


def delete_slab(slab: FreightSlab) -> None:
    in_use = slab.benchmarks.count()
    if in_use:
        raise FreightBenchmarkError(
            f"{in_use} destination{'s have' if in_use != 1 else ' has'} a rate on "
            f"{slab.label}. Clear those rates first, or mark the slab inactive."
        )
    slab.delete()
