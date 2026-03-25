import logging
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum, Count, F, Value, CharField
from django.db.models.functions import TruncMonth, ExtractMonth

from django.db.models import Avg, Max, Min, DecimalField
from django.db.models.functions import TruncDate

from ..models import (
    ProductionRun, RunStatus,
    ResourceElectricity, ResourceWater, ResourceGas, ResourceCompressedAir,
    ResourceLabour, ResourceMachineCost, ResourceOverhead,
    ProductionRunCost, WasteLog, ProductionMaterialUsage,
    MachineBreakdown, Machine,
)

logger = logging.getLogger(__name__)

MONTH_NAMES = [
    '', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


class ReportService:
    def __init__(self, company):
        self.company = company

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _base_runs_qs(self, date_from=None, date_to=None, line_id=None, status='COMPLETED'):
        qs = ProductionRun.objects.filter(company=self.company)
        if status:
            qs = qs.filter(status=status)
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if line_id:
            qs = qs.filter(line_id=line_id)
        return qs

    def _run_ids(self, qs):
        return list(qs.values_list('id', flat=True))

    # ------------------------------------------------------------------
    # 1. Day-wise Resource Consumption
    # ------------------------------------------------------------------

    def get_daywise_resource_consumption(self, date_from=None, date_to=None, line_id=None):
        runs_qs = self._base_runs_qs(date_from, date_to, line_id, status=None)
        run_ids = self._run_ids(runs_qs)

        if not run_ids:
            return {'daily_data': [], 'summary': {
                'total_days': 0, 'total_production': 0,
                'grand_total_cost': 0, 'avg_cost_per_case': 0,
            }}

        # Production per date
        prod_by_date = dict(
            runs_qs.values('date').annotate(total=Sum('total_production'))
            .values_list('date', 'total')
        )

        # Aggregate each resource by production_run__date
        def _agg(model, qty_field, cost_field='total_cost'):
            return dict(
                model.objects.filter(production_run_id__in=run_ids)
                .values('production_run__date')
                .annotate(total_qty=Sum(qty_field), total_cost=Sum(cost_field))
                .values_list('production_run__date', 'total_qty', 'total_cost')
                .values_list('production_run__date')
                .annotate(total_qty=Sum(qty_field), total_cost=Sum(cost_field))
            )

        # Build per-resource aggregations
        def _resource_agg(model, qty_field, cost_field='total_cost'):
            rows = (
                model.objects.filter(production_run_id__in=run_ids)
                .values('production_run__date')
                .annotate(total_qty=Sum(qty_field), total_cost=Sum(cost_field))
            )
            result = {}
            for r in rows:
                result[r['production_run__date']] = {
                    'qty': float(r['total_qty'] or 0),
                    'cost': float(r['total_cost'] or 0),
                }
            return result

        elec = _resource_agg(ResourceElectricity, 'units_consumed')
        water = _resource_agg(ResourceWater, 'volume_consumed')
        gas = _resource_agg(ResourceGas, 'qty_consumed')
        air = _resource_agg(ResourceCompressedAir, 'units_consumed')
        labour = _resource_agg(ResourceLabour, 'hours_worked')
        # For labour, also sum worker_count × hours_worked as labour_hours
        labour_hrs = {}
        for row in (ResourceLabour.objects.filter(production_run_id__in=run_ids)
                     .values('production_run__date')
                     .annotate(total_hrs=Sum('hours_worked'))):
            labour_hrs[row['production_run__date']] = float(row['total_hrs'] or 0)

        waste_by_date = {}
        for row in (WasteLog.objects.filter(production_run_id__in=run_ids)
                     .values('production_run__date')
                     .annotate(total_qty=Sum('wastage_qty'))):
            waste_by_date[row['production_run__date']] = float(row['total_qty'] or 0)

        # Collect all dates
        all_dates = sorted(set(
            list(prod_by_date.keys()) +
            list(elec.keys()) + list(water.keys()) + list(gas.keys()) +
            list(air.keys()) + list(labour.keys()) + list(waste_by_date.keys())
        ))

        daily_data = []
        grand_total_cost = Decimal(0)
        grand_total_production = Decimal(0)

        for d in all_dates:
            production = float(prod_by_date.get(d) or 0)
            e = elec.get(d, {'qty': 0, 'cost': 0})
            w = water.get(d, {'qty': 0, 'cost': 0})
            g = gas.get(d, {'qty': 0, 'cost': 0})
            a = air.get(d, {'qty': 0, 'cost': 0})
            lb = labour.get(d, {'qty': 0, 'cost': 0})
            wst = waste_by_date.get(d, 0)

            total_cost = e['cost'] + w['cost'] + g['cost'] + a['cost'] + lb['cost']
            cost_per_case = total_cost / production if production else 0

            daily_data.append({
                'date': str(d),
                'total_production': production,
                'electricity_units': e['qty'],
                'electricity_cost': round(e['cost'], 2),
                'water_volume': w['qty'],
                'water_cost': round(w['cost'], 2),
                'gas_units': g['qty'],
                'gas_cost': round(g['cost'], 2),
                'compressed_air_units': a['qty'],
                'compressed_air_cost': round(a['cost'], 2),
                'labour_hours': labour_hrs.get(d, 0),
                'labour_cost': round(lb['cost'], 2),
                'waste_qty': wst,
                'total_resource_cost': round(total_cost, 2),
                'cost_per_case': round(cost_per_case, 2),
            })
            grand_total_cost += Decimal(str(total_cost))
            grand_total_production += Decimal(str(production))

        avg_cost = float(grand_total_cost / grand_total_production) if grand_total_production else 0

        return {
            'daily_data': daily_data,
            'summary': {
                'total_days': len(daily_data),
                'total_production': float(grand_total_production),
                'grand_total_cost': round(float(grand_total_cost), 2),
                'avg_cost_per_case': round(avg_cost, 2),
            },
        }

    # ------------------------------------------------------------------
    # 2. Monthly Summary
    # ------------------------------------------------------------------

    def get_monthly_summary(self, year, line_id=None):
        runs_qs = self._base_runs_qs(line_id=line_id).filter(date__year=year)

        # Monthly production aggregation
        monthly_agg = (
            runs_qs
            .annotate(month=ExtractMonth('date'))
            .values('month')
            .annotate(
                total_runs=Count('id'),
                total_production=Sum('total_production'),
                total_breakdown=Sum('total_breakdown_time'),
            )
            .order_by('month')
        )
        monthly_map = {r['month']: r for r in monthly_agg}

        # Monthly cost aggregation
        cost_qs = ProductionRunCost.objects.filter(
            production_run__company=self.company,
            production_run__status='COMPLETED',
            production_run__date__year=year,
        )
        if line_id:
            cost_qs = cost_qs.filter(production_run__line_id=line_id)

        monthly_costs = (
            cost_qs
            .annotate(month=ExtractMonth('production_run__date'))
            .values('month')
            .annotate(
                total_cost=Sum('total_cost'),
                electricity_cost=Sum('electricity_cost'),
                water_cost=Sum('water_cost'),
                gas_cost=Sum('gas_cost'),
                compressed_air_cost=Sum('compressed_air_cost'),
                labour_cost=Sum('labour_cost'),
                machine_cost=Sum('machine_cost'),
                overhead_cost=Sum('overhead_cost'),
            )
        )
        cost_map = {r['month']: r for r in monthly_costs}

        # Monthly waste
        waste_qs = WasteLog.objects.filter(
            production_run__company=self.company,
            production_run__status='COMPLETED',
            production_run__date__year=year,
        )
        if line_id:
            waste_qs = waste_qs.filter(production_run__line_id=line_id)

        monthly_waste = (
            waste_qs
            .annotate(month=ExtractMonth('production_run__date'))
            .values('month')
            .annotate(total_waste=Sum('wastage_qty'))
        )
        waste_map = {r['month']: float(r['total_waste'] or 0) for r in monthly_waste}

        # OEE per month — compute from per-run data
        oee_runs = runs_qs.values('id', 'date', 'total_production', 'total_breakdown_time',
                                   'rated_speed', 'rejected_qty')
        monthly_oees = defaultdict(list)
        for run in oee_runs:
            m = run['date'].month
            available = 720
            breakdown = float(run['total_breakdown_time'] or 0)
            operating = max(0, available - breakdown)
            availability = (operating / available * 100) if available else 0

            rated = float(run['rated_speed'] or 0)
            total_prod = float(run['total_production'] or 0)
            actual_speed = (total_prod / operating) if operating > 0 else 0
            performance = min((actual_speed / rated * 100) if rated else 0, 100)

            rejected = float(run['rejected_qty'] or 0)
            quality = ((total_prod - rejected) / total_prod * 100) if total_prod > 0 else 100
            oee = (availability * performance * quality) / 10000
            monthly_oees[m].append(oee)

        months = []
        annual_runs = 0
        annual_production = 0.0
        annual_cost = 0.0
        annual_oees = []

        for m in range(1, 13):
            prod = monthly_map.get(m, {})
            cost = cost_map.get(m, {})
            total_runs = prod.get('total_runs', 0)
            total_production = float(prod.get('total_production') or 0)
            total_cost = float(cost.get('total_cost') or 0)
            cost_per_unit = total_cost / total_production if total_production else 0

            oee_values = monthly_oees.get(m, [])
            avg_oee = sum(oee_values) / len(oee_values) if oee_values else 0

            months.append({
                'month': m,
                'month_name': MONTH_NAMES[m],
                'total_runs': total_runs,
                'total_production': total_production,
                'avg_oee': round(avg_oee, 1),
                'total_cost': round(total_cost, 2),
                'cost_per_unit': round(cost_per_unit, 2),
                'total_waste': waste_map.get(m, 0),
                'electricity_cost': round(float(cost.get('electricity_cost') or 0), 2),
                'water_cost': round(float(cost.get('water_cost') or 0), 2),
                'gas_cost': round(float(cost.get('gas_cost') or 0), 2),
                'compressed_air_cost': round(float(cost.get('compressed_air_cost') or 0), 2),
                'labour_cost': round(float(cost.get('labour_cost') or 0), 2),
                'machine_cost': round(float(cost.get('machine_cost') or 0), 2),
                'overhead_cost': round(float(cost.get('overhead_cost') or 0), 2),
                'total_breakdown_minutes': float(prod.get('total_breakdown') or 0),
            })

            annual_runs += total_runs
            annual_production += total_production
            annual_cost += total_cost
            annual_oees.extend(oee_values)

        return {
            'year': year,
            'months': months,
            'annual_summary': {
                'total_runs': annual_runs,
                'total_production': annual_production,
                'avg_oee': round(sum(annual_oees) / len(annual_oees), 1) if annual_oees else 0,
                'grand_total_cost': round(annual_cost, 2),
            },
        }

    # ------------------------------------------------------------------
    # 3. Plan vs Production
    # ------------------------------------------------------------------

    def get_plan_vs_production(self, company_code, date_from=None, date_to=None):
        runs_qs = self._base_runs_qs(date_from, date_to)
        runs_qs = runs_qs.exclude(sap_doc_entry__isnull=True).exclude(sap_doc_entry=0)

        # Group actual production by SAP doc entry
        actual_by_doc = (
            runs_qs
            .values('sap_doc_entry')
            .annotate(
                actual_production=Sum('total_production'),
                run_count=Count('id'),
            )
        )
        if not actual_by_doc:
            return {
                'items': [],
                'summary': {
                    'total_orders': 0, 'avg_achievement_pct': 0,
                    'total_planned': 0, 'total_actual': 0,
                },
            }

        doc_entries = [r['sap_doc_entry'] for r in actual_by_doc]
        actual_map = {r['sap_doc_entry']: float(r['actual_production'] or 0) for r in actual_by_doc}

        # Batch fetch from SAP
        try:
            from .sap_reader import ProductionOrderReader
            reader = ProductionOrderReader(company_code)
            sap_data = reader.get_production_orders_by_entries(doc_entries)
        except Exception as e:
            logger.warning(f"SAP fetch failed, using local data only: {e}")
            sap_data = {}

        items = []
        total_planned = 0
        total_actual = 0

        for doc_entry in doc_entries:
            actual = actual_map.get(doc_entry, 0)
            sap = sap_data.get(doc_entry, {})
            planned = float(sap.get('PlannedQty', 0))
            variance = actual - planned
            achievement = (actual / planned * 100) if planned else 0

            if achievement >= 95:
                item_status = 'exceeded' if achievement > 105 else 'on_track'
            elif achievement >= 80:
                item_status = 'on_track'
            else:
                item_status = 'behind'

            items.append({
                'sap_doc_entry': doc_entry,
                'sap_doc_num': sap.get('DocNum', ''),
                'item_code': sap.get('ItemCode', ''),
                'product_name': sap.get('ProdName', ''),
                'planned_qty': planned,
                'actual_production': actual,
                'variance': round(variance, 2),
                'achievement_pct': round(achievement, 1),
                'status': item_status,
            })
            total_planned += planned
            total_actual += actual

        avg_achievement = (total_actual / total_planned * 100) if total_planned else 0

        return {
            'items': items,
            'summary': {
                'total_orders': len(items),
                'avg_achievement_pct': round(avg_achievement, 1),
                'total_planned': total_planned,
                'total_actual': total_actual,
            },
        }

    # ------------------------------------------------------------------
    # 4. Procurement vs Planned
    # ------------------------------------------------------------------

    def get_procurement_vs_planned(self, company_code, sap_doc_entry):
        # Get BOM components from SAP
        try:
            from .sap_reader import ProductionOrderReader
            reader = ProductionOrderReader(company_code)
            order_detail = reader.get_production_order_detail(sap_doc_entry)
            components = order_detail.get('components', [])
            header = order_detail.get('header', {})
        except Exception as e:
            logger.error(f"Failed to fetch SAP order detail: {e}")
            return {
                'sap_doc_entry': sap_doc_entry,
                'sap_doc_num': '',
                'product_name': '',
                'items': [],
                'summary': {'total_items': 0, 'fully_fulfilled': 0, 'shortage_items': 0},
            }

        # Get actual consumption from local production runs
        runs_qs = ProductionRun.objects.filter(
            company=self.company, sap_doc_entry=sap_doc_entry,
        )
        run_ids = self._run_ids(runs_qs)

        consumption_qs = (
            ProductionMaterialUsage.objects.filter(production_run_id__in=run_ids)
            .values('material_code')
            .annotate(
                consumed_qty=Sum('issued_qty'),
                wastage_qty=Sum('wastage_qty'),
            )
        )
        consumption_map = {r['material_code']: r for r in consumption_qs}

        # Get procurement data (POItemReceipt)
        procurement_map = {}
        try:
            from raw_material_gatein.models import POItemReceipt
            procurement_qs = (
                POItemReceipt.objects.filter(
                    po_receipt__vehicle_entry__company=self.company,
                )
                .values('po_item_code')
                .annotate(
                    procured_qty=Sum('received_qty'),
                    accepted_qty=Sum('accepted_qty'),
                )
            )
            procurement_map = {r['po_item_code']: r for r in procurement_qs}
        except Exception as e:
            logger.warning(f"Could not fetch procurement data: {e}")

        items = []
        fully_fulfilled = 0
        shortage_items = 0

        for comp in components:
            item_code = comp.get('ItemCode', '')
            item_name = comp.get('ItemName', '')
            uom = comp.get('UomCode', '')
            bom_planned = float(comp.get('PlannedQty', 0))

            proc = procurement_map.get(item_code, {})
            procured = float(proc.get('procured_qty', 0))

            cons = consumption_map.get(item_code, {})
            consumed = float(cons.get('consumed_qty', 0))

            procurement_pct = (procured / bom_planned * 100) if bom_planned else 0
            excess_shortage = procured - bom_planned
            consumption_pct = (consumed / bom_planned * 100) if bom_planned else 0

            if procurement_pct >= 100:
                item_status = 'fulfilled'
                if procurement_pct > 110:
                    item_status = 'excess'
                fully_fulfilled += 1
            else:
                item_status = 'shortage'
                shortage_items += 1

            items.append({
                'item_code': item_code,
                'item_name': item_name,
                'uom': uom,
                'bom_planned_qty': bom_planned,
                'procured_qty': procured,
                'consumed_qty': consumed,
                'procurement_fulfillment_pct': round(procurement_pct, 1),
                'excess_shortage': round(excess_shortage, 2),
                'consumption_vs_planned_pct': round(consumption_pct, 1),
                'status': item_status,
            })

        return {
            'sap_doc_entry': sap_doc_entry,
            'sap_doc_num': header.get('DocNum', ''),
            'product_name': header.get('ProdName', ''),
            'items': items,
            'summary': {
                'total_items': len(items),
                'fully_fulfilled': fully_fulfilled,
                'shortage_items': shortage_items,
            },
        }

    # ------------------------------------------------------------------
    # 5. OEE Trend Report (Phase 2)
    # ------------------------------------------------------------------

    def get_oee_trend(self, date_from=None, date_to=None, line_id=None, group_by='daily'):
        runs_qs = self._base_runs_qs(date_from, date_to, line_id)
        runs = runs_qs.select_related('line').order_by('date')

        # Per-run OEE
        run_oees = []
        for run in runs:
            available = 720
            breakdown = float(run.total_breakdown_time or 0)
            operating = max(0, available - breakdown)
            availability = (operating / available * 100) if available else 0

            rated = float(run.rated_speed or 0)
            total_prod = float(run.total_production or 0)
            actual_speed = (total_prod / operating) if operating > 0 else 0
            performance = min((actual_speed / rated * 100) if rated else 0, 100)

            rejected = float(run.rejected_qty or 0)
            quality = ((total_prod - rejected) / total_prod * 100) if total_prod > 0 else 100
            oee = (availability * performance * quality) / 10000

            run_oees.append({
                'run_id': run.id,
                'run_number': run.run_number,
                'date': str(run.date),
                'line': run.line.name,
                'line_id': run.line_id,
                'availability': round(availability, 1),
                'performance': round(performance, 1),
                'quality': round(quality, 1),
                'oee': round(oee, 1),
            })

        # Group into trend buckets
        if group_by == 'monthly':
            buckets = defaultdict(list)
            for r in run_oees:
                key = r['date'][:7]  # YYYY-MM
                buckets[key].append(r)
        elif group_by == 'weekly':
            from datetime import datetime
            buckets = defaultdict(list)
            for r in run_oees:
                dt = datetime.strptime(r['date'], '%Y-%m-%d')
                iso = dt.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
                buckets[key].append(r)
        else:  # daily
            buckets = defaultdict(list)
            for r in run_oees:
                buckets[r['date']].append(r)

        trend = []
        for period in sorted(buckets.keys()):
            items = buckets[period]
            avg_oee = sum(i['oee'] for i in items) / len(items)
            avg_avail = sum(i['availability'] for i in items) / len(items)
            avg_perf = sum(i['performance'] for i in items) / len(items)
            avg_qual = sum(i['quality'] for i in items) / len(items)
            trend.append({
                'period': period,
                'avg_oee': round(avg_oee, 1),
                'avg_availability': round(avg_avail, 1),
                'avg_performance': round(avg_perf, 1),
                'avg_quality': round(avg_qual, 1),
                'run_count': len(items),
            })

        # Per-line comparison
        line_map = defaultdict(list)
        for r in run_oees:
            line_map[r['line']].append(r['oee'])

        by_line = []
        for line_name in sorted(line_map.keys()):
            vals = line_map[line_name]
            by_line.append({
                'line': line_name,
                'avg_oee': round(sum(vals) / len(vals), 1),
                'min_oee': round(min(vals), 1),
                'max_oee': round(max(vals), 1),
                'run_count': len(vals),
            })

        overall = sum(r['oee'] for r in run_oees) / len(run_oees) if run_oees else 0

        return {
            'trend': trend,
            'by_line': by_line,
            'per_run': run_oees,
            'summary': {
                'total_runs': len(run_oees),
                'avg_oee': round(overall, 1),
                'group_by': group_by,
            },
        }

    # ------------------------------------------------------------------
    # 6. Downtime Pareto Analysis (Phase 2)
    # ------------------------------------------------------------------

    def get_downtime_pareto(self, date_from=None, date_to=None, line_id=None):
        runs_qs = self._base_runs_qs(date_from, date_to, line_id, status=None)
        run_ids = self._run_ids(runs_qs)

        bd_qs = MachineBreakdown.objects.filter(
            production_run_id__in=run_ids
        ).select_related('machine', 'breakdown_category')

        # --- By Category (Pareto) ---
        by_category = list(
            bd_qs.values('breakdown_category__name')
            .annotate(count=Count('id'), total_minutes=Sum('breakdown_minutes'))
            .order_by('-total_minutes')
        )
        grand_total_mins = sum(r['total_minutes'] or 0 for r in by_category)
        cumulative = 0
        pareto = []
        for r in by_category:
            mins = r['total_minutes'] or 0
            cumulative += mins
            pareto.append({
                'category': r['breakdown_category__name'] or 'Uncategorized',
                'count': r['count'],
                'total_minutes': mins,
                'percentage': round(mins / grand_total_mins * 100, 1) if grand_total_mins else 0,
                'cumulative_pct': round(cumulative / grand_total_mins * 100, 1) if grand_total_mins else 0,
            })

        # --- By Machine ---
        by_machine = list(
            bd_qs.values('machine__name')
            .annotate(count=Count('id'), total_minutes=Sum('breakdown_minutes'))
            .order_by('-total_minutes')
        )
        machine_data = [{
            'machine': r['machine__name'] or 'Unknown',
            'count': r['count'],
            'total_minutes': r['total_minutes'] or 0,
        } for r in by_machine]

        # --- MTBF & MTTR ---
        # MTBF = total operating time / number of breakdowns
        # MTTR = total breakdown time / number of breakdowns
        total_running = float(
            runs_qs.aggregate(t=Sum('total_running_minutes'))['t'] or 0
        )
        total_breakdowns = bd_qs.count()
        total_breakdown_mins = float(
            bd_qs.aggregate(t=Sum('breakdown_minutes'))['t'] or 0
        )
        mtbf = total_running / total_breakdowns if total_breakdowns else 0
        mttr = total_breakdown_mins / total_breakdowns if total_breakdowns else 0

        # --- Daily trend ---
        daily_trend = list(
            bd_qs
            .annotate(bd_date=TruncDate('start_time'))
            .values('bd_date')
            .annotate(count=Count('id'), total_minutes=Sum('breakdown_minutes'))
            .order_by('bd_date')
        )
        trend = [{
            'date': str(r['bd_date']),
            'count': r['count'],
            'total_minutes': r['total_minutes'] or 0,
        } for r in daily_trend]

        return {
            'pareto': pareto,
            'by_machine': machine_data,
            'trend': trend,
            'summary': {
                'total_breakdowns': total_breakdowns,
                'total_breakdown_minutes': round(total_breakdown_mins, 1),
                'total_running_minutes': round(total_running, 1),
                'mtbf_minutes': round(mtbf, 1),
                'mttr_minutes': round(mttr, 1),
            },
        }

    # ------------------------------------------------------------------
    # 7. Cost Analysis Report (Phase 2)
    # ------------------------------------------------------------------

    def get_cost_analysis(self, date_from=None, date_to=None, line_id=None):
        runs_qs = self._base_runs_qs(date_from, date_to, line_id)
        run_ids = self._run_ids(runs_qs)

        cost_qs = ProductionRunCost.objects.filter(
            production_run_id__in=run_ids
        ).select_related('production_run', 'production_run__line')

        if not cost_qs.exists():
            return {
                'per_run': [], 'trend': [], 'by_line': [],
                'cost_distribution': {},
                'summary': {
                    'total_cost': 0, 'avg_per_unit': 0,
                    'total_production': 0, 'run_count': 0,
                },
            }

        # Per-run cost data
        per_run = []
        for c in cost_qs.order_by('production_run__date'):
            run = c.production_run
            per_run.append({
                'run_id': run.id,
                'run_number': run.run_number,
                'date': str(run.date),
                'line': run.line.name,
                'product': run.product,
                'produced_qty': float(c.produced_qty or 0),
                'raw_material_cost': float(c.raw_material_cost or 0),
                'labour_cost': float(c.labour_cost or 0),
                'machine_cost': float(c.machine_cost or 0),
                'electricity_cost': float(c.electricity_cost or 0),
                'water_cost': float(c.water_cost or 0),
                'gas_cost': float(c.gas_cost or 0),
                'compressed_air_cost': float(c.compressed_air_cost or 0),
                'overhead_cost': float(c.overhead_cost or 0),
                'total_cost': float(c.total_cost or 0),
                'per_unit_cost': float(c.per_unit_cost or 0),
            })

        # Daily cost trend
        daily_buckets = defaultdict(lambda: {'total_cost': 0, 'production': 0, 'runs': 0})
        for r in per_run:
            d = r['date']
            daily_buckets[d]['total_cost'] += r['total_cost']
            daily_buckets[d]['production'] += r['produced_qty']
            daily_buckets[d]['runs'] += 1

        trend = []
        for date in sorted(daily_buckets.keys()):
            b = daily_buckets[date]
            trend.append({
                'date': date,
                'total_cost': round(b['total_cost'], 2),
                'production': b['production'],
                'per_unit_cost': round(b['total_cost'] / b['production'], 2) if b['production'] else 0,
                'run_count': b['runs'],
            })

        # By line
        line_buckets = defaultdict(lambda: {'total_cost': 0, 'production': 0, 'runs': 0})
        for r in per_run:
            line_buckets[r['line']]['total_cost'] += r['total_cost']
            line_buckets[r['line']]['production'] += r['produced_qty']
            line_buckets[r['line']]['runs'] += 1

        by_line = []
        for line in sorted(line_buckets.keys()):
            b = line_buckets[line]
            by_line.append({
                'line': line,
                'total_cost': round(b['total_cost'], 2),
                'production': b['production'],
                'avg_per_unit': round(b['total_cost'] / b['production'], 2) if b['production'] else 0,
                'run_count': b['runs'],
            })

        # Cost distribution (aggregate percentages)
        agg = cost_qs.aggregate(
            raw_material=Sum('raw_material_cost'),
            labour=Sum('labour_cost'),
            machine=Sum('machine_cost'),
            electricity=Sum('electricity_cost'),
            water=Sum('water_cost'),
            gas=Sum('gas_cost'),
            compressed_air=Sum('compressed_air_cost'),
            overhead=Sum('overhead_cost'),
            total=Sum('total_cost'),
            total_production=Sum('produced_qty'),
        )
        total = float(agg['total'] or 0)
        distribution = {}
        for key in ['raw_material', 'labour', 'machine', 'electricity', 'water', 'gas', 'compressed_air', 'overhead']:
            val = float(agg[key] or 0)
            distribution[key] = {
                'amount': round(val, 2),
                'percentage': round(val / total * 100, 1) if total else 0,
            }

        return {
            'per_run': per_run,
            'trend': trend,
            'by_line': by_line,
            'cost_distribution': distribution,
            'summary': {
                'total_cost': round(total, 2),
                'avg_per_unit': round(total / float(agg['total_production'] or 1), 2),
                'total_production': float(agg['total_production'] or 0),
                'run_count': len(per_run),
            },
        }

    # ------------------------------------------------------------------
    # 8. Waste & Scrap Report (Phase 2)
    # ------------------------------------------------------------------

    def get_waste_trend(self, date_from=None, date_to=None, line_id=None):
        runs_qs = self._base_runs_qs(date_from, date_to, line_id, status=None)
        run_ids = self._run_ids(runs_qs)

        waste_qs = WasteLog.objects.filter(production_run_id__in=run_ids)

        if not waste_qs.exists():
            return {
                'by_material': [], 'by_reason': [], 'trend': [],
                'by_approval_status': [],
                'summary': {
                    'total_waste_qty': 0, 'total_waste_logs': 0,
                    'unique_materials': 0, 'approval_rate': 0,
                },
            }

        # By material
        by_material = list(
            waste_qs.values('material_name', 'uom')
            .annotate(total_qty=Sum('wastage_qty'), count=Count('id'))
            .order_by('-total_qty')
        )
        material_data = [{
            'material_name': r['material_name'],
            'uom': r['uom'],
            'total_qty': float(r['total_qty'] or 0),
            'count': r['count'],
        } for r in by_material]

        # By reason
        by_reason = list(
            waste_qs.values('reason')
            .annotate(total_qty=Sum('wastage_qty'), count=Count('id'))
            .order_by('-total_qty')
        )
        reason_data = [{
            'reason': r['reason'] or 'Not specified',
            'total_qty': float(r['total_qty'] or 0),
            'count': r['count'],
        } for r in by_reason]

        # Daily trend
        daily_trend = list(
            waste_qs
            .values('production_run__date')
            .annotate(total_qty=Sum('wastage_qty'), count=Count('id'))
            .order_by('production_run__date')
        )
        trend = [{
            'date': str(r['production_run__date']),
            'total_qty': float(r['total_qty'] or 0),
            'count': r['count'],
        } for r in daily_trend]

        # By approval status
        by_status = list(
            waste_qs.values('wastage_approval_status')
            .annotate(count=Count('id'), total_qty=Sum('wastage_qty'))
        )
        status_data = [{
            'status': r['wastage_approval_status'],
            'count': r['count'],
            'total_qty': float(r['total_qty'] or 0),
        } for r in by_status]

        total_logs = waste_qs.count()
        fully_approved = waste_qs.filter(wastage_approval_status='FULLY_APPROVED').count()
        total_waste = float(waste_qs.aggregate(t=Sum('wastage_qty'))['t'] or 0)

        # Waste vs production ratio
        total_prod = float(runs_qs.aggregate(t=Sum('total_production'))['t'] or 0)
        waste_pct = (total_waste / total_prod * 100) if total_prod else 0

        return {
            'by_material': material_data,
            'by_reason': reason_data,
            'trend': trend,
            'by_approval_status': status_data,
            'summary': {
                'total_waste_qty': round(total_waste, 2),
                'total_waste_logs': total_logs,
                'unique_materials': len(material_data),
                'approval_rate': round(fully_approved / total_logs * 100, 1) if total_logs else 0,
                'waste_vs_production_pct': round(waste_pct, 2),
                'total_production': total_prod,
            },
        }
