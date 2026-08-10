"""What to actually produce tomorrow.

The gap says what is owed. It does not say what can be *run*, and those are
different numbers: a SKU short 226,000 bottles with 4,000 caps on the shelf is a
4,000-bottle run and a purchase order, not a 226,000-bottle plan.

Three things turn a gap into a plan, and the middle one is the reason this is not
a sort:

**Priority.** Oldest promise first — the earliest delivery date on the order
book, then the money behind it.

**Allocation, not availability.** Components are shared. Computing "buildable"
per SKU independently promises the same 706,738 caps to all six SKUs that use
them and produces a plan that cannot be run. So stock is walked down the
priority list and *spent*: each SKU takes what it needs from what is left, and
the SKU below it sees the remainder. This is what a planner does on paper, and
it is why the answer depends on the order.

**A day is a day.** If the reference template is on file, the plan is also capped
at one day of each line's hours, including a changeover per SKU scheduled on it.

Every row says which of the three limited it — demand, material or capacity — and
a material-limited row names the component and how short it is, so the plan and
the buy list agree with each other by construction.
"""
from datetime import timedelta

MINUTES_PER_HOUR = 60.0

# What limited the run.
BY_DEMAND, BY_MATERIAL, BY_CAPACITY = "DEMAND", "MATERIAL", "CAPACITY"


def _priority_key(sku):
    """Oldest promise first, then the largest money behind it.

    A SKU with no delivery date on the book sorts last rather than first: absence
    of a date is not urgency, and treating it as urgent would push a real
    overdue order down the list.
    """
    return (sku["earliest_due"] or "9999-12-31", -sku["value"])


def build_tomorrow_plan(*, skus, components, capacity, run_date, machines=None,
                        routes=None):
    """Return the runnable plan for ``run_date``, and why each row is that size."""
    gap = [s for s in skus if s["to_produce"] > 0 and s["has_bom"]]
    gap.sort(key=_priority_key)

    stock = {
        c["item"]: c["onhand"]
        for c in components
        if not c["is_resource"]
    }
    by_code = {c["item"]: c for c in components}

    # One day of each line, not a month of it.
    day_hours, changeovers = {}, {}
    machines = machines or {}
    routes = routes or {}
    for machine_id, machine in machines.items():
        day_hours[machine_id] = float(machine.shift_hours or 0) * float(
            machine.shifts_per_day or 0
        )
        changeovers[machine_id] = float(machine.changeover_minutes or 0) / MINUTES_PER_HOUR

    rows = []
    for position, sku in enumerate(gap, start=1):
        wanted = sku["to_produce"]
        buildable, blocker = _buildable(sku, stock, by_code, wanted)

        planned = min(wanted, buildable)
        limited_by = BY_DEMAND if planned >= wanted else BY_MATERIAL

        machine_id, hours, rate = None, None, None
        route = routes.get(sku["item"])
        if route is not None and route.primary_machine_id in machines:
            machine_id = route.primary_machine_id
            machine = machines[machine_id]
            rate = float(route.output_on_primary or 0) or float(machine.output_per_hour or 0)
            if rate > 0:
                left = day_hours.get(machine_id, 0.0)
                if left > 0:
                    left -= changeovers.get(machine_id, 0.0)  # one changeover for this SKU
                capped = max(int(max(left, 0.0) * rate), 0)
                if capped < planned:
                    planned, limited_by = capped, BY_CAPACITY
                hours = round(planned / rate, 2) if rate else None
                day_hours[machine_id] = max(
                    day_hours.get(machine_id, 0.0)
                    - (planned / rate if rate else 0.0)
                    - changeovers.get(machine_id, 0.0),
                    0.0,
                )

        if planned > 0:
            _consume(sku, stock, planned)

        rows.append({
            "priority": position,
            "sku": sku["item"],
            "name": sku["name"],
            "variety": sku["variety"],
            "uom": sku["uom"],
            "orders": sku["orders"],
            "value": sku["value"],
            "earliest_due": sku["earliest_due"],
            "days_late": sku["days_late"] if "days_late" in sku else 0,
            "to_produce": sku["to_produce"],
            "buildable": round(buildable, 2),
            "planned": round(planned, 2),
            "limited_by": limited_by,
            "blocker": blocker,
            "machine": machine_id,
            "hours": hours,
            "rate_per_hour": rate,
            "litres": round(_litres(sku, planned), 2),
        })

    runnable = [r for r in rows if r["planned"] > 0]
    blocked = [r for r in rows if r["planned"] <= 0]
    return {
        "date": run_date.isoformat(),
        "capacity_limited": bool(machines and routes),
        "rows": rows,
        "totals": {
            "skus": len(runnable),
            "pieces": round(sum(r["planned"] for r in runnable), 2),
            "litres": round(sum(r["litres"] for r in runnable), 2),
            "value": round(sum(r["value"] for r in runnable), 2),
            "blocked_skus": len(blocked),
            "blocked_pieces": round(sum(r["to_produce"] for r in blocked), 2),
            "hours": round(sum(r["hours"] or 0 for r in runnable), 2),
        },
    }


def _buildable(sku, stock, by_code, wanted):
    """How many units the remaining component stock actually allows.

    Returns the count and, when something bites before the full gap, the
    component that bit — named, with the arithmetic, so the row can be checked
    and so it points straight at the purchase order that would release it.
    """
    limit = wanted
    blocker = None
    for line in sku["components"]:
        if line["is_resource"] or line["per_unit"] <= 0:
            continue
        component = by_code.get(line["child"])
        if component is None:
            continue
        available = stock.get(line["child"], 0.0)
        possible = available / line["per_unit"]
        if possible < limit:
            limit = possible
            blocker = {
                "item": component["item"],
                "name": component["name"],
                "uom": component["uom"],
                "onhand": round(available, 2),
                "per_unit": line["per_unit"],
                "needed_for_gap": round(line["per_unit"] * wanted, 2),
                "short": round(max(line["per_unit"] * wanted - available, 0), 2),
            }
    return max(int(limit), 0), blocker


def _consume(sku, stock, planned):
    """Spend the components this run uses, so the next SKU sees the remainder."""
    for line in sku["components"]:
        if line["is_resource"] or line["per_unit"] <= 0:
            continue
        used = line["per_unit"] * planned
        stock[line["child"]] = max(stock.get(line["child"], 0.0) - used, 0.0)


def _litres(sku, planned):
    """Fill volume for the run, from the BOM's own conversion-resource line."""
    return sum(
        line["per_unit"] * planned
        for line in sku["components"]
        if line["is_resource"]
    )


def next_working_day(today):
    """Tomorrow. Sunday is not a running day at the plant, so Saturday's plan is
    for Monday — a plan dated on a day nobody is filling is a plan nobody uses."""
    nxt = today + timedelta(days=1)
    if nxt.weekday() == 6:  # Sunday
        nxt += timedelta(days=1)
    return nxt
