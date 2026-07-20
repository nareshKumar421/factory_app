"""Daily / monthly reporting and aggregation for the blowing module."""
import calendar
from decimal import Decimal

from django.db.models import Sum, Avg, Count

from ..models import BlowingRun, BottleBuyPrice


# Sum expressions reused across reports. Keys become the aggregate output names.
_RUN_SUMS = {
    'total_production': Sum('total_counter_production'),
    'total_rejection': Sum('rejection_pcs'),
    'total_preform_g': Sum('preform_used_g'),
    'total_units': Sum('total_units'),
    'operator_cost': Sum('cost_summary__operator_cost'),
    'labour_cost': Sum('cost_summary__labour_cost'),
    'wastage_cost': Sum('cost_summary__wastage_cost'),
    'electricity_cost': Sum('cost_summary__electricity_cost'),
    'total_cost': Sum('cost_summary__total_cost'),
    'scrap_total': Sum('cost_summary__scrap_total'),
    'net_cost': Sum('cost_summary__net_cost'),
    'avg_per_bottle_cost': Avg('cost_summary__per_bottle_cost'),
    'run_count': Count('id'),
}


def _clean(agg: dict) -> dict:
    """Replace None aggregates with 0 for a stable JSON shape."""
    out = {}
    for k, v in agg.items():
        if v is None:
            out[k] = 0
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


