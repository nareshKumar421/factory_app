"""Main breakdowns with sub-breakdowns: Filler › Cap stuck.

Run with: manage.py test production_execution.tests_breakdown_subcategories
"""
import importlib
from datetime import date, timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from production_execution.models import (
    BreakdownCategory, BreakdownSubCategory, MachineBreakdown,
    ProductionLine, ProductionRun, RunStatus,
)

BASE_URL = '/api/v1/production-execution'

seed_migration = importlib.import_module(
    'production_execution.migrations.0051_seed_beverage_breakdowns'
)


class SubBreakdownTestCase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        user = get_user_model().objects.create_user(
            email='bd@test.com', password='testpass123'
        )
        UserCompany.objects.create(
            user=user, company=self.company,
            role=UserRole.objects.create(name='Admin'), is_active=True,
        )
        user.user_permissions.set(
            Permission.objects.filter(content_type__app_label='production_execution')
        )
        self.client = APIClient()
        self.client.force_authenticate(user=get_user_model().objects.get(pk=user.pk))
        self.client.credentials(HTTP_COMPANY_CODE='TEST_CO')

        self.line = ProductionLine.objects.create(company=self.company, name='L1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date.today(),
            line=self.line, status=RunStatus.IN_PROGRESS,
        )
        self.filler = BreakdownCategory.objects.create(company=self.company, name='Filler')
        self.cap_stuck = BreakdownSubCategory.objects.create(
            category=self.filler, name='Cap stuck'
        )
        self.capper = BreakdownSubCategory.objects.create(
            category=self.filler, name='Capper'
        )
        self.power_cut = BreakdownCategory.objects.create(
            company=self.company, name='Power cut'
        )

    def add(self, **payload):
        payload.setdefault('create_maintenance_work_order', False)
        return self.client.post(
            f'{BASE_URL}/runs/{self.run.id}/add-breakdown/', payload, format='json'
        )

    def add_past(self, start_min, end_min, **payload):
        anchor = timezone.now() - timedelta(hours=5)
        payload.update(
            start_time=(anchor + timedelta(minutes=start_min)).isoformat(),
            end_time=(anchor + timedelta(minutes=end_min)).isoformat(),
        )
        return self.client.post(
            f'{BASE_URL}/runs/{self.run.id}/breakdowns/manual/', payload, format='json'
        )


