"""
Cost auto-computation for a BlowingRun — two-bucket model (preform vs blowing).

Rates come from the blowing Cost Master (``BlowingCostRate``): company-wide
defaults with per-machine overrides, resolved override-first at compute time.
Electricity is split in two:
  * ELECTRICITY_MACHINE  — metered consumption = machine meter units × ₹/unit.
  * ELECTRICITY_UTILITY  — flat ₹/day utility charge, entered per run (utility_cost).

Blowing cost = operator + labour + electricity(machine + utility) + wastage
               + packing − scrap. Preform (resin) cost is the per-bottle side.
Compared per bottle against an editable benchmark (default 0.50) and, for the
total, against the effective landed buy price (make-vs-buy).

``compute_run_cost`` is pure when handed a resolved ``rates`` dict (so tests need
no DB); ``recalculate_run_cost`` resolves the catalog + market price and persists.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# Category keys — mirror blowing.models.BlowingCostCategory (kept as literals so
# this module stays import-light / unit-testable).
OPERATOR = 'OPERATOR'
LABOUR = 'LABOUR'
ELEC_MACHINE = 'ELECTRICITY_MACHINE'
ELEC_UTILITY = 'ELECTRICITY_UTILITY'
PACKING = 'PACKING'
SCRAP = 'SCRAP_RECOVERY'
WASTAGE = 'WASTAGE'
BENCHMARK = 'BENCHMARK_BLOWING_PER_BOTTLE'

DEFAULT_BASIS = {
    OPERATOR: 'PER_PERSON_DAY',
    LABOUR: 'PER_PERSON_DAY',
    ELEC_MACHINE: 'PER_UNIT',   # metered: machine meter units × ₹/unit
    ELEC_UTILITY: 'PER_DAY',    # flat ₹/day charge entered per run
    PACKING: 'PER_BOTTLE',
    SCRAP: 'PER_BOTTLE',
    WASTAGE: 'PER_BOTTLE',
}


def _dec(v):
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


def _resolve_rates(company, machine):
    """Return ``{category: {'rate','is_credit','basis'}}`` — company-wide defaults
    overlaid with the machine's overrides (override wins)."""
    from blowing.models import BlowingCostRate

    resolved = {}
    qs = BlowingCostRate.objects.filter(company=company, is_active=True)
    for r in qs.filter(machine__isnull=True):
        resolved[r.category] = {'rate': r.rate, 'is_credit': r.is_credit, 'basis': r.basis}
    if machine is not None:
        for r in qs.filter(machine=machine):
            resolved[r.category] = {'rate': r.rate, 'is_credit': r.is_credit, 'basis': r.basis}
    return resolved


def _resolve_market_price(run):
    """Effective landed buy price for this run's bottle, or None if no quote."""
    from blowing.models import BottleBuyPrice

    bp = (
        BottleBuyPrice.objects
        .filter(company=run.company, preform_spec=run.preform_spec,
                is_active=True, effective_from__lte=run.date)
        .order_by('-effective_from')
        .first()
    )
    return bp.landed_cost_per_bottle if bp else None


