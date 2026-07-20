"""
Cost auto-computation for a BlowingRun.

Formulas reverse-engineered from FactoryFlow/docs.local/Linear.xlsx (sheet
"Linear Data"). All rates come from the run's snapshotted BlowingRateConfig, so
recomputation is deterministic and independent of the live rate card.

Verified against row 1 (40g preform, 58 boxes, 34,959 pcs, 98 rejects,
2 operators, 2 contract + 8 own labour, 1379.77 total units):
    operator_cost   = 2 * 900              = 1800
    labour_cost     = 10 * 600             = 6000
    electricity     = 1379.77 * 6.95       = 9589.4
    wastage         = 98 * 0.040 * 159     = 623.28
    total_cost                             = 18012.68
    scrap_bottle    = 98 * 1.8             = 176.4
    net_cost        = 18012.68 - (176.4 + 1232.5 carton) = 16603.78
    blowing/bottle  = 16603.78 / 34959     = 0.4749
    per_bottle_cost = 0.4749 + 0.2         = 0.6749
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def _dec(v):
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


def compute_run_cost(run) -> dict:
    """Pure cost computation (no DB). Returns the BlowingRunCost field values.

    Reads only plain attributes off ``run`` (and ``run.preform_spec.gram``), so it
    is unit-testable against a lightweight stand-in object.
    """
    operator_cost = _dec(run.operator_count) * _dec(run.operator_rate_per_day)
    labour_cost = (
        _dec(run.contract_labour_count + run.own_labour_count)
        * _dec(run.labour_rate_per_day)
    )
    electricity_cost = _dec(run.total_units) * _dec(run.electricity_rate_per_unit)

    # Wastage = rejected preforms scrapped, valued at preform rate:
    # rejection_pcs * (preform grams / 1000) kg * preform_rate_per_kg
    gram = _dec(run.preform_spec.gram)
    wastage_cost = _dec(run.rejection_pcs) * (gram / Decimal('1000')) * _dec(run.preform_rate_per_kg)

    total_cost = operator_cost + labour_cost + wastage_cost + electricity_cost

    scrap_bottle_value = _dec(run.rejection_pcs) * _dec(run.scrap_rate_per_bottle)
    scrap_total = scrap_bottle_value + _dec(run.scrap_carton_value)
    net_cost = total_cost - scrap_total

    produced = _dec(run.total_counter_production)
    blowing_cost_per_bottle = (net_cost / produced) if produced > 0 else Decimal('0')
    packing_cost = produced * _dec(run.packing_rate_per_bottle)
    per_bottle_cost = blowing_cost_per_bottle + _dec(run.packing_rate_per_bottle)

    # --- fully-loaded make cost (Phase 1) -------------------------------
    # Basis = good bottles, so rejection correctly inflates unit cost.
    good = int(run.total_counter_production or 0) - int(run.rejection_pcs or 0)
    good = good if good > 0 else 0
    good_d = Decimal(good)

    # Preform material for ALL preforms consumed (includes rejected preforms —
    # this replaces the separate wastage line in the fully-loaded model, no
    # double count). Stretch-blow molding: material = preform weight, no parison.
    preform_cost = (_dec(run.preform_used_g) / Decimal('1000')) * _dec(run.preform_rate_per_kg)

    # Mould amortization (wear-based, per good bottle)
    mould_life = int(run.mould_life_bottles or 0)
    mould_per_bottle = (_dec(run.mould_cost) / Decimal(mould_life)) if mould_life > 0 else Decimal('0')
    mould_amortization = good_d * mould_per_bottle

    packing_full = good_d * _dec(run.packing_rate_per_bottle)

    # Fixed per-day costs (absorbed over the day's good output)
    machine_depreciation = _dec(run.machine_depreciation_per_day)
    maintenance_cost = _dec(run.maintenance_per_day)
    overhead_cost = _dec(run.factory_overhead_per_day)
    qa_cost = _dec(run.qa_cost_per_day)

    # Variable = scales with bottles; Fixed = per day regardless of volume.
    variable_cost_total = preform_cost + electricity_cost + mould_amortization + packing_full
    fixed_cost_total = (
        operator_cost + labour_cost + machine_depreciation
        + maintenance_cost + overhead_cost + qa_cost
    )
    fully_loaded_cost = variable_cost_total + fixed_cost_total - scrap_total

    variable_cost_per_bottle = (variable_cost_total / good_d) if good > 0 else Decimal('0')
    make_cost_per_bottle = (fully_loaded_cost / good_d) if good > 0 else Decimal('0')

    return {
        'operator_cost': operator_cost,
        'labour_cost': labour_cost,
        'wastage_cost': wastage_cost,
        'electricity_cost': electricity_cost,
        'total_cost': total_cost,
        'scrap_bottle_value': scrap_bottle_value,
        'scrap_total': scrap_total,
        'net_cost': net_cost,
        'blowing_cost_per_bottle': blowing_cost_per_bottle,
        'packing_cost': packing_cost,
        'per_bottle_cost': per_bottle_cost,
        # fully-loaded
        'good_bottles': good,
        'preform_cost': preform_cost,
        'mould_amortization': mould_amortization,
        'machine_depreciation': machine_depreciation,
        'maintenance_cost': maintenance_cost,
        'overhead_cost': overhead_cost,
        'qa_cost': qa_cost,
        'variable_cost_total': variable_cost_total,
        'fixed_cost_total': fixed_cost_total,
        'fully_loaded_cost': fully_loaded_cost,
        'variable_cost_per_bottle': variable_cost_per_bottle,
        'make_cost_per_bottle': make_cost_per_bottle,
    }


def recalculate_run_cost(run) -> None:
    """Recompute and persist the BlowingRunCost for a run."""
    from blowing.models import BlowingRunCost

    values = compute_run_cost(run)
    obj, _ = BlowingRunCost.objects.update_or_create(run=run, defaults=values)
    # Refresh the cached reverse relation so a run object serialized right after
    # this call reflects the new cost (not a stale select_related snapshot).
    run.cost_summary = obj
    logger.debug(
        "Recalculated blowing run %s cost: net=%s per_bottle=%s",
        run.id, values['net_cost'], values['per_bottle_cost'],
    )
