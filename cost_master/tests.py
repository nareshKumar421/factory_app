"""Cost Master tests — scope validation, effective dating, resolution
precedence, and API permission gating.

DB-backed: run with the SQLite scratchpad settings (shared Postgres must not
be used for tests) and an explicit app label::

    python manage.py test cost_master --noinput
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import Department
from company.models import Company

from . import services
from .models import CostRate, CostScope, CostType

User = get_user_model()


def _mk_type(code='labour-contract', name='Contract Labour', **kw):
    return services.create_cost_type({'code': code, 'name': name, **kw})


class CostTypeServiceTest(TestCase):
    def test_duplicate_code_rejected(self):
        _mk_type()
        with self.assertRaises(ValueError):
            _mk_type(name='Another')

    def test_soft_delete_keeps_code_reserved(self):
        ct = _mk_type()
        services.delete_cost_type(ct.id)
        ct.refresh_from_db()
        self.assertFalse(ct.is_active)
        with self.assertRaises(ValueError):
            _mk_type()  # code still taken by the inactive row


class CostRateServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        cls.mart = Company.objects.create(name='Jivo Mart', code='JIVO_MART')
        cls.dept = Department.objects.create(name='Blowing')
        cls.ct = _mk_type()

    def _upsert(self, **kw):
        data = {'cost_type_id': self.ct.id, 'rate': Decimal('100'),
                'scope': CostScope.FACTORY, **kw}
        return services.upsert_rate(data)

    def test_scope_target_validation(self):
        with self.assertRaises(ValueError):
            self._upsert(scope=CostScope.COMPANY)          # needs company
        with self.assertRaises(ValueError):
            self._upsert(scope=CostScope.DEPARTMENT)       # needs department
        with self.assertRaises(ValueError):
            self._upsert(scope=CostScope.VALUE)            # needs value_key
        with self.assertRaises(ValueError):
            self._upsert(scope=CostScope.COMPANY, company_id=999999)

    def test_factory_scope_drops_irrelevant_targets(self):
        rate = self._upsert(scope=CostScope.FACTORY, company_id=self.oil.id,
                            value_key='machine:BM-01')
        self.assertIsNone(rate.company_id)
        self.assertEqual(rate.value_key, '')

    def test_same_date_overwrites_new_date_adds_row(self):
        first = self._upsert(rate=Decimal('100'),
                             effective_from=date(2026, 8, 1))
        fixed = self._upsert(rate=Decimal('110'),
                             effective_from=date(2026, 8, 1))
        self.assertEqual(first.id, fixed.id)   # typo fix, same row
        self.assertEqual(fixed.rate, Decimal('110'))
        later = self._upsert(rate=Decimal('120'),
                             effective_from=date(2026, 9, 1))
        self.assertNotEqual(later.id, first.id)
        self.assertEqual(CostRate.objects.filter(is_active=True).count(), 2)

    def test_list_rates_current_vs_history(self):
        self._upsert(rate=Decimal('100'), effective_from=date(2026, 8, 1))
        self._upsert(rate=Decimal('120'), effective_from=date(2026, 9, 1))
        current = services.list_rates(as_of=date(2026, 8, 15))
        self.assertEqual([r.rate for r in current], [Decimal('100')])
        current = services.list_rates(as_of=date(2026, 9, 2))
        self.assertEqual([r.rate for r in current], [Decimal('120')])
        history = services.list_rates(history=True)
        self.assertEqual(len(history), 2)

    def test_list_rates_department_scope_company_split(self):
        services.upsert_rate({
            'cost_type_id': self.ct.id, 'scope': CostScope.DEPARTMENT,
            'department_id': self.dept.id, 'rate': Decimal('50')})
        services.upsert_rate({
            'cost_type_id': self.ct.id, 'scope': CostScope.DEPARTMENT,
            'department_id': self.dept.id, 'company_id': self.oil.id,
            'rate': Decimal('60')})
        plain = services.list_rates(scope=CostScope.DEPARTMENT,
                                    department_id=self.dept.id)
        self.assertEqual([r.rate for r in plain], [Decimal('50')])
        oil = services.list_rates(scope=CostScope.DEPARTMENT,
                                  department_id=self.dept.id,
                                  company_id=self.oil.id)
        self.assertEqual([r.rate for r in oil], [Decimal('60')])

    def test_delete_then_reupsert(self):
        rate = self._upsert(effective_from=date(2026, 8, 1))
        services.delete_rate(rate.id)
        again = self._upsert(rate=Decimal('105'), effective_from=date(2026, 8, 1))
        self.assertNotEqual(again.id, rate.id)
        self.assertEqual(again.rate, Decimal('105'))

    def test_resolve_precedence(self):
        day = date(2026, 8, 15)
        kw = {'cost_type_id': self.ct.id, 'effective_from': date(2026, 8, 1)}
        services.upsert_rate({**kw, 'scope': CostScope.FACTORY,
                              'rate': Decimal('1')})
        services.upsert_rate({**kw, 'scope': CostScope.COMPANY,
                              'company_id': self.oil.id, 'rate': Decimal('2')})
        services.upsert_rate({**kw, 'scope': CostScope.DEPARTMENT,
                              'department_id': self.dept.id, 'rate': Decimal('3')})
        services.upsert_rate({**kw, 'scope': CostScope.DEPARTMENT,
                              'department_id': self.dept.id,
                              'company_id': self.oil.id, 'rate': Decimal('4')})
        services.upsert_rate({**kw, 'scope': CostScope.VALUE,
                              'value_key': 'machine:BM-01', 'rate': Decimal('5')})

        def resolved(**ctx):
            r = services.resolve_rate('labour-contract', as_of=day, **ctx)
            return r.rate if r else None

        self.assertEqual(resolved(), Decimal('1'))
        self.assertEqual(resolved(company_id=self.mart.id), Decimal('1'))
        self.assertEqual(resolved(company_id=self.oil.id), Decimal('2'))
        self.assertEqual(resolved(company_id=self.mart.id,
                                  department_id=self.dept.id), Decimal('3'))
        self.assertEqual(resolved(company_id=self.oil.id,
                                  department_id=self.dept.id), Decimal('4'))
        self.assertEqual(resolved(company_id=self.oil.id,
                                  department_id=self.dept.id,
                                  value_key='machine:BM-01'), Decimal('5'))

    def test_resolve_effective_dating(self):
        self._upsert(rate=Decimal('100'), effective_from=date(2026, 8, 1))
        self._upsert(rate=Decimal('120'), effective_from=date(2026, 9, 1))
        aug = services.resolve_rate('labour-contract', as_of=date(2026, 8, 20))
        sep = services.resolve_rate('labour-contract', as_of=date(2026, 9, 1))
        self.assertEqual(aug.rate, Decimal('100'))
        self.assertEqual(sep.rate, Decimal('120'))
        self.assertIsNone(
            services.resolve_rate('labour-contract', as_of=date(2026, 7, 31)))


class ImportScatteredCostsCommandTest(TestCase):
    """The one-time import of rates scattered around the app."""

    @classmethod
    def setUpTestData(cls):
        from blowing.models import BlowingCostRate, BlowingMachine
        from maintenance.models import ElectricityMeter, SafetyViolationType
        from production_execution.models import CostRate as ProductionCostRate
        from production_execution.models import ProductionLine

        cls.oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        cls.bev = Company.objects.create(name='Jivo Beverages', code='JIVO_BEV')

        machine = BlowingMachine.objects.create(company=cls.oil, name='BM-01')
        BlowingCostRate.objects.create(
            company=cls.oil, category='OPERATOR', basis='PER_PERSON_DAY',
            rate=Decimal('900'), effective_from=date(2026, 6, 1))
        BlowingCostRate.objects.create(
            company=cls.oil, category='OPERATOR', basis='PER_PERSON_DAY',
            rate=Decimal('950'), effective_from=date(2026, 8, 1))
        BlowingCostRate.objects.create(
            company=cls.oil, machine=machine, category='ELECTRICITY_MACHINE',
            basis='PER_UNIT', rate=Decimal('7.5'), effective_from=date(2026, 6, 1))

        line = ProductionLine.objects.create(company=cls.bev, name='Line-2')
        ProductionCostRate.objects.create(
            company=cls.bev, category='LABOUR', basis='PER_PERSON_DAY',
            rate=Decimal('600'))
        ProductionCostRate.objects.create(
            company=cls.bev, line=line, category='WATER', basis='PER_UNIT',
            rate=Decimal('0.3'))

        single = ElectricityMeter.objects.create(
            name='MTR-7', rate_per_unit=Decimal('6.95'))
        single.companies.add(cls.oil)
        shared = ElectricityMeter.objects.create(
            name='MTR-SHARED', rate_per_unit=Decimal('7.10'))
        shared.companies.add(cls.oil, cls.bev)
        ElectricityMeter.objects.create(name='MTR-0', rate_per_unit=Decimal('0'))

        SafetyViolationType.objects.create(
            company=cls.oil, name='No Helmet',
            default_fine_amount=Decimal('500'))

    def test_dry_run_writes_nothing(self):
        call_command('import_scattered_costs')
        self.assertEqual(CostType.objects.count(), 0)
        self.assertEqual(CostRate.objects.count(), 0)

    def test_commit_imports_and_is_idempotent(self):
        call_command('import_scattered_costs', '--commit')

        # blowing: 2 dated company rows + 1 machine row
        operator = CostType.objects.get(code='blowing-operator')
        history = CostRate.objects.filter(
            cost_type=operator, scope=CostScope.COMPANY,
            company=self.oil).order_by('effective_from')
        self.assertEqual([r.rate for r in history],
                         [Decimal('900'), Decimal('950')])
        machine_rate = CostRate.objects.get(cost_type__code='blowing-electricity-machine')
        self.assertEqual(machine_rate.scope, CostScope.VALUE)
        self.assertEqual(machine_rate.value_key, 'machine:BM-01')
        self.assertEqual(machine_rate.company_id, self.oil.id)

        # production: PER_UNIT source basis means "per case" → PER_CASE
        water = CostRate.objects.get(cost_type__code='prod-water')
        self.assertEqual(water.basis, 'PER_CASE')
        self.assertEqual(water.value_key, 'line:Line-2')
        labour = CostRate.objects.get(cost_type__code='prod-labour')
        self.assertEqual(labour.scope, CostScope.COMPANY)
        self.assertEqual(labour.company_id, self.bev.id)

        # meters: single-company attributed, shared → factory-level (no company),
        # zero-rate meter skipped
        meters = CostRate.objects.filter(cost_type__code='electricity-meter-unit-rate')
        self.assertEqual(meters.count(), 2)
        self.assertEqual(meters.get(value_key='meter:MTR-7').company_id, self.oil.id)
        self.assertIsNone(meters.get(value_key='meter:MTR-SHARED').company_id)

        fine = CostRate.objects.get(cost_type__code='safety-violation-fine')
        self.assertEqual(fine.value_key, 'violation:No Helmet')
        self.assertEqual(fine.basis, 'FLAT')

        total_rates = CostRate.objects.count()
        total_types = CostType.objects.count()
        call_command('import_scattered_costs', '--commit')  # re-run: no duplicates
        self.assertEqual(CostRate.objects.count(), total_rates)
        self.assertEqual(CostType.objects.count(), total_types)


class CostMasterAPITest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        view = Permission.objects.get(codename='can_view_cost_master')
        manage = Permission.objects.get(codename='can_manage_cost_master')
        cls.viewer = User.objects.create_user(
            email='viewer@jivo.in', password='x',
            full_name='Viewer', employee_code='V1')
        cls.viewer.user_permissions.add(view)
        cls.manager = User.objects.create_user(
            email='manager@jivo.in', password='x',
            full_name='Manager', employee_code='M1')
        cls.manager.user_permissions.add(manage)
        cls.nobody = User.objects.create_user(
            email='nobody@jivo.in', password='x',
            full_name='Nobody', employee_code='N1')

    def test_unauthenticated_rejected(self):
        response = self.client.get('/api/v1/cost-master/cost-types/')
        self.assertEqual(response.status_code, 401)

    def test_no_permission_rejected(self):
        self.client.force_authenticate(self.nobody)
        response = self.client.get('/api/v1/cost-master/cost-types/')
        self.assertEqual(response.status_code, 403)

    def test_viewer_reads_but_cannot_write(self):
        self.client.force_authenticate(self.viewer)
        self.assertEqual(
            self.client.get('/api/v1/cost-master/cost-types/').status_code, 200)
        self.assertEqual(
            self.client.get('/api/v1/cost-master/rates/').status_code, 200)
        response = self.client.post('/api/v1/cost-master/cost-types/', {
            'code': 'water', 'name': 'Water'})
        self.assertEqual(response.status_code, 403)

    def test_manager_full_flow(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post('/api/v1/cost-master/cost-types/', {
            'code': 'labour-contract', 'name': 'Contract Labour',
            'default_basis': 'PER_PERSON_DAY'})
        self.assertEqual(response.status_code, 201, response.data)
        ct_id = response.data['id']

        response = self.client.post('/api/v1/cost-master/rates/', {
            'cost_type_id': ct_id, 'scope': 'COMPANY',
            'company_id': self.oil.id, 'rate': '600',
            'effective_from': '2026-08-01'})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['basis'], 'PER_PERSON_DAY')  # type default
        rate_id = response.data['id']

        response = self.client.get(
            '/api/v1/cost-master/rates/',
            {'scope': 'COMPANY', 'company_id': self.oil.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['company_code'], 'JIVO_OIL')

        response = self.client.post('/api/v1/cost-master/rates/', {
            'cost_type_id': ct_id, 'scope': 'COMPANY', 'rate': '600'})
        self.assertEqual(response.status_code, 400)  # company missing

        self.assertEqual(
            self.client.delete(
                f'/api/v1/cost-master/rates/{rate_id}/').status_code, 204)
        response = self.client.patch(
            f'/api/v1/cost-master/cost-types/{ct_id}/', {'name': 'Labour'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'Labour')
        self.assertEqual(
            self.client.delete(
                f'/api/v1/cost-master/cost-types/{ct_id}/').status_code, 204)