class CategoryListTests(SubBreakdownTestCase):

    def test_each_main_carries_its_active_subs_by_name(self):
        BreakdownSubCategory.objects.create(
            category=self.filler, name='Belt', is_active=False
        )
        resp = self.client.get(f'{BASE_URL}/breakdown-categories/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        by_name = {c['name']: c for c in resp.data}
        self.assertEqual(
            [s['name'] for s in by_name['Filler']['sub_categories']],
            ['Cap stuck', 'Capper'],
        )
        self.assertEqual(by_name['Power cut']['sub_categories'], [])


class AddBreakdownTests(SubBreakdownTestCase):

    def test_sub_breakdown_is_stored_and_reason_may_be_left_out(self):
        resp = self.add(
            breakdown_category_id=self.filler.id,
            breakdown_subcategory_id=self.cap_stuck.id,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['breakdown_category_name'], 'Filler')
        self.assertEqual(resp.data['breakdown_subcategory'], self.cap_stuck.id)
        self.assertEqual(resp.data['breakdown_subcategory_name'], 'Cap stuck')
        self.assertEqual(resp.data['reason'], '')

        detail = self.client.get(f'{BASE_URL}/runs/{self.run.id}/')
        self.assertEqual(
            detail.data['breakdowns'][0]['breakdown_subcategory_name'], 'Cap stuck'
        )

    def test_a_main_with_subs_needs_one(self):
        resp = self.add(breakdown_category_id=self.filler.id, reason='cap jammed')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Pick a sub-breakdown under Filler', resp.data['detail'])
        self.assertFalse(MachineBreakdown.objects.exists())

    def test_a_sub_from_another_main_is_refused(self):
        resp = self.add(
            breakdown_category_id=self.power_cut.id,
            breakdown_subcategory_id=self.cap_stuck.id,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(MachineBreakdown.objects.exists())

    def test_a_retired_sub_is_refused(self):
        self.cap_stuck.is_active = False
        self.cap_stuck.save()
        resp = self.add(
            breakdown_category_id=self.filler.id,
            breakdown_subcategory_id=self.cap_stuck.id,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_main_without_subs_still_needs_a_reason(self):
        resp = self.add(breakdown_category_id=self.power_cut.id)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Reason is required', resp.data['detail'])

        resp = self.add(breakdown_category_id=self.power_cut.id, reason='grid down')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['breakdown_subcategory'], None)
        self.assertEqual(resp.data['breakdown_subcategory_name'], '')

    def test_past_breakdown_takes_a_sub_too(self):
        resp = self.add_past(
            10, 25,
            breakdown_category_id=self.filler.id,
            breakdown_subcategory_id=self.capper.id,
            reason='head 4',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        bd = MachineBreakdown.objects.get(pk=resp.data['id'])
        self.assertEqual(bd.breakdown_subcategory, self.capper)
        self.assertEqual(bd.problem, 'Filler › Capper — head 4')

        resp = self.add_past(30, 40, breakdown_category_id=self.filler.id, reason='x')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class BreakdownTextTests(SubBreakdownTestCase):
    """What a maintenance work order is raised with."""

    def make(self, sub=None, reason=''):
        return MachineBreakdown(
            production_run=self.run, start_time=timezone.now(),
            breakdown_category=self.filler if sub else self.power_cut,
            breakdown_subcategory=sub, reason=reason,
        )

    def test_problem_names_the_sub_and_keeps_the_reason(self):
        self.assertEqual(self.make(self.cap_stuck).problem, 'Filler › Cap stuck')
        self.assertEqual(
            self.make(self.cap_stuck, 'station 3').problem, 'Filler › Cap stuck — station 3'
        )
        self.assertEqual(self.make(reason='grid down').problem, 'grid down')


class DowntimeReportTests(SubBreakdownTestCase):

    def setUp(self):
        super().setUp()
        self.run.status = RunStatus.COMPLETED
        self.run.save()
        start = timezone.now() - timedelta(hours=3)

        def log(minutes, category, sub=None, reason=''):
            MachineBreakdown.objects.create(
                production_run=self.run, start_time=start,
                end_time=start + timedelta(minutes=minutes), breakdown_minutes=minutes,
                is_active=False, breakdown_category=category,
                breakdown_subcategory=sub, reason=reason,
            )

        # Two cap-stuck stops typed differently are one cause.
        log(10, self.filler, self.cap_stuck, 'capstuck')
        log(5, self.filler, self.cap_stuck)
        log(3, self.filler, self.capper)
        log(20, self.power_cut, reason='grid down')

    def test_downtime_analysis_groups_on_the_sub(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/downtime/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [(b['reason'], b['count'], b['total_minutes']) for b in resp.data['breakdowns']],
            [('grid down', 1, 20), ('Filler › Cap stuck', 2, 15), ('Filler › Capper', 1, 3)],
        )

    def test_pareto_splits_a_main_by_its_subs(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/downtime-pareto/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        filler = next(p for p in resp.data['pareto'] if p['category'] == 'Filler')
        self.assertEqual(filler['total_minutes'], 18)
        self.assertEqual(
            [(r['reason'], r['count'], r['total_minutes'], r['avg_minutes'])
             for r in filler['reasons']],
            [('Cap stuck', 2, 15, 7.5), ('Capper', 1, 3, 3.0)],
        )


class SeedBeverageBreakdownsTests(TestCase):

    def seed(self):
        seed_migration.seed(apps, None)

    def test_seeds_beverages_once_and_leaves_other_companies_alone(self):
        bev = Company.objects.create(code='JIVO_BEVERAGES', name='Jivo Beverages')
        oil = Company.objects.create(code='JIVO_OIL', name='Jivo Oil')
        # Already there by hand, retired, in another case: reused, not doubled.
        BreakdownCategory.objects.create(company=bev, name='FILLER', is_active=False)

        self.seed()
        self.seed()

        mains = BreakdownCategory.objects.filter(company=bev)
        self.assertEqual(mains.count(), len(seed_migration.BREAKDOWNS))
        self.assertFalse(mains.filter(is_active=False).exists())
        filler = mains.get(name__iexact='filler')
        self.assertEqual(
            sorted(filler.sub_categories.values_list('name', flat=True)),
            sorted(seed_migration.BREAKDOWNS['Filler']),
        )
        self.assertEqual(
            BreakdownSubCategory.objects.filter(category__company=bev).count(),
            sum(len(subs) for subs in seed_migration.BREAKDOWNS.values()),
        )
        self.assertFalse(mains.get(name='Power cut').sub_categories.exists())
        self.assertFalse(BreakdownCategory.objects.filter(company=oil).exists())

    def test_no_beverages_company_is_a_no_op(self):
        self.seed()
        self.assertFalse(BreakdownCategory.objects.exists())
