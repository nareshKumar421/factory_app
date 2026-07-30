"""
Cost-calculator tests, verified against Linear.xlsx row 1 plus the Phase 1
fully-loaded make-cost model.

Uses SimpleTestCase + a lightweight stand-in object so the assertions need no
database (the project has cross-app Postgres-only migrations that prevent a
SQLite test DB, and the shared Postgres box should not be used for tests).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from .services.cost_calculator import compute_run_cost


def _row1_run():
    # 40g Frystal, 34,959 pcs, 98 rejects (=> 34,861 good), 2 operators + 2 contract
    # + 8 own labour, total_units 1379.7705 (electricity meter), carton scrap 1232.5,
    # preform 6.36/bottle. Blowing-side rates come from _row1_rates().
    return SimpleNamespace(
        operator_count=2, contract_labour_count=2, own_labour_count=8,
        machine_units=Decimal('0'), utility_units=Decimal('1379.7705'),
        rejection_pcs=98, preform_rate_per_bottle=Decimal('6.36'),
        scrap_carton_value=Decimal('1232.5'),
        total_counter_production=34959,
    )


def _row1_rates(elec_machine='0'):
    # Cost Master catalog rates: op 900/person-day, labour 600/person-day,
    # utility electricity 6.95/unit, machine electricity flat/day, packing 0.2,
    # scrap 1.8/bottle (credit), industry benchmark 0.50/bottle.
    return {
        'OPERATOR': {'rate': Decimal('900'), 'basis': 'PER_PERSON_DAY', 'is_credit': False},
        'LABOUR': {'rate': Decimal('600'), 'basis': 'PER_PERSON_DAY', 'is_credit': False},
        'ELECTRICITY_MACHINE': {'rate': Decimal(elec_machine), 'basis': 'PER_DAY', 'is_credit': False},
        'ELECTRICITY_UTILITY': {'rate': Decimal('6.95'), 'basis': 'PER_UNIT', 'is_credit': False},
        'PACKING': {'rate': Decimal('0.2'), 'basis': 'PER_BOTTLE', 'is_credit': False},
        'SCRAP_RECOVERY': {'rate': Decimal('1.8'), 'basis': 'PER_BOTTLE', 'is_credit': True},
        'BENCHMARK_BLOWING_PER_BOTTLE': {'rate': Decimal('0.50'), 'basis': 'PER_BOTTLE', 'is_credit': False},
    }


class BlowingCostComponentsTest(SimpleTestCase):
    """Individual blowing-cost components + the two-electricity split."""

    def test_components(self):
        c = compute_run_cost(_row1_run(), rates=_row1_rates())
        self.assertEqual(c['good_bottles'], 34861)
        self.assertEqual(c['operator_cost'], Decimal('1800'))        # 2 × 900
        self.assertEqual(c['labour_cost'], Decimal('6000'))          # 10 × 600
        self.assertEqual(c['wastage_cost'], Decimal('623.28'))       # 98 × 6.36
        self.assertEqual(c['scrap_bottle_value'], Decimal('176.4'))  # 98 × 1.8
        self.assertEqual(c['scrap_total'], Decimal('1408.9'))        # 176.4 + 1232.5 carton
        self.assertAlmostEqual(float(c['electricity_utility_cost']), 1379.7705 * 6.95, places=4)
        self.assertEqual(c['electricity_machine_cost'], Decimal('0'))
        self.assertAlmostEqual(float(c['packing_cost']), 34861 * 0.2, places=2)

    def test_two_electricities_split(self):
        # Both electricities are metered (units × ₹/unit). Machine cost scales
        # with the machine-electricity rate; utility is independent of it.
        run = _row1_run()
        run.machine_units = Decimal('100')
        base = compute_run_cost(run, rates=_row1_rates(elec_machine='0'))
        withmc = compute_run_cost(run, rates=_row1_rates(elec_machine='15'))
        self.assertEqual(withmc['electricity_machine_cost'], Decimal('1500'))  # 100 × 15
        self.assertAlmostEqual(
            float(withmc['electricity_utility_cost']),
            float(base['electricity_utility_cost']), places=6)   # unchanged
        self.assertAlmostEqual(
            float(withmc['blowing_cost'] - base['blowing_cost']), 1500.0, places=2)

    def test_zero_production_no_divide_by_zero(self):
        run = _row1_run()
        run.total_counter_production = 0
        run.rejection_pcs = 0
        c = compute_run_cost(run, rates=_row1_rates())
        self.assertEqual(c['blowing_cost_per_bottle'], Decimal('0'))
        self.assertEqual(c['total_per_bottle_cost'], Decimal('0'))


class BlowingTwoBucketCostTest(SimpleTestCase):
    """Preform + blowing bucket identities, benchmark, and per-bottle totals."""

    def test_identities(self):
        c = compute_run_cost(_row1_run(), rates=_row1_rates())
        good = 34861
        # blowing = operator + labour + electricity + wastage + packing − scrap
        exp_blowing = (1800 + 6000 + 1379.7705 * 6.95 + 623.28 + 34861 * 0.2 - 1408.9)
        self.assertAlmostEqual(float(c['blowing_cost']), exp_blowing, places=2)
        # preform = produced × per-bottle rate
        self.assertAlmostEqual(float(c['preform_cost']), 34959 * 6.36, places=2)
        # fully loaded = preform + blowing
        self.assertAlmostEqual(
            float(c['fully_loaded_cost']), 34959 * 6.36 + exp_blowing, places=2)
        # per-bottle totals
        self.assertAlmostEqual(float(c['blowing_cost_per_bottle']), exp_blowing / good, places=6)
        self.assertAlmostEqual(float(c['preform_cost_per_bottle']), 34959 * 6.36 / good, places=6)
        self.assertAlmostEqual(
            float(c['total_per_bottle_cost']),
            float(c['preform_cost_per_bottle']) + float(c['blowing_cost_per_bottle']), places=6)
        self.assertEqual(c['make_cost_per_bottle'], c['total_per_bottle_cost'])
        # benchmark surfaced
        self.assertEqual(c['benchmark_blowing_per_bottle'], Decimal('0.50'))

    def test_cost_lines_emitted(self):
        c = compute_run_cost(_row1_run(), rates=_row1_rates())
        cats = {l['category'] for l in c['cost_lines']}
        self.assertEqual(cats, {'OPERATOR', 'LABOUR', 'ELECTRICITY_MACHINE',
                                'ELECTRICITY_UTILITY', 'WASTAGE', 'PACKING', 'SCRAP_RECOVERY'})
        scrap = next(l for l in c['cost_lines'] if l['category'] == 'SCRAP_RECOVERY')
        self.assertTrue(scrap['is_credit'])

    def test_market_price_passthrough(self):
        c = compute_run_cost(_row1_run(), rates=_row1_rates(), market_price=Decimal('7.40'))
        self.assertEqual(c['market_price_per_bottle'], Decimal('7.40'))


class MakeVsBuyMathTest(SimpleTestCase):
    """The breakeven identity used by the make-vs-buy report."""

    def test_breakeven(self):
        # fixed 11,600; variable/bottle 6.90; buy landed 7.40 => contribution 0.50
        fixed = Decimal('11600')
        variable_per_bottle = Decimal('6.90')
        buy = Decimal('7.40')
        contribution = buy - variable_per_bottle
        breakeven = fixed / contribution
        self.assertAlmostEqual(float(breakeven), 23200.0, places=1)
        # below breakeven, making a small volume is more expensive than buying
        vol = 10000
        make_total = fixed + variable_per_bottle * vol
        buy_total = buy * vol
        self.assertGreater(float(make_total), float(buy_total))
        # above breakeven, making wins
        vol = 40000
        make_total = fixed + variable_per_bottle * vol
        buy_total = buy * vol
        self.assertLess(float(make_total), float(buy_total))


class BlowingRunLifecycleTest(TestCase):
    """Full run lifecycle: warehouse gate -> approve -> start/stop/breakdown -> complete."""

    def setUp(self):
        from company.models import Company
        from .models import BlowingMachine, PreformSpec, BlowingRateConfig
        from .services import BlowingService
        self.company = Company.objects.create(name='Test Oil', code='TEST_OIL')
        self.svc = BlowingService('TEST_OIL')
        self.machine = BlowingMachine.objects.create(
            company=self.company, name='M1', sap_warehouse_code='WH1')
        self.spec = PreformSpec.objects.create(
            company=self.company, make='Frystal', gram=Decimal('40'),
            preforms_per_box=1000, preform_rate_per_bottle=Decimal('6.36'),
            sap_item_code='PF40')
        BlowingRateConfig.objects.create(
            company=self.company, effective_from=date(2026, 1, 1),
            operator_rate_per_day=Decimal('900'), labour_rate_per_day=Decimal('600'),
            electricity_rate_per_unit=Decimal('6.95'),
            scrap_rate_per_bottle=Decimal('1.8'), packing_rate_per_bottle=Decimal('0.2'))

    def _make_run(self):
        return self.svc.create_run({
            'date': date(2026, 6, 13), 'machine_id': self.machine.id,
            'preform_spec_id': self.spec.id, 'preform_boxes_used': Decimal('10'),
        })

    def _approve_via_warehouse(self, bom):
        """Approve a blowing preform BOM request through the warehouse service
        (stock check relaxed for preform)."""
        from warehouse.services.warehouse_service import WarehouseService
        line = bom.lines.first()
        WarehouseService('TEST_OIL').approve_bom_request(
            bom.id,
            {'lines': [{'line_id': line.id, 'approved_qty': str(line.required_qty), 'status': 'APPROVED'}]},
            user=None,
        )

    def test_start_gated_by_warehouse_approval(self):
        run = self._make_run()
        self.assertEqual(run.warehouse_approval_status, 'NOT_REQUESTED')
        with self.assertRaises(ValueError):
            self.svc.start_production(run.id)  # not requested yet

        bom = self.svc.submit_preform_request(run.id)
        self.assertEqual(bom.blowing_run_id, run.id)
        self.assertEqual(bom.required_qty, Decimal('10000.00'))  # 10 boxes x 1000
        run.refresh_from_db()
        self.assertEqual(run.warehouse_approval_status, 'PENDING')
        with self.assertRaises(ValueError):
            self.svc.start_production(run.id)  # still pending

        self._approve_via_warehouse(bom)
        run.refresh_from_db()
        self.assertEqual(run.warehouse_approval_status, 'APPROVED')
        seg = self.svc.start_production(run.id)  # now allowed
        self.assertTrue(seg.is_active)
        run.refresh_from_db()
        self.assertEqual(run.status, 'IN_PROGRESS')

    def test_full_flow(self):
        run = self._make_run()
        bom = self.svc.submit_preform_request(run.id)
        self._approve_via_warehouse(bom)
        self.svc.start_production(run.id)
        self.svc.stop_production(run.id, produced_pcs=5000)
        # breakdown then resume
        bd = self.svc.add_breakdown(run.id, {'reason': 'mould jam'})
        self.assertTrue(bd.is_active)
        self.svc.resolve_breakdown(run.id, bd.id, 'start_production')
        self.svc.stop_production(run.id, produced_pcs=3000)
        # cannot complete with an active segment? none active now
        run2 = self.svc.complete_run(run.id, {
            'total_counter_production': 8000, 'rejection_pcs': 100,
            'operator_count': 2, 'contract_labour_count': 2, 'own_labour_count': 8,
            'machine_start_reading': Decimal('100'), 'machine_stop_reading': Decimal('250'),
            'utility_units': Decimal('50'), 'scrap_carton_value': Decimal('0'),
        })
        self.assertEqual(run2.status, 'COMPLETED')
        self.assertEqual(run2.segments.count(), 2)
        self.assertEqual(run2.breakdowns.count(), 1)
        self.assertTrue(hasattr(run2, 'cost_summary'))
        self.assertEqual(run2.cost_summary.good_bottles, 7900)  # 8000 - 100

    def test_cannot_complete_while_running(self):
        run = self._make_run()
        bom = self.svc.submit_preform_request(run.id)
        self._approve_via_warehouse(bom)
        self.svc.start_production(run.id)
        with self.assertRaises(ValueError):
            self.svc.complete_run(run.id, {'total_counter_production': 100})


class BlowingAuditTest(TestCase):
    def setUp(self):
        from company.models import Company
        from .services import BlowingService
        self.company = Company.objects.create(name='Test Oil', code='TEST_OIL')
        self.svc = BlowingService('TEST_OIL')

    def test_machine_create_and_update_logged(self):
        m = self.svc.create_machine({'name': 'M1', 'heads': 6, 'depreciation_per_day': '100'})
        logs = list(self.svc.list_audit_logs('machine', m.id))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action, 'CREATE')

        self.svc.update_machine(m.id, {'heads': 8, 'name': 'M1'})
        logs = list(self.svc.list_audit_logs('machine', m.id))
        self.assertEqual(len(logs), 2)  # create + update
        upd = logs[0]  # newest first
        self.assertEqual(upd.action, 'UPDATE')
        self.assertIn('heads', upd.changes)
        self.assertEqual(upd.changes['heads']['old'], 6)
        self.assertEqual(upd.changes['heads']['new'], 8)
        self.assertNotIn('name', upd.changes)  # unchanged field not logged

    def test_noop_update_not_logged(self):
        m = self.svc.create_machine({'name': 'M1', 'heads': 6})
        self.svc.update_machine(m.id, {'heads': 6})  # no change
        logs = list(self.svc.list_audit_logs('machine', m.id))
        self.assertEqual(len(logs), 1)  # only the create
