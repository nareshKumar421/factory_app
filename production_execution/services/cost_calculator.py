"""Automatic run-costing engine.

Run cost is *derived* — there is no manual per-run cost entry. Everything is
computed from:
  - the **Cost master** rates (``CostRate``: per-line override > company global)
  - the **Line master** operating profile (``standard_hours_per_month``,
    ``electricity_units_per_hour``)
  - the **BOM material snapshot** on the run (``ProductionMaterialUsage.unit_price``)
  - the run's actuals: running hours ``H`` and produced cases ``Q``.

Labour is paid **for the day** — never divided into running hours. Salary
follows its configured basis: per person/day it is a full-day wage per head;
any other basis (PER_DAY, PER_MONTH, …) is applied like every other category.

Per category:
  MATERIAL              Σ(consumed_qty × snapshot unit_price)         [from BOM]
  ELECTRICITY_VARIABLE  actual metered units × rate when ResourceElectricity
                        entries exist, else (units/hr × H) × rate estimate
  LABOUR                labour_count × rate                           [per person / day]
  WASTE_RECOVERY        waste_qty × rate    [credit; PER_UNIT basis only]
  everything else, by basis:
      PER_UNIT          Q × rate                                     [per case]
      PER_PERSON_DAY    person_count × rate                          [salary / manpower]
      PER_DAY           rate                                         [flat daily charge]
      PER_HOUR          H × rate
      PER_MONTH         (rate / std_hours_per_month) × H

Results are written to ``ProductionRunCostLine`` (per-category breakdown) and
summarised into the ``ProductionRunCost`` rollup header.
"""
import logging
from decimal import Decimal

from django.db.models import Q, Sum

logger = logging.getLogger(__name__)

ZERO = Decimal('0')


def _resolve_rates(company, line):
    """Return {category: CostRate}, with a per-line override beating the global."""
    from production_execution.models import CostRate

    resolved = {}
    qs = CostRate.objects.filter(company=company, is_active=True).filter(
        Q(line__isnull=True) | Q(line=line)
    )
    for rate in qs:
        current = resolved.get(rate.category)
        # Line-specific always wins; otherwise first-seen (global) fills the slot.
        if current is None or rate.line_id is not None:
            resolved[rate.category] = rate
    return resolved


def _q2(value: Decimal) -> Decimal:
    return (value or ZERO).quantize(Decimal('0.01'))