def compute_run_cost(run, rates=None, market_price=None) -> dict:
    """Pure cost computation given resolved ``rates`` (a category→info dict). If
    ``rates`` is None the catalog + market price are resolved from the DB."""
    if rates is None:
        rates = _resolve_rates(run.company, run.machine)
        if market_price is None:
            market_price = _resolve_market_price(run)

    def rate_of(cat):
        return _dec(rates.get(cat, {}).get('rate', 0))

    def basis_of(cat):
        return rates.get(cat, {}).get('basis') or DEFAULT_BASIS.get(cat, '')

    produced = _dec(run.total_counter_production)
    rejects = _dec(run.rejection_pcs)
    good = int(run.total_counter_production or 0) - int(run.rejection_pcs or 0)
    good = good if good > 0 else 0
    good_d = Decimal(good)

    op_count = _dec(run.operator_count)
    labour_count = _dec(run.contract_labour_count + run.own_labour_count)
    total_units = _dec(run.total_units)
    preform_rate = _dec(run.preform_rate_per_bottle)

    # --- blowing-cost components (catalog-driven) -----------------------
    machine_units = _dec(run.machine_units)
    operator_cost = op_count * rate_of(OPERATOR)
    labour_cost = labour_count * rate_of(LABOUR)
    # Machine electricity = metered meter units × ₹/unit (the variable/metered cost).
    electricity_machine_cost = machine_units * rate_of(ELEC_MACHINE)
    # Utility electricity = a flat ₹/day charge the operator enters per run.
    electricity_utility_cost = _dec(run.utility_cost)
    electricity_cost = electricity_machine_cost + electricity_utility_cost
    packing_cost = good_d * rate_of(PACKING)
    wastage_cost = rejects * preform_rate                        # rejected bottles' resin
    scrap_recovery = rejects * rate_of(SCRAP)
    scrap_carton = _dec(run.scrap_carton_value)
    scrap_total = scrap_recovery + scrap_carton

    blowing_cost = (
        operator_cost + labour_cost + electricity_cost
        + wastage_cost + packing_cost - scrap_total
    )

    # --- preform (resin) side -------------------------------------------
    preform_cost = produced * preform_rate

    # --- per-bottle + benchmark + market --------------------------------
    blowing_cost_per_bottle = (blowing_cost / good_d) if good > 0 else Decimal('0')
    preform_cost_per_bottle = (preform_cost / good_d) if good > 0 else Decimal('0')
    total_per_bottle_cost = preform_cost_per_bottle + blowing_cost_per_bottle
    benchmark = rate_of(BENCHMARK)

    # --- fixed/variable split (kept for the make-vs-buy breakeven report)
    # Utility is the flat ₹/day charge (fixed); machine electricity is metered (variable).
    fixed_cost_total = operator_cost + labour_cost + electricity_utility_cost
    variable_cost_total = (
        preform_cost + electricity_machine_cost + packing_cost + wastage_cost - scrap_total
    )
    fully_loaded_cost = preform_cost + blowing_cost
    variable_cost_per_bottle = (variable_cost_total / good_d) if good > 0 else Decimal('0')

    # --- per-category cost lines (drive the expandable UI breakdown) ----
    cost_lines = [
        {'category': OPERATOR, 'basis': basis_of(OPERATOR), 'quantity': op_count,
         'rate': rate_of(OPERATOR), 'amount': operator_cost, 'is_credit': False,
         'note': f"{int(op_count)} × ₹{rate_of(OPERATOR)}/day"},
        {'category': LABOUR, 'basis': basis_of(LABOUR), 'quantity': labour_count,
         'rate': rate_of(LABOUR), 'amount': labour_cost, 'is_credit': False,
         'note': f"{int(labour_count)} × ₹{rate_of(LABOUR)}/day"},
        {'category': ELEC_MACHINE, 'basis': basis_of(ELEC_MACHINE), 'quantity': machine_units,
         'rate': rate_of(ELEC_MACHINE), 'amount': electricity_machine_cost, 'is_credit': False,
         'note': f"{machine_units} units × ₹{rate_of(ELEC_MACHINE)}"},
        {'category': ELEC_UTILITY, 'basis': basis_of(ELEC_UTILITY), 'quantity': Decimal('1'),
         'rate': electricity_utility_cost, 'amount': electricity_utility_cost, 'is_credit': False,
         'note': f"₹{electricity_utility_cost}/day (fixed)"},
        {'category': WASTAGE, 'basis': basis_of(WASTAGE), 'quantity': rejects,
         'rate': preform_rate, 'amount': wastage_cost, 'is_credit': False,
         'note': f"{int(rejects)} rejects × ₹{preform_rate} preform"},
        {'category': PACKING, 'basis': basis_of(PACKING), 'quantity': good_d,
         'rate': rate_of(PACKING), 'amount': packing_cost, 'is_credit': False,
         'note': f"{good} bottles × ₹{rate_of(PACKING)}/bottle"},
        {'category': SCRAP, 'basis': basis_of(SCRAP), 'quantity': rejects,
         'rate': rate_of(SCRAP), 'amount': scrap_total, 'is_credit': True,
         'note': (f"{int(rejects)} rejects × ₹{rate_of(SCRAP)}"
                  + (f" + ₹{scrap_carton} carton" if scrap_carton else ""))},
    ]

    market = _dec(market_price) if market_price is not None else None

    return {
        # blowing-cost components
        'operator_cost': operator_cost,
        'labour_cost': labour_cost,
        'wastage_cost': wastage_cost,
        'electricity_machine_cost': electricity_machine_cost,
        'electricity_utility_cost': electricity_utility_cost,
        'electricity_cost': electricity_cost,
        'packing_cost': packing_cost,
        'scrap_bottle_value': scrap_recovery,
        'scrap_total': scrap_total,
        'blowing_cost': blowing_cost,
        'blowing_cost_per_bottle': blowing_cost_per_bottle,
        # preform side
        'good_bottles': good,
        'preform_cost': preform_cost,
        'preform_cost_per_bottle': preform_cost_per_bottle,
        # totals + comparisons
        'total_per_bottle_cost': total_per_bottle_cost,
        'benchmark_blowing_per_bottle': benchmark,
        'market_price_per_bottle': market,
        # legacy/compat fields (make-vs-buy report + old serializer)
        'total_cost': operator_cost + labour_cost + electricity_cost + wastage_cost + packing_cost,
        'net_cost': fully_loaded_cost,
        'per_bottle_cost': total_per_bottle_cost,
        'variable_cost_total': variable_cost_total,
        'fixed_cost_total': fixed_cost_total,
        'fully_loaded_cost': fully_loaded_cost,
        'variable_cost_per_bottle': variable_cost_per_bottle,
        'make_cost_per_bottle': total_per_bottle_cost,
        # unused-but-retained columns default to 0
        'mould_amortization': Decimal('0'),
        'machine_depreciation': Decimal('0'),
        'maintenance_cost': Decimal('0'),
        'overhead_cost': Decimal('0'),
        'qa_cost': Decimal('0'),
        # lines (popped off before writing BlowingRunCost)
        'cost_lines': cost_lines,
    }


def recalculate_run_cost(run) -> None:
    """Resolve the catalog + market price, recompute, and persist the cost
    summary and per-category cost lines for a run."""
    from blowing.models import BlowingRunCost, BlowingRunCostLine

    values = compute_run_cost(run)
    lines = values.pop('cost_lines', [])

    obj, _ = BlowingRunCost.objects.update_or_create(run=run, defaults=values)
    run.cost_summary = obj

    # Replace the run's cost lines.
    BlowingRunCostLine.objects.filter(run=run).delete()
    BlowingRunCostLine.objects.bulk_create([
        BlowingRunCostLine(
            run=run, category=l['category'], basis=l['basis'],
            quantity=l['quantity'], rate=l['rate'], amount=l['amount'],
            is_credit=l['is_credit'], note=l['note'],
        )
        for l in lines
    ])

    logger.debug(
        "Recalculated blowing run %s cost: blowing/bottle=%s total/bottle=%s",
        run.id, values['blowing_cost_per_bottle'], values['total_per_bottle_cost'],
    )
