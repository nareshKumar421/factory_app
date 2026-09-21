"""
board_builder/cards/dispatch.py

The dispatch clock, split into the legs nobody currently compares.

WHAT THIS MEASURES AND WHY IT IS WORTH A CARD
----------------------------------------------
``SalesDispatchGateOut`` stamps three moments on every docking: ``docked_at``
when the truck is put on a bay, ``printed_at`` when its gate pass is printed,
and ``dispatched_at`` when it leaves. All three are written today and none of
them are ever subtracted from each other.

Doing it splits the wait into two very different problems. Dock-to-print is
LOADING -- boxes onto a truck and a bill reconciled. Print-to-out is the GATE --
paperwork, security, the barrier. Measured over 120 days of production data
the first leg is about 1h 15m and the second about 39m, so roughly two thirds
of the clock sits in the half nobody watches, and a single "turnaround" figure
hides which of the two moved.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from ..catalogue import CardContext, CardOption, CardSpec, register
from .. import viz

WINDOW = CardOption(
    key="window_days",
    label="Window",
    kind="choice",
    default="30",
    choices=(("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 90 days")),
)


def _mean_hours(seconds: list[float]) -> float:
    """Arithmetic mean of a list of second-counts, in hours. Empty reads zero."""
    return (sum(seconds) / len(seconds) / 3600) if seconds else 0.0


def _stage_clock(context: CardContext) -> dict:
    from gate_core.models.sales_dispatch import SalesDispatchGateOut

    days = int(context.option("window_days", "30"))
    since = timezone.now() - timedelta(days=days)

    rows = (
        SalesDispatchGateOut.objects.filter(
            is_active=True,
            company__code=context.company_code,
            docked_at__gte=since,
            printed_at__isnull=False,
            dispatched_at__isnull=False,
        )
        # Only the three stamps: this table is wide and a board must not drag
        # the SAP payload columns across the wire to subtract two datetimes.
        .values_list("docked_at", "printed_at", "dispatched_at")
    )

    load_legs: list[float] = []
    gate_legs: list[float] = []
    for docked, printed, dispatched in rows.iterator(chunk_size=2000):
        load = (printed - docked).total_seconds()
        gate = (dispatched - printed).total_seconds()
        # Out-of-order stamps happen on a record corrected by hand. Dropped
        # rather than clamped: a negative leg averaged in is a wrong figure,
        # and a zero leg is a claim that a stage took no time.
        if load >= 0 and gate >= 0:
            load_legs.append(load)
            gate_legs.append(gate)

    if not load_legs:
        return viz.missing(
            f"No docking completed both stages in the last {days} days.",
            sub="Docked to printed to gone",
        )

    load_hours = _mean_hours(load_legs)
    gate_hours = _mean_hours(gate_legs)
    total = load_hours + gate_hours
    loading_share = round(load_hours / total * 100) if total else 0

    return viz.figure(
        value=f"{total:.2f}",
        unit="h average",
        sub=f"{len(load_legs):,} dockings · last {days} days",
        tag=viz.tag(
            f"{loading_share}% loading",
            "warn" if loading_share >= 60 else "neut",
        ),
        # One scale for the two legs, so the visible gap between the bars is
        # the difference in minutes and not a choice of axis.
        viz=viz.pair(
            [
                ("Dock → print", load_hours, "main"),
                ("Print → out", gate_hours, "light"),
            ],
            note="Average hours per leg, one scale",
        ),
    )


register(
    CardSpec(
        key="dispatch_stage_clock",
        title="Dispatch stage clock",
        summary="Docked → gate pass printed → gone, as two legs on one scale.",
        category="Dispatch",
        feed="sales_dispatch_out",
        columns=2,
        rows=1,
        accent="dispatch",
        options=(WINDOW,),
        build=_stage_clock,
        note=(
            "Only dockings that completed BOTH stages inside the window are "
            "counted, so a truck still on a bay never shortens the average. "
            "Dock-to-print is loading; print-to-out is the gate."
        ),
    )
)