def recalculate_run_cost(production_run) -> None:
    """Recalculate and persist the derived cost (lines + rollup) for a run."""
    from production_execution.models import (
        CostCategory as C,
        CostBasis as B,
        ProductionRunCost,
        ProductionRunCostLine,
    )

    run = production_run
    line = run.line
    company = run.company

    H = Decimal(run.total_running_minutes or 0) / Decimal('60')  # running hours
    Q = Decimal(str(run.total_production or 0))                   # produced cases
    labour_count = Decimal(run.labour_count or 0)
    salaried_count = Decimal(run.other_manpower_count or 0)
    elec_units_per_hour = Decimal(str(line.electricity_units_per_hour or 0))
    std_hours = Decimal(str(line.standard_hours_per_month or 0))

    waste_qty = Decimal(str(
        run.waste_logs.aggregate(t=Sum('wastage_qty'))['t'] or 0
    ))
    actual_elec_units = Decimal(str(
        run.electricity_usage.aggregate(t=Sum('units_consumed'))['t'] or 0
    ))

    rates = _resolve_rates(company, line)
    computed = []  # list of dicts → ProductionRunCostLine

    # --- Material (from BOM snapshot; not a CostRate) -----------------------
    material_amount = ZERO
    material_items = 0
    for m in run.material_usages.all():
        if m.unit_price is None:
            continue
        consumed = (m.opening_qty or ZERO) + (m.issued_qty or ZERO) - (m.closing_qty or ZERO)
        material_amount += Decimal(str(consumed)) * m.unit_price
        material_items += 1
    if material_amount != ZERO:
        computed.append({
            'category': C.MATERIAL, 'basis': '', 'quantity': Q, 'rate': ZERO,
            'amount': _q2(material_amount), 'is_credit': False,
            'note': f"{material_items} BOM item(s) × LastPurPrc",
        })

    # --- Rate-driven categories --------------------------------------------
    for category, rate_row in rates.items():
        rate = rate_row.rate or ZERO
        basis = rate_row.basis
        qty = ZERO
        amount = ZERO
        note = ''

        if category == C.ELECTRICITY_VARIABLE:
            # Actual recorded units (meter/bill entries) win over the
            # line-profile estimate of units/hr × running hours.
            if actual_elec_units > 0:
                qty = actual_elec_units
                amount = qty * rate
                note = f"{qty:.2f} units (metered) × ₹{rate}"
            else:
                qty = elec_units_per_hour * H
                amount = qty * rate
                note = f"{qty:.2f} units (est. {elec_units_per_hour}/h × {H:.2f} h) × ₹{rate}"
        elif category == C.LABOUR:
            # Labour is a full-day wage per worker — never split into hours.
            qty = labour_count
            amount = labour_count * rate
            note = f"{labour_count:.0f} × ₹{rate}/day"
        elif category == C.WASTE_RECOVERY:
            # Waste recovery is a per-unit scrap credit (waste_qty × rate).
            # Any other basis is a misconfigured Cost Master row — applying
            # e.g. a PER_MONTH rate per waste unit yields absurd credits.
            if basis != B.PER_UNIT:
                logger.warning(
                    "Skipping WASTE_RECOVERY rate id=%s for run %s: basis %s "
                    "is not PER_UNIT", rate_row.id, run.id, basis,
                )
                continue
            qty = waste_qty
            amount = qty * rate
            note = f"{qty} × ₹{rate} (credit)"
        elif category == C.MANPOWER_SALARIED and basis == B.PER_PERSON_DAY:
            # Salaried headcount priced per person/day — full-day wage per
            # head. Any other basis (PER_DAY, PER_MONTH, …) falls through to
            # the generic basis handlers below so the configured basis is
            # honoured (a PER_MONTH salary is prorated over run hours).
            qty = salaried_count
            amount = salaried_count * rate
            note = f"{salaried_count:.0f} × ₹{rate}/day"
        elif basis == B.PER_PERSON_DAY:
            # Any other per-person/day cost — daily rate, not divided into hours.
            qty = Decimal('1')
            amount = rate
            note = f"₹{rate}/day"
        elif basis == B.PER_UNIT:
            qty = Q
            amount = Q * rate
            note = f"{Q} cases × ₹{rate}"
        elif basis == B.PER_DAY:
            # Flat daily charge — applied once per run, not split into hours.
            qty = Decimal('1')
            amount = rate
            note = f"₹{rate}/day (fixed)"
        elif basis == B.PER_HOUR:
            qty = H
            amount = H * rate
            note = f"{H:.2f} h × ₹{rate}"
        elif basis == B.PER_MONTH:
            if std_hours > 0:
                qty = H
                amount = (rate / std_hours) * H
                note = f"₹{rate}/mo ÷ {std_hours:.0f} h × {H:.2f} h"
            else:
                note = "no std hours/month set on line"

        if amount == ZERO:
            continue
        computed.append({
            'category': category, 'basis': basis, 'quantity': qty, 'rate': rate,
            'amount': _q2(amount), 'is_credit': rate_row.is_credit,
            'note': note,
        })

    # --- Persist cost lines -------------------------------------------------
    run.cost_lines.all().delete()
    ProductionRunCostLine.objects.bulk_create([
        ProductionRunCostLine(production_run=run, **c) for c in computed
    ])

    # --- Roll up into the summary header -----------------------------------
    by_cat = {c['category']: c['amount'] for c in computed}

    def amt(*cats):
        return sum((by_cat.get(c, ZERO) for c in cats), ZERO)

    raw_material_cost = amt(C.MATERIAL)
    labour_cost = amt(C.LABOUR, C.MANPOWER_SALARIED)
    electricity_cost = amt(C.ELECTRICITY_VARIABLE, C.ELECTRICITY_FIXED)
    water_cost = amt(C.WATER)
    overhead_cost = amt(
        C.LUBRICATION, C.LAB_CHEMICALS, C.BATCH_CODING,
        C.MAINTENANCE, C.OVERHEAD, C.OTHER,
    )
    waste_recovery_credit = amt(C.WASTE_RECOVERY)

    total_cost = sum(
        (c['amount'] for c in computed if not c['is_credit']), ZERO
    )
    credits = sum((c['amount'] for c in computed if c['is_credit']), ZERO)
    net_cost = total_cost - credits
    per_unit = (net_cost / Q) if Q > 0 else ZERO

    ProductionRunCost.objects.update_or_create(
        production_run=run,
        defaults={
            'raw_material_cost': raw_material_cost,
            'labour_cost': labour_cost,
            'machine_cost': ZERO,
            'electricity_cost': electricity_cost,
            'water_cost': water_cost,
            'gas_cost': ZERO,
            'compressed_air_cost': ZERO,
            'overhead_cost': overhead_cost,
            'waste_recovery_credit': waste_recovery_credit,
            'total_cost': _q2(total_cost),
            'net_cost': _q2(net_cost),
            'produced_qty': Q,
            'per_unit_cost': per_unit.quantize(Decimal('0.0001')),
        }
    )
    logger.debug(
        f"Recalculated cost for run {run.id}: total={total_cost}, "
        f"net={net_cost}, per_unit={per_unit}"
    )