class BlowingReportService:
    def __init__(self, company):
        # Accepts a Company instance (mirrors production_execution ReportService).
        self.company = company

    def _base_qs(self):
        return BlowingRun.objects.filter(company=self.company)

    def get_daily_report(self, date) -> dict:
        qs = self._base_qs().filter(date=date)
        totals = _clean(qs.aggregate(**_RUN_SUMS))

        by_machine = [
            {'machine_id': r['machine_id'], 'machine_name': r['machine__name'],
             **_clean({k: r[k] for k in ('total_production', 'total_rejection', 'net_cost')})}
            for r in qs.values('machine_id', 'machine__name').annotate(
                total_production=Sum('total_counter_production'),
                total_rejection=Sum('rejection_pcs'),
                net_cost=Sum('cost_summary__net_cost'),
            )
        ]
        by_preform = [
            {'make': r['preform_spec__make'], 'gram': float(r['preform_spec__gram']),
             **_clean({k: r[k] for k in ('total_production', 'total_rejection', 'net_cost')})}
            for r in qs.values('preform_spec__make', 'preform_spec__gram').annotate(
                total_production=Sum('total_counter_production'),
                total_rejection=Sum('rejection_pcs'),
                net_cost=Sum('cost_summary__net_cost'),
            )
        ]
        return {
            'date': str(date),
            'totals': totals,
            'by_machine': by_machine,
            'by_preform': by_preform,
        }

    def get_variances(self, date_from, date_to) -> dict:
        """
        Per run: actual vs the preform spec's standard (target) for make cost,
        rejection %, and electricity units/bottle. Flags breaches (actual worse
        than standard). Standard costing's control loop.
        """
        runs = (
            self._base_qs()
            .filter(date__gte=date_from, date__lte=date_to)
            .select_related('preform_spec', 'machine', 'cost_summary')
            .order_by('-date', 'run_number')
        )

        def variance(actual, std):
            if std is None:
                return None, None, False
            actual = float(actual or 0)
            std = float(std)
            var = actual - std
            pct = (var / std * 100) if std else None
            # "breach" = actual materially worse (higher) than standard
            breach = std > 0 and actual > std * 1.05
            return var, pct, breach

        rows = []
        breaches = 0
        for r in runs:
            spec = r.preform_spec
            cost = getattr(r, 'cost_summary', None)
            make = float(cost.make_cost_per_bottle) if cost else 0.0
            good = cost.good_bottles if cost else 0
            units_per_bottle = (float(r.total_units) / good) if good > 0 else 0.0

            mv, mp, mb = variance(make, spec.std_make_cost_per_bottle)
            rv, rp, rb = variance(r.rejection_pct, spec.std_reject_pct)
            uv, up, ub = variance(units_per_bottle, spec.std_units_per_bottle)
            any_breach = bool(mb or rb or ub)
            if any_breach:
                breaches += 1

            rows.append({
                'run_id': r.id,
                'date': str(r.date),
                'run_number': r.run_number,
                'machine_name': r.machine.name,
                'preform': f"{spec.make} {float(spec.gram):g}g",
                'make_cost': {'actual': make, 'std': float(spec.std_make_cost_per_bottle) if spec.std_make_cost_per_bottle is not None else None, 'variance': mv, 'variance_pct': mp, 'breach': mb},
                'reject_pct': {'actual': float(r.rejection_pct), 'std': float(spec.std_reject_pct) if spec.std_reject_pct is not None else None, 'variance': rv, 'variance_pct': rp, 'breach': rb},
                'units_per_bottle': {'actual': units_per_bottle, 'std': float(spec.std_units_per_bottle) if spec.std_units_per_bottle is not None else None, 'variance': uv, 'variance_pct': up, 'breach': ub},
                'any_breach': any_breach,
            })

        return {
            'date_from': str(date_from),
            'date_to': str(date_to),
            'rows': rows,
            'summary': {'runs': len(rows), 'breaches': breaches},
        }

    def get_make_vs_buy(self, date_from, date_to, preform_spec_id=None) -> dict:
        """
        Per bottle size over a period: aggregate the fully-loaded make cost from
        runs, compare against the effective landed buy price, and derive the
        breakeven volume and period savings.
        """
        runs = self._base_qs().filter(date__gte=date_from, date__lte=date_to)
        if preform_spec_id:
            runs = runs.filter(preform_spec_id=preform_spec_id)

        agg = (
            runs.values('preform_spec_id', 'preform_spec__make', 'preform_spec__gram')
            .annotate(
                good=Sum('cost_summary__good_bottles'),
                fully_loaded=Sum('cost_summary__fully_loaded_cost'),
                variable_total=Sum('cost_summary__variable_cost_total'),
                fixed_total=Sum('cost_summary__fixed_cost_total'),
                preform_total=Sum('cost_summary__preform_cost'),
                electricity_total=Sum('cost_summary__electricity_cost'),
            )
            .order_by('preform_spec__gram')
        )

        rows = []
        totals = {'good': 0, 'make_cost': 0.0, 'buy_cost': 0.0, 'savings': 0.0}
        for a in agg:
            good = a['good'] or 0
            fully = Decimal(str(a['fully_loaded'] or 0))
            var_total = Decimal(str(a['variable_total'] or 0))
            fixed_total = Decimal(str(a['fixed_total'] or 0))
            preform_total = Decimal(str(a['preform_total'] or 0))
            electricity_total = Decimal(str(a['electricity_total'] or 0))
            make_per_bottle = (fully / Decimal(good)) if good > 0 else Decimal('0')
            var_per_bottle = (var_total / Decimal(good)) if good > 0 else Decimal('0')
            preform_pb = (preform_total / Decimal(good)) if good > 0 else Decimal('0')
            electricity_pb = (electricity_total / Decimal(good)) if good > 0 else Decimal('0')

            bp = (
                BottleBuyPrice.objects
                .filter(company=self.company, is_active=True,
                        preform_spec_id=a['preform_spec_id'], effective_from__lte=date_to)
                .order_by('-effective_from')
                .first()
            )
            landed = bp.landed_cost_per_bottle if bp else None

            delta = None            # +ve => make is cheaper
            breakeven = None
            savings = None
            verdict = 'NO_BUY_PRICE'
            if landed is not None:
                delta = landed - make_per_bottle
                contribution = landed - var_per_bottle    # covers fixed cost
                if contribution > 0:
                    breakeven = float(fixed_total / contribution)
                savings = float(delta * Decimal(good))
                verdict = 'MAKE' if delta > 0 else 'BUY'

            row = {
                'preform_spec_id': a['preform_spec_id'],
                'make': a['preform_spec__make'],
                'gram': float(a['preform_spec__gram']),
                'good_bottles': good,
                'make_cost_per_bottle': float(make_per_bottle),
                'variable_cost_per_bottle': float(var_per_bottle),
                'preform_per_bottle': float(preform_pb),
                'electricity_per_bottle': float(electricity_pb),
                'fixed_cost_total': float(fixed_total),
                'buy_landed_per_bottle': float(landed) if landed is not None else None,
                'supplier': bp.supplier_name if bp else '',
                'delta_per_bottle': float(delta) if delta is not None else None,
                'breakeven_bottles': breakeven,
                'period_savings': savings,
                'verdict': verdict,
            }
            rows.append(row)
            totals['good'] += good
            totals['make_cost'] += float(make_per_bottle) * good
            if landed is not None:
                totals['buy_cost'] += float(landed) * good
                totals['savings'] += savings or 0.0

        return {
            'date_from': str(date_from),
            'date_to': str(date_to),
            'rows': rows,
            'totals': {
                'good_bottles': totals['good'],
                'make_cost': round(totals['make_cost'], 2),
                'buy_cost': round(totals['buy_cost'], 2),
                'period_savings': round(totals['savings'], 2),
                'verdict': 'MAKE' if totals['savings'] > 0 else ('BUY' if totals['buy_cost'] else 'NO_BUY_PRICE'),
            },
        }

    def get_monthly_summary(self, year: int, month: int) -> dict:
        last_day = calendar.monthrange(year, month)[1]
        qs = self._base_qs().filter(date__year=year, date__month=month)
        totals = _clean(qs.aggregate(**_RUN_SUMS))

        daywise = [
            {'date': str(r['date']),
             **_clean({k: r[k] for k in (
                 'total_production', 'total_rejection', 'total_units',
                 'net_cost', 'avg_per_bottle_cost')})}
            for r in qs.values('date').annotate(
                total_production=Sum('total_counter_production'),
                total_rejection=Sum('rejection_pcs'),
                total_units=Sum('total_units'),
                net_cost=Sum('cost_summary__net_cost'),
                avg_per_bottle_cost=Avg('cost_summary__per_bottle_cost'),
            ).order_by('date')
        ]
        return {
            'year': year,
            'month': month,
            'days_in_month': last_day,
            'totals': totals,
            'daywise': daywise,
        }
