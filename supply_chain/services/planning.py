"""Steps 6 and 7 of the brief — timing and feasibility.

Steps 1-5 (demand, floor, FG gap, BOM explosion, material requirement) are
already produced by ``sales_planning_requirement``: the HANA procedure
``SALES PLANNING VS REQUIREMENT_WEEKLY`` returns per item its ``required_qty``,
``open_po_qty`` and ``net_shortage_qty``. This module does not recompute any of
it. It answers the two questions nothing in the system answers today:

  **Step 6 — when must this be ordered?**  A requirement without a date is a
  report. Applying each material's lead time turns it into the alarm the brief
  is actually asking for: order today, or order by a stated date.

  **Step 7 — can we even run the plan?**  Comparing the production requirement
  against real line capacity, including the changeover time the template
  collects and the brief's own step 7 forgets.

Everything here is a pure read over rows already in Postgres, so it runs without
SAP and is testable without HANA.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from ..models import (
    AlarmState,
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    SupplyChainPolicy,
)

ZERO = Decimal("0")


def _dec(value):
    return Decimal(value or 0)


def _qty(value):
    """``Decimal('5000.000000')`` -> ``'5000'``.

    Quantities are stored at six decimal places; rendering that scale into an
    action list makes every number harder to read for no gain.
    """
    d = _dec(value).normalize()
    return f"{d:f}"


def _round_to_moq(quantity, moq):
    """Round an order up to a whole number of MOQ lots.

    The template collects MOQ and the brief never uses it, so a requirement of 10
    against an MOQ of 500 would be raised as an un-placeable order for 10.
    """
    moq = _dec(moq)
    if moq <= 0 or quantity <= 0:
        return quantity
    lots = (quantity / moq).to_integral_value(rounding="ROUND_CEILING")
    return lots * moq


def _planning_rows(company_code, forecast_id=None):
    """The latest planning rows for a company, newest load first."""
    from sales_planning_requirement.models import SalesPlanningRequirementRow

    qs = SalesPlanningRequirementRow.objects.filter(company_code=company_code)
    if forecast_id is not None:
        qs = qs.filter(forecast_id=forecast_id)
    return qs


def _required_by(row, today):
    """The date the material must be USABLE — the start of the plan period.

    Lead time is defined by the template as order-placed to usable-in-production,
    so it counts back from when production needs it, not from the period end.
    """
    return row.forecast_start_date or row.forecast_end_date or today


def material_alarms(company_code, *, forecast_id=None, today=None, policy=None):
    """Step 6 — the procurement action list, one entry per material.

    Returns ``{"rows": [...], "totals": {...}}`` sorted most urgent first, so the
    top of the list is literally what to order today.
    """
    policy = policy or SupplyChainPolicy.for_company(company_code)
    today = today or timezone.localdate()

    lead_times = {
        lt.material_code: lt
        for lt in MaterialLeadTime.objects.filter(company_code=company_code, is_active=True)
    }
    fg_codes = set(
        MaterialMachineMap.objects.filter(company_code=company_code, is_active=True)
        .values_list("sku_code", flat=True)
    )

    rows = []
    for row in _planning_rows(company_code, forecast_id):
        # Finished goods are produced, not purchased — they belong to step 7.
        if row.item_code in fg_codes:
            continue
        shortage = _dec(row.net_shortage_qty if policy.use_net_of_open_po else row.required_qty)
        lead = lead_times.get(row.item_code)
        required_by = _required_by(row, today)

        if shortage <= 0:
            state, order_by, days_left = AlarmState.COVERED, None, None
        elif lead is None:
            # Not an edge case — it is the reference data gap the template exists
            # to close, and it must be visible, not silently sorted to the bottom.
            state, order_by, days_left = AlarmState.NO_LEAD_TIME, None, None
        else:
            order_by = required_by - timedelta(days=lead.lead_time_days)
            days_left = (order_by - today).days
            if days_left < 0:
                state = AlarmState.OVERDUE
            elif days_left <= policy.urgency_window_days:
                state = AlarmState.ORDER_NOW
            else:
                state = AlarmState.SCHEDULED

        order_qty = shortage
        if policy.apply_moq_rounding and lead is not None:
            order_qty = _round_to_moq(shortage, lead.moq)

        rows.append({
            "item_code": row.item_code,
            "item_name": row.item_name,
            "material_type": lead.material_type if lead else "",
            "supplier_name": lead.supplier_name if lead else "",
            "required_qty": _qty(row.required_qty),
            "stock_in_hand": _qty(row.stock_in_hand),
            "min_stock": _qty(row.min_stock),
            "open_po_qty": _qty(row.open_po_qty),
            "shortage_qty": _qty(shortage),
            "order_qty": _qty(order_qty),
            "moq": _qty(lead.moq) if lead else "",
            "unit": lead.unit if lead else "",
            "lead_time_days": lead.lead_time_days if lead else None,
            "required_by": required_by.isoformat() if required_by else None,
            "order_by": order_by.isoformat() if order_by else None,
            "days_until_order_by": days_left,
            "alarm": state,
        })

    # Most urgent first: overdue, then order-now, then by how little time is left.
    priority = {
        AlarmState.OVERDUE: 0,
        AlarmState.ORDER_NOW: 1,
        AlarmState.NO_LEAD_TIME: 2,
        AlarmState.SCHEDULED: 3,
        AlarmState.COVERED: 4,
    }
    rows.sort(key=lambda r: (
        priority.get(r["alarm"], 9),
        r["days_until_order_by"] if r["days_until_order_by"] is not None else 9999,
        r["item_code"],
    ))

    totals = {state: 0 for state in priority}
    for r in rows:
        totals[r["alarm"]] += 1
    return {
        "rows": rows,
        "totals": {
            "materials": len(rows),
            "overdue": totals[AlarmState.OVERDUE],
            "order_now": totals[AlarmState.ORDER_NOW],
            "no_lead_time": totals[AlarmState.NO_LEAD_TIME],
            "scheduled": totals[AlarmState.SCHEDULED],
            "covered": totals[AlarmState.COVERED],
        },
    }


def capacity_check(company_code, *, forecast_id=None, policy=None):
    """Step 7 — can the plan actually be run on the lines we have?

    Worked in HOURS, not units: two SKUs on one line can have different output
    rates, so summing units across them compares nothing meaningful.
    """
    policy = policy or SupplyChainPolicy.for_company(company_code)

    mappings = {
        m.sku_code: m
        for m in MaterialMachineMap.objects.filter(company_code=company_code, is_active=True)
    }
    machines = {
        m.machine_id: m
        for m in MachineCapacity.objects.filter(company_code=company_code, is_active=True)
    }

    demand = {}      # machine_id -> {"hours": Decimal, "skus": [...]}
    unmapped = []    # produced SKUs with no machine on file
    for row in _planning_rows(company_code, forecast_id):
        mapping = mappings.get(row.item_code)
        if mapping is None:
            continue
        qty = _dec(row.net_shortage_qty if policy.use_net_of_open_po else row.required_qty)
        if qty <= 0:
            continue
        machine = machines.get(mapping.primary_machine_id)
        rate = _dec(mapping.output_on_primary) or (_dec(machine.output_per_hour) if machine else ZERO)
        if machine is None or rate <= 0:
            unmapped.append({
                "sku_code": row.item_code,
                "sku_name": row.item_name,
                "quantity": _qty(qty),
                "primary_machine_id": mapping.primary_machine_id,
                "reason": "No machine on file" if machine is None else "No output rate on file",
            })
            continue
        hours = qty / rate
        bucket = demand.setdefault(machine.machine_id, {"hours": ZERO, "skus": []})
        bucket["hours"] += hours
        bucket["skus"].append({
            "sku_code": row.item_code,
            "sku_name": row.item_name,
            "quantity": _qty(qty),
            "rate_per_hour": _qty(rate),
            "hours": str(hours.quantize(Decimal("0.01"))),
        })

    lines, feasible_all = [], True
    for machine_id, bucket in sorted(demand.items()):
        machine = machines[machine_id]
        # One changeover per SKU scheduled on the line.
        changeover_hours = ZERO
        if policy.include_changeover_in_capacity:
            changeover_hours = (
                _dec(machine.changeover_minutes) * len(bucket["skus"]) / Decimal("60")
            )
        available = machine.available_hours - changeover_hours
        required = bucket["hours"]
        feasible = required <= available
        feasible_all = feasible_all and feasible
        lines.append({
            "machine_id": machine_id,
            "name": machine.name,
            "location": machine.location,
            "available_hours": str(machine.available_hours.quantize(Decimal("0.01"))),
            "changeover_hours": str(changeover_hours.quantize(Decimal("0.01"))),
            "usable_hours": str(available.quantize(Decimal("0.01"))),
            "required_hours": str(required.quantize(Decimal("0.01"))),
            "utilisation_percent": str(
                (required / available * 100).quantize(Decimal("0.1")) if available > 0
                else Decimal("999.9")
            ),
            "shortfall_hours": str(
                (required - available).quantize(Decimal("0.01")) if required > available else "0.00"
            ),
            "feasible": feasible,
            "sku_count": len(bucket["skus"]),
            "alternates_available": sorted({
                alt for sku in bucket["skus"]
                for alt in mappings[sku["sku_code"]].alternates
            }),
            "skus": bucket["skus"],
        })

    return {
        "machines": lines,
        "unmapped_skus": unmapped,
        "totals": {
            "machines": len(lines),
            "over_capacity": sum(1 for l in lines if not l["feasible"]),
            "unmapped_skus": len(unmapped),
            "feasible": feasible_all and not unmapped,
        },
    }


def dashboard(company_code, *, forecast_id=None, today=None):
    """Both steps in one payload — the single view the brief asks for."""
    policy = SupplyChainPolicy.for_company(company_code)
    alarms = material_alarms(company_code, forecast_id=forecast_id, today=today, policy=policy)
    capacity = capacity_check(company_code, forecast_id=forecast_id, policy=policy)
    return {
        "company_code": company_code,
        "generated_at": timezone.now().isoformat(),
        "policy": {
            "floor_percent": str(policy.floor_percent),
            "floor_basis": policy.floor_basis,
            "urgency_window_days": policy.urgency_window_days,
            "use_net_of_open_po": policy.use_net_of_open_po,
            "apply_moq_rounding": policy.apply_moq_rounding,
            "include_changeover_in_capacity": policy.include_changeover_in_capacity,
        },
        "procurement": alarms,
        "production": capacity,
        # The headline the brief wants a HOD to read in one glance.
        "headline": {
            "needs_ordering_today": alarms["totals"]["overdue"] + alarms["totals"]["order_now"],
            "missing_lead_times": alarms["totals"]["no_lead_time"],
            "lines_over_capacity": capacity["totals"]["over_capacity"],
            "plan_is_feasible": capacity["totals"]["feasible"],
        },
    }
