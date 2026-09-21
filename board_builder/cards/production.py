"""
board_builder/cards/production.py

What the lines lost, and to what.

AVAILABILITY IS ALREADY IN THE TABLE
-------------------------------------
``ProductionRun`` records ``total_running_minutes`` and
``total_breakdown_time`` on every run. Those two are availability -- one of the
three OEE terms -- with nothing further to capture and nothing to join. The
board has never shown them.

Aggregated over a quarter of production data, one line is not like the others:
Sidel averages 213 breakdown minutes a run where every other line is between 3
and 14. A figure that large is either the plant's biggest maintenance problem
or a data-entry habit on one line, and both are worth a tile that says which
line it is rather than an average that buries it.

WHY THE CARD SHOWS A LIST AND NOT A TOTAL
------------------------------------------
"The plant lost 40 minutes a run" is a number nobody can act on. Breakdown is
never spread evenly across lines -- it is one machine, most of the time -- so
the roll-up is the part that hides the finding. The headline here is the plant
total and the visualisation names the worst lines under it, which is the order
somebody walking to the floor needs them in.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Sum
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

WORST = CardOption(
    key="lines",
    label="Lines shown",
    kind="int",
    default=4,
    minimum=2,
    maximum=8,
    help="How many of the worst lines the list names.",
)


def _downtime(context: CardContext) -> dict:
    from production_execution.models import ProductionRun

    days = int(context.option("window_days", "30"))
    shown = int(context.option("lines", 4))
    since = timezone.localdate() - timedelta(days=days)

    rows = list(
        ProductionRun.objects.filter(
            company__code=context.company_code,
            date__gte=since,
        )
        .values("line__name")
        .annotate(runs=Count("id"), breakdown=Sum("total_breakdown_time"))
        .order_by()
    )

    if not rows:
        return viz.missing(
            f"No run was recorded in the last {days} days.",
            sub="Breakdown minutes per run",
        )

    total_runs = sum(row["runs"] for row in rows)
    total_breakdown = sum(row["breakdown"] or 0 for row in rows)
    plant_average = total_breakdown / total_runs if total_runs else 0

    per_line = sorted(
        (
            {
                "name": row["line__name"] or "Unnamed line",
                "runs": row["runs"],
                "per_run": (row["breakdown"] or 0) / row["runs"] if row["runs"] else 0,
            }
            for row in rows
        ),
        key=lambda line: line["per_run"],
        reverse=True,
    )

    worst = per_line[0]
    # A line an order of magnitude past the plant average is the finding; a
    # line merely above it is Tuesday. The multiple is what decides the tone,
    # not the absolute, because a slow-cycle line legitimately loses more.
    tone = "bad" if worst["per_run"] >= plant_average * 3 and plant_average else "neut"

    return viz.figure(
        value=f"{plant_average:,.0f}",
        unit="min / run",
        sub=f"{total_runs:,} runs on {len(rows)} lines · last {days} days",
        tag=viz.tag(f"worst {worst['name']}", tone),
        viz=viz.table(
            columns=["Line", "Runs", "Breakdown / run"],
            rows=[
                [line["name"], f"{line['runs']:,}", f"{line['per_run']:,.0f} min"]
                for line in per_line[:shown]
            ],
            aligns=["left", "right", "right"],
        ),
    )


register(
    CardSpec(
        key="production_line_downtime",
        title="Line downtime",
        summary="Breakdown minutes per run, plant average with the worst lines named.",
        category="Production",
        feed="production_reports",
        columns=2,
        rows=2,
        accent="production",
        options=(WINDOW, WORST),
        build=_downtime,
        note=(
            "Breakdown minutes as recorded on the run, divided by runs -- not "
            "by running time, so a line that is rarely scheduled is not "
            "flattered by it. Runs that were never closed carry implausible "
            "running times on some lines; this card reads breakdown only, "
            "which is unaffected."
        ),
    )
)
