"""
Production Planning Module Tests

Run with:
    python manage.py test production_planning
"""
import uuid
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from company.models import Company, UserCompany, UserRole
from .models import (
    ProductionPlan, PlanMaterialRequirement,
    WeeklyPlan, DailyProductionEntry,
    PlanStatus, SAPSyncStatus, WeeklyPlanStatus,
)
from .services import ProductionPlanningService

User = get_user_model()

COMPANY_CODE = 'TEST01'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(email=None, password='pass1234'):
    uid = uuid.uuid4().hex[:8]
    return User.objects.create_user(
        email=email or f'user_{uid}@test.com',
        password=password,
        full_name='Test Planner',
        employee_code=f'EMP{uid}',
    )


def make_company():
    return Company.objects.get_or_create(
        code=COMPANY_CODE, defaults={'name': 'Test Company'}
    )[0]


def make_user_company(user, company):
    role, _ = UserRole.objects.get_or_create(name='Planner')
    return UserCompany.objects.get_or_create(
        user=user, company=company,
        defaults={'role': role, 'is_active': True},
    )[0]


def make_plan(company, user=None, **kwargs):
    """Create a production plan (DRAFT by default)."""
    defaults = dict(
        item_code='FG-OIL-1L',
        item_name='Jivo Sunflower Oil 1L',
        uom='LTR',
        warehouse_code='WH-MAIN',
        planned_qty=Decimal('50000'),
        completed_qty=Decimal('0'),
        target_start_date=date(2026, 3, 1),
        due_date=date(2026, 3, 31),
        status=PlanStatus.DRAFT,
        sap_posting_status=SAPSyncStatus.NOT_POSTED,
        created_by=user,
    )
    defaults.update(kwargs)
    return ProductionPlan.objects.create(company=company, **defaults)


def make_weekly_plan(plan, user, **kwargs):
    defaults = dict(
        week_number=1,
        week_label='Week 1',
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 7),
        target_qty=Decimal('12500'),
        created_by=user,
    )
    defaults.update(kwargs)
    return WeeklyPlan.objects.create(production_plan=plan, **defaults)


def make_material(plan, **kwargs):
    defaults = dict(
        component_code='RM-SEEDS',
        component_name='Sunflower Seeds',
        required_qty=Decimal('55000'),
        uom='KG',
        warehouse_code='WH-MAIN',
    )
    defaults.update(kwargs)
    return PlanMaterialRequirement.objects.create(production_plan=plan, **defaults)


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------

class ProductionPlanModelTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.plan = make_plan(self.company, user=self.user)

    def test_str_contains_item_code(self):
        self.assertIn('FG-OIL-1L', str(self.plan))

    def test_default_status_is_draft(self):
        self.assertEqual(self.plan.status, PlanStatus.DRAFT)

    def test_default_sap_posting_status(self):
        self.assertEqual(self.plan.sap_posting_status, SAPSyncStatus.NOT_POSTED)

    def test_progress_percent_zero_when_no_production(self):
        self.assertEqual(self.plan.progress_percent, 0.0)

    def test_progress_percent_partial(self):
        self.plan.completed_qty = Decimal('25000')
        self.plan.save()
        self.assertEqual(self.plan.progress_percent, 50.0)

    def test_progress_percent_full(self):
        self.plan.completed_qty = self.plan.planned_qty
        self.plan.save()
        self.assertEqual(self.plan.progress_percent, 100.0)

    def test_recompute_completed_qty(self):
        week = make_weekly_plan(self.plan, self.user)
        DailyProductionEntry.objects.create(
            weekly_plan=week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('5000'),
            recorded_by=self.user,
        )
        DailyProductionEntry.objects.create(
            weekly_plan=week,
            production_date=date(2026, 3, 2),
            produced_qty=Decimal('3000'),
            recorded_by=self.user,
        )
        self.plan.recompute_completed_qty()
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.completed_qty, Decimal('8000'))

    def test_sap_doc_entry_nullable(self):
        # New plans have no SAP doc entry until posted
        self.assertIsNone(self.plan.sap_doc_entry)
        self.assertIsNone(self.plan.sap_doc_num)

    def test_no_unique_together_on_sap_doc_entry(self):
        """Multiple DRAFT plans can exist without sap_doc_entry."""
        plan2 = make_plan(self.company, user=self.user)
        self.assertIsNone(plan2.sap_doc_entry)
        # Both plans exist without any integrity error


class WeeklyPlanModelTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.plan = make_plan(self.company, user=self.user, status=PlanStatus.OPEN)
        self.week = make_weekly_plan(self.plan, self.user)

    def test_progress_percent(self):
        self.week.produced_qty = Decimal('6250')
        self.week.save()
        self.assertEqual(self.week.progress_percent, 50.0)

    def test_recompute_produced_qty_pending(self):
        self.week.recompute_produced_qty()
        self.assertEqual(self.week.produced_qty, Decimal('0'))
        self.assertEqual(self.week.status, WeeklyPlanStatus.PENDING)

    def test_recompute_produced_qty_in_progress(self):
        DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('5000'),
            recorded_by=self.user,
        )
        self.week.recompute_produced_qty()
        self.assertEqual(self.week.produced_qty, Decimal('5000'))
        self.assertEqual(self.week.status, WeeklyPlanStatus.IN_PROGRESS)

    def test_recompute_produced_qty_completed(self):
        DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('12500'),
            recorded_by=self.user,
        )
        self.week.recompute_produced_qty()
        self.assertEqual(self.week.status, WeeklyPlanStatus.COMPLETED)


class PlanMaterialTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.plan = make_plan(self.company, user=self.user)

    def test_material_fields(self):
        mat = make_material(self.plan)
        self.assertEqual(mat.component_code, 'RM-SEEDS')
        self.assertEqual(mat.warehouse_code, 'WH-MAIN')

    def test_no_issued_qty_field(self):
        """PlanMaterialRequirement no longer has issued_qty."""
        mat = make_material(self.plan)
        self.assertFalse(hasattr(mat, 'issued_qty'))


# ---------------------------------------------------------------------------
# Serializer Tests
# ---------------------------------------------------------------------------

class ProductionPlanCreateSerializerTest(TestCase):

    def _make_serializer(self, data):
        from .serializers import ProductionPlanCreateSerializer
        return ProductionPlanCreateSerializer(data=data)

    def test_valid_data(self):
        s = self._make_serializer({
            'item_code': 'FG-OIL-1L',
            'item_name': 'Jivo Oil 1L',
            'planned_qty': '50000',
            'target_start_date': '2026-03-01',
            'due_date': '2026-03-31',
        })
        self.assertTrue(s.is_valid(), s.errors)

    def test_due_date_before_start_date_rejected(self):
        s = self._make_serializer({
            'item_code': 'FG-OIL-1L',
            'item_name': 'Jivo Oil 1L',
            'planned_qty': '50000',
            'target_start_date': '2026-03-31',
            'due_date': '2026-03-01',
        })
        self.assertFalse(s.is_valid())

    def test_with_nested_materials(self):
        s = self._make_serializer({
            'item_code': 'FG-OIL-1L',
            'item_name': 'Jivo Oil 1L',
            'planned_qty': '50000',
            'target_start_date': '2026-03-01',
            'due_date': '2026-03-31',
            'materials': [
                {
                    'component_code': 'RM-SEEDS',
                    'component_name': 'Sunflower Seeds',
                    'required_qty': '55000',
                    'uom': 'KG',
                }
            ],
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(len(s.validated_data['materials']), 1)


class WeeklyPlanCreateSerializerTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.plan = make_plan(
            self.company, user=self.user, status=PlanStatus.OPEN,
            sap_posting_status=SAPSyncStatus.POSTED,
        )

    def _make_serializer(self, data):
        from .serializers import WeeklyPlanCreateSerializer
        return WeeklyPlanCreateSerializer(
            data=data, context={'production_plan': self.plan}
        )

    def test_valid_data(self):
        s = self._make_serializer({
            'week_number': 1,
            'week_label': 'Week 1',
            'start_date': '2026-03-01',
            'end_date': '2026-03-07',
            'target_qty': '12500',
        })
        self.assertTrue(s.is_valid(), s.errors)

    def test_end_date_before_start_date_rejected(self):
        s = self._make_serializer({
            'week_number': 1,
            'start_date': '2026-03-07',
            'end_date': '2026-03-01',
            'target_qty': '12500',
        })
        self.assertFalse(s.is_valid())

    def test_dates_outside_plan_range_rejected(self):
        s = self._make_serializer({
            'week_number': 1,
            'start_date': '2026-04-01',
            'end_date': '2026-04-07',
            'target_qty': '12500',
        })
        self.assertFalse(s.is_valid())

    def test_target_exceeds_planned_qty_rejected(self):
        s = self._make_serializer({
            'week_number': 1,
            'start_date': '2026-03-01',
            'end_date': '2026-03-07',
            'target_qty': '99999',
        })
        self.assertFalse(s.is_valid())

    def test_duplicate_week_number_rejected(self):
        make_weekly_plan(self.plan, self.user, week_number=1)
        s = self._make_serializer({
            'week_number': 1,
            'start_date': '2026-03-01',
            'end_date': '2026-03-07',
            'target_qty': '5000',
        })
        self.assertFalse(s.is_valid())


class DailyEntryCreateSerializerTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.plan = make_plan(self.company, user=self.user, status=PlanStatus.IN_PROGRESS)
        self.week = make_weekly_plan(self.plan, self.user)

    def _make_serializer(self, data):
        from .serializers import DailyProductionEntryCreateSerializer
        return DailyProductionEntryCreateSerializer(
            data=data, context={'weekly_plan': self.week}
        )

    def test_valid_entry(self):
        s = self._make_serializer({
            'production_date': '2026-03-03',
            'produced_qty': '2000',
        })
        self.assertTrue(s.is_valid(), s.errors)

    def test_date_outside_week_range_rejected(self):
        s = self._make_serializer({
            'production_date': '2026-03-15',
            'produced_qty': '2000',
        })
        self.assertFalse(s.is_valid())

    def test_future_date_rejected(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        s = self._make_serializer({
            'production_date': future,
            'produced_qty': '2000',
        })
        self.assertFalse(s.is_valid())

    def test_duplicate_entry_rejected(self):
        DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 3),
            produced_qty=Decimal('1000'),
            shift=None,
            recorded_by=self.user,
        )
        s = self._make_serializer({
            'production_date': '2026-03-03',
            'produced_qty': '2000',
        })
        self.assertFalse(s.is_valid())

    def test_closed_plan_rejected(self):
        self.plan.status = PlanStatus.CLOSED
        self.plan.save()
        s = self._make_serializer({
            'production_date': '2026-03-03',
            'produced_qty': '2000',
        })
        self.assertFalse(s.is_valid())


# ---------------------------------------------------------------------------
# Service Tests
# ---------------------------------------------------------------------------

class PlanCRUDServiceTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.service = ProductionPlanningService(COMPANY_CODE)

    def test_create_plan(self):
        plan = self.service.create_plan(
            data={
                'item_code': 'FG-OIL-1L',
                'item_name': 'Jivo Oil 1L',
                'uom': 'LTR',
                'warehouse_code': 'WH-MAIN',
                'planned_qty': Decimal('50000'),
                'target_start_date': date(2026, 3, 1),
                'due_date': date(2026, 3, 31),
                'materials': [],
            },
            user=self.user,
        )
        self.assertEqual(plan.status, PlanStatus.DRAFT)
        self.assertEqual(plan.sap_posting_status, SAPSyncStatus.NOT_POSTED)
        self.assertIsNone(plan.sap_doc_entry)

    def test_create_plan_with_materials(self):
        plan = self.service.create_plan(
            data={
                'item_code': 'FG-OIL-1L',
                'item_name': 'Jivo Oil 1L',
                'planned_qty': Decimal('50000'),
                'target_start_date': date(2026, 3, 1),
                'due_date': date(2026, 3, 31),
                'materials': [
                    {
                        'component_code': 'RM-SEEDS',
                        'component_name': 'Seeds',
                        'required_qty': Decimal('55000'),
                        'uom': 'KG',
                    }
                ],
            },
            user=self.user,
        )
        self.assertEqual(plan.materials.count(), 1)

    def test_update_plan_draft_allowed(self):
        plan = make_plan(self.company, user=self.user)
        updated = self.service.update_plan(plan.id, {'remarks': 'Updated'})
        self.assertEqual(updated.remarks, 'Updated')

    def test_update_plan_non_draft_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.OPEN)
        with self.assertRaises(ValueError):
            self.service.update_plan(plan.id, {'remarks': 'Should fail'})

    def test_delete_plan_draft_allowed(self):
        plan = make_plan(self.company, user=self.user)
        plan_id = plan.id
        self.service.delete_plan(plan_id)
        self.assertFalse(ProductionPlan.objects.filter(id=plan_id).exists())

    def test_delete_plan_non_draft_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.OPEN)
        with self.assertRaises(ValueError):
            self.service.delete_plan(plan.id)

    def test_get_plans_empty(self):
        plans = self.service.get_plans()
        self.assertEqual(list(plans), [])

    def test_get_plans_status_filter(self):
        make_plan(self.company, user=self.user)
        make_plan(
            self.company, user=self.user,
            item_code='FG-OIL-2L', item_name='Oil 2L',
            status=PlanStatus.OPEN,
        )
        self.assertEqual(self.service.get_plans(status='DRAFT').count(), 1)
        self.assertEqual(self.service.get_plans(status='OPEN').count(), 1)

    def test_get_plan_not_found_raises(self):
        with self.assertRaises(ValueError):
            self.service.get_plan(9999)


class MaterialServiceTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.service = ProductionPlanningService(COMPANY_CODE)
        self.plan = make_plan(self.company, user=self.user)

    def test_add_material_to_draft(self):
        mat = self.service.add_material(self.plan.id, {
            'component_code': 'RM-SEEDS',
            'component_name': 'Seeds',
            'required_qty': Decimal('1000'),
            'uom': 'KG',
        })
        self.assertEqual(mat.component_code, 'RM-SEEDS')

    def test_delete_material_draft_allowed(self):
        mat = make_material(self.plan)
        self.service.delete_material(self.plan.id, mat.id)
        self.assertFalse(PlanMaterialRequirement.objects.filter(id=mat.id).exists())

    def test_delete_material_non_draft_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.OPEN)
        mat = make_material(plan)
        with self.assertRaises(ValueError):
            self.service.delete_material(plan.id, mat.id)

    def test_add_material_to_closed_plan_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.CLOSED)
        with self.assertRaises(ValueError):
            self.service.add_material(plan.id, {
                'component_code': 'RM-X',
                'component_name': 'X',
                'required_qty': Decimal('100'),
            })


class SAPPostingServiceTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.service = ProductionPlanningService(COMPANY_CODE)

    @patch('production_planning.services.SAPClient')
    def test_post_to_sap_success(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.create_production_order.return_value = {
            'DocEntry': 42,
            'DocNum': 10042,
            'Status': 'R',
        }

        plan = make_plan(self.company, user=self.user)
        result = self.service.post_to_sap(plan.id, self.user)

        self.assertEqual(result.sap_doc_entry, 42)
        self.assertEqual(result.sap_doc_num, 10042)
        self.assertEqual(result.sap_posting_status, SAPSyncStatus.POSTED)
        self.assertEqual(result.status, PlanStatus.OPEN)

    @patch('production_planning.services.SAPClient')
    def test_post_to_sap_failure_records_error(self, MockSAPClient):
        from sap_client.exceptions import SAPConnectionError
        mock_client = MockSAPClient.return_value
        mock_client.create_production_order.side_effect = SAPConnectionError('timeout')

        plan = make_plan(self.company, user=self.user)
        with self.assertRaises(SAPConnectionError):
            self.service.post_to_sap(plan.id, self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.sap_posting_status, SAPSyncStatus.FAILED)
        self.assertIsNotNone(plan.sap_error_message)

    def test_post_already_posted_raises(self):
        plan = make_plan(
            self.company, user=self.user,
            sap_posting_status=SAPSyncStatus.POSTED,
            sap_doc_entry=42, sap_doc_num=10042,
        )
        with self.assertRaises(ValueError):
            self.service.post_to_sap(plan.id, self.user)

    def test_post_completed_plan_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.COMPLETED)
        with self.assertRaises(ValueError):
            self.service.post_to_sap(plan.id, self.user)


class CloseAndWeeklyServiceTest(TestCase):

    def setUp(self):
        self.company = make_company()
        self.user = make_user()
        self.service = ProductionPlanningService(COMPANY_CODE)

    def test_close_plan(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.IN_PROGRESS)
        week = make_weekly_plan(plan, self.user)
        DailyProductionEntry.objects.create(
            weekly_plan=week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('50000'),
            recorded_by=self.user,
        )
        closed = self.service.close_plan(plan.id, self.user)
        self.assertEqual(closed.status, PlanStatus.COMPLETED)
        self.assertEqual(closed.closed_by, self.user)

    def test_close_already_closed_raises(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.CLOSED)
        with self.assertRaises(ValueError):
            self.service.close_plan(plan.id, self.user)

    def test_create_weekly_plan_transitions_open_to_in_progress(self):
        plan = make_plan(
            self.company, user=self.user, status=PlanStatus.OPEN,
            sap_posting_status=SAPSyncStatus.POSTED,
        )
        self.service.create_weekly_plan(
            plan_id=plan.id,
            data={
                'week_number': 1,
                'week_label': 'Week 1',
                'start_date': date(2026, 3, 1),
                'end_date': date(2026, 3, 7),
                'target_qty': Decimal('12500'),
            },
            user=self.user,
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.IN_PROGRESS)

    def test_create_weekly_plan_on_draft_raises(self):
        """Weekly plans require the plan to be posted to SAP first."""
        plan = make_plan(self.company, user=self.user)
        with self.assertRaises(ValueError):
            self.service.create_weekly_plan(
                plan_id=plan.id,
                data={
                    'week_number': 1,
                    'start_date': date(2026, 3, 1),
                    'end_date': date(2026, 3, 7),
                    'target_qty': Decimal('12500'),
                },
                user=self.user,
            )

    def test_delete_weekly_plan_with_entries_raises(self):
        plan = make_plan(
            self.company, user=self.user, status=PlanStatus.OPEN,
            sap_posting_status=SAPSyncStatus.POSTED,
        )
        week = make_weekly_plan(plan, self.user)
        DailyProductionEntry.objects.create(
            weekly_plan=week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('1000'),
            recorded_by=self.user,
        )
        with self.assertRaises(ValueError):
            self.service.delete_weekly_plan(plan.id, week.id)

    def test_add_daily_entry_updates_totals(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.IN_PROGRESS)
        week = make_weekly_plan(plan, self.user)

        self.service.add_daily_entry(
            week_id=week.id,
            data={
                'production_date': date(2026, 3, 1),
                'produced_qty': Decimal('5000'),
            },
            user=self.user,
        )

        week.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(week.produced_qty, Decimal('5000'))
        self.assertEqual(plan.completed_qty, Decimal('5000'))

    def test_get_summary(self):
        make_plan(
            self.company, user=self.user,
            planned_qty=Decimal('10000'),
        )
        make_plan(
            self.company, user=self.user,
            item_code='FG-OIL-2L', item_name='Oil 2L',
            planned_qty=Decimal('20000'),
            status=PlanStatus.IN_PROGRESS,
        )
        summary = self.service.get_summary()
        self.assertEqual(summary['total_plans'], 2)
        self.assertEqual(summary['total_planned_qty'], 30000.0)
        self.assertIn('DRAFT', summary['status_breakdown'])
        self.assertIn('IN_PROGRESS', summary['status_breakdown'])


# ---------------------------------------------------------------------------
# API (Integration) Tests
# ---------------------------------------------------------------------------

class BaseAPITest(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        make_user_company(self.user, self.company)

        from django.contrib.auth.models import Permission
        perms = Permission.objects.filter(
            content_type__app_label='production_planning'
        )
        self.user.user_permissions.set(perms)

        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Token {token.key}',
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )


class PlanListCreateAPITest(BaseAPITest):

    BASE_URL = '/api/v1/production-planning/'

    def test_list_empty(self):
        resp = self.client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_returns_plans(self):
        make_plan(self.company, user=self.user)
        resp = self.client.get(self.BASE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_create_plan(self):
        resp = self.client.post(
            self.BASE_URL,
            data={
                'item_code': 'FG-OIL-1L',
                'item_name': 'Jivo Oil 1L',
                'uom': 'LTR',
                'planned_qty': '50000',
                'target_start_date': '2026-03-01',
                'due_date': '2026-03-31',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['status'], 'DRAFT')
        self.assertEqual(data['sap_posting_status'], 'NOT_POSTED')
        self.assertIsNone(data['sap_doc_entry'])

    def test_create_plan_with_materials(self):
        resp = self.client.post(
            self.BASE_URL,
            data={
                'item_code': 'FG-OIL-1L',
                'item_name': 'Jivo Oil 1L',
                'planned_qty': '50000',
                'target_start_date': '2026-03-01',
                'due_date': '2026-03-31',
                'materials': [
                    {
                        'component_code': 'RM-SEEDS',
                        'component_name': 'Seeds',
                        'required_qty': '55000',
                        'uom': 'KG',
                    }
                ],
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(resp.json()['materials']), 1)

    def test_create_plan_invalid_dates(self):
        resp = self.client.post(
            self.BASE_URL,
            data={
                'item_code': 'FG-OIL-1L',
                'item_name': 'Jivo Oil 1L',
                'planned_qty': '50000',
                'target_start_date': '2026-03-31',
                'due_date': '2026-03-01',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_status_filter(self):
        make_plan(self.company, user=self.user)
        make_plan(
            self.company, user=self.user,
            item_code='FG-OIL-2L', item_name='Oil 2L',
            status=PlanStatus.OPEN,
        )
        resp = self.client.get(self.BASE_URL + '?status=DRAFT')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)


class PlanDetailAPITest(BaseAPITest):

    def setUp(self):
        super().setUp()
        self.plan = make_plan(self.company, user=self.user)
        self.url = f'/api/v1/production-planning/{self.plan.id}/'

    def test_get_detail(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['item_code'], 'FG-OIL-1L')
        self.assertIn('materials', data)
        self.assertIn('weekly_plans', data)

    def test_get_not_found(self):
        resp = self.client.get('/api/v1/production-planning/9999/')
        self.assertEqual(resp.status_code, 404)

    def test_patch_draft_allowed(self):
        resp = self.client.patch(self.url, data={'remarks': 'test remark'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['remarks'], 'test remark')

    def test_patch_non_draft_rejected(self):
        self.plan.status = PlanStatus.OPEN
        self.plan.save()
        resp = self.client.patch(self.url, data={'remarks': 'fail'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_delete_draft_allowed(self):
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProductionPlan.objects.filter(id=self.plan.id).exists())

    def test_delete_non_draft_rejected(self):
        self.plan.status = PlanStatus.OPEN
        self.plan.save()
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 400)


class PostToSAPAPITest(BaseAPITest):

    def setUp(self):
        super().setUp()
        self.plan = make_plan(self.company, user=self.user)
        self.url = f'/api/v1/production-planning/{self.plan.id}/post-to-sap/'

    @patch('production_planning.services.SAPClient')
    def test_post_to_sap_success(self, MockSAPClient):
        mock_client = MockSAPClient.return_value
        mock_client.create_production_order.return_value = {
            'DocEntry': 42,
            'DocNum': 10042,
            'Status': 'R',
        }
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['sap_doc_num'], 10042)

    @patch('production_planning.services.SAPClient')
    def test_post_to_sap_sap_error(self, MockSAPClient):
        from sap_client.exceptions import SAPConnectionError
        MockSAPClient.return_value.create_production_order.side_effect = (
            SAPConnectionError('timeout')
        )
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 503)

    def test_post_already_posted_rejected(self):
        self.plan.sap_posting_status = SAPSyncStatus.POSTED
        self.plan.sap_doc_entry = 42
        self.plan.sap_doc_num = 10042
        self.plan.save()
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 400)


class ClosePlanAPITest(BaseAPITest):

    def test_close_plan(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.IN_PROGRESS)
        resp = self.client.post(f'/api/v1/production-planning/{plan.id}/close/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['status'], 'COMPLETED')

    def test_close_already_closed_rejected(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.CLOSED)
        resp = self.client.post(f'/api/v1/production-planning/{plan.id}/close/')
        self.assertEqual(resp.status_code, 400)

    def test_summary(self):
        make_plan(self.company, user=self.user)
        resp = self.client.get('/api/v1/production-planning/summary/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('total_plans', data)
        self.assertIn('status_breakdown', data)
        self.assertIn('sap_posting_breakdown', data)


class MaterialAPITest(BaseAPITest):

    def setUp(self):
        super().setUp()
        self.plan = make_plan(self.company, user=self.user)

    def test_list_materials(self):
        make_material(self.plan)
        resp = self.client.get(f'/api/v1/production-planning/{self.plan.id}/materials/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_add_material(self):
        resp = self.client.post(
            f'/api/v1/production-planning/{self.plan.id}/materials/',
            data={
                'component_code': 'RM-SEEDS',
                'component_name': 'Seeds',
                'required_qty': '1000',
                'uom': 'KG',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['component_code'], 'RM-SEEDS')

    def test_delete_material(self):
        mat = make_material(self.plan)
        resp = self.client.delete(
            f'/api/v1/production-planning/{self.plan.id}/materials/{mat.id}/'
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PlanMaterialRequirement.objects.filter(id=mat.id).exists())

    def test_delete_material_non_draft_rejected(self):
        plan = make_plan(self.company, user=self.user, status=PlanStatus.OPEN)
        mat = make_material(plan)
        resp = self.client.delete(
            f'/api/v1/production-planning/{plan.id}/materials/{mat.id}/'
        )
        self.assertEqual(resp.status_code, 400)


class DropdownAPITest(BaseAPITest):

    @patch('production_planning.services.HanaItemReader')
    def test_items_dropdown(self, MockReader):
        from sap_client.dtos import ItemDTO
        MockReader.return_value.get_all_items.return_value = [
            ItemDTO(
                item_code='FG-OIL-1L', item_name='Jivo Oil 1L',
                uom='LTR', item_group='Finished Goods',
                make_item=True, purchase_item=False,
            )
        ]
        resp = self.client.get('/api/v1/production-planning/dropdown/items/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]['item_code'], 'FG-OIL-1L')

    @patch('production_planning.services.HanaItemReader')
    def test_items_dropdown_finished_type(self, MockReader):
        MockReader.return_value.get_finished_goods.return_value = []
        resp = self.client.get('/api/v1/production-planning/dropdown/items/?type=finished')
        self.assertEqual(resp.status_code, 200)
        MockReader.return_value.get_finished_goods.assert_called_once()

    @patch('production_planning.services.HanaItemReader')
    def test_uom_dropdown(self, MockReader):
        from sap_client.dtos import UoMDTO
        MockReader.return_value.get_uom_list.return_value = [
            UoMDTO(uom_code='LTR', uom_name='Litre'),
        ]
        resp = self.client.get('/api/v1/production-planning/dropdown/uom/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]['uom_code'], 'LTR')

    @patch('production_planning.views.ProductionPlanningService.get_warehouses_dropdown')
    def test_warehouses_dropdown(self, mock_wh):
        from sap_client.dtos import WarehouseDTO
        mock_wh.return_value = [WarehouseDTO(warehouse_code='WH-01', warehouse_name='Main WH')]
        resp = self.client.get('/api/v1/production-planning/dropdown/warehouses/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]['warehouse_code'], 'WH-01')


class WeeklyPlanAPITest(BaseAPITest):

    def setUp(self):
        super().setUp()
        self.plan = make_plan(
            self.company, user=self.user,
            status=PlanStatus.OPEN,
            sap_posting_status=SAPSyncStatus.POSTED,
        )

    def test_create_weekly_plan(self):
        resp = self.client.post(
            f'/api/v1/production-planning/{self.plan.id}/weekly-plans/',
            data={
                'week_number': 1,
                'week_label': 'Week 1',
                'start_date': '2026-03-01',
                'end_date': '2026-03-07',
                'target_qty': '12500',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['week_number'], 1)

        # Plan transitions to IN_PROGRESS
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, PlanStatus.IN_PROGRESS)

    def test_list_weekly_plans(self):
        make_weekly_plan(self.plan, self.user)
        resp = self.client.get(
            f'/api/v1/production-planning/{self.plan.id}/weekly-plans/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_create_weekly_plan_exceeds_total(self):
        resp = self.client.post(
            f'/api/v1/production-planning/{self.plan.id}/weekly-plans/',
            data={
                'week_number': 1,
                'start_date': '2026-03-01',
                'end_date': '2026-03-07',
                'target_qty': '99999',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_weekly_plan_no_entries(self):
        week = make_weekly_plan(self.plan, self.user)
        resp = self.client.delete(
            f'/api/v1/production-planning/{self.plan.id}/weekly-plans/{week.id}/'
        )
        self.assertEqual(resp.status_code, 204)

    def test_delete_weekly_plan_with_entries_blocked(self):
        week = make_weekly_plan(self.plan, self.user)
        DailyProductionEntry.objects.create(
            weekly_plan=week,
            production_date=date(2026, 3, 1),
            produced_qty=Decimal('1000'),
            recorded_by=self.user,
        )
        resp = self.client.delete(
            f'/api/v1/production-planning/{self.plan.id}/weekly-plans/{week.id}/'
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_weekly_plan_on_draft_rejected(self):
        draft_plan = make_plan(self.company, user=self.user)
        resp = self.client.post(
            f'/api/v1/production-planning/{draft_plan.id}/weekly-plans/',
            data={
                'week_number': 1,
                'start_date': '2026-03-01',
                'end_date': '2026-03-07',
                'target_qty': '12500',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class DailyEntryAPITest(BaseAPITest):

    def setUp(self):
        super().setUp()
        self.plan = make_plan(self.company, user=self.user, status=PlanStatus.IN_PROGRESS)
        self.week = make_weekly_plan(self.plan, self.user)

    def test_add_daily_entry(self):
        resp = self.client.post(
            f'/api/v1/production-planning/weekly-plans/{self.week.id}/daily-entries/',
            data={
                'production_date': '2026-03-03',
                'produced_qty': '2000',
                'remarks': 'Good run',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data['produced_qty'], '2000.000')
        self.assertIn('weekly_plan_progress', data)
        self.assertIn('plan_progress', data)

        self.week.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertEqual(self.week.produced_qty, Decimal('2000'))
        self.assertEqual(self.plan.completed_qty, Decimal('2000'))

    def test_add_daily_entry_future_date_rejected(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        resp = self.client.post(
            f'/api/v1/production-planning/weekly-plans/{self.week.id}/daily-entries/',
            data={'production_date': future, 'produced_qty': '2000'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_daily_entry_wrong_week_date_rejected(self):
        resp = self.client.post(
            f'/api/v1/production-planning/weekly-plans/{self.week.id}/daily-entries/',
            data={'production_date': '2026-03-20', 'produced_qty': '2000'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_list_daily_entries(self):
        DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 3),
            produced_qty=Decimal('2000'),
            recorded_by=self.user,
        )
        resp = self.client.get(
            f'/api/v1/production-planning/weekly-plans/{self.week.id}/daily-entries/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_all_daily_entries_list(self):
        DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 3),
            produced_qty=Decimal('2000'),
            recorded_by=self.user,
        )
        resp = self.client.get('/api/v1/production-planning/daily-entries/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_edit_daily_entry(self):
        entry = DailyProductionEntry.objects.create(
            weekly_plan=self.week,
            production_date=date(2026, 3, 3),
            produced_qty=Decimal('2000'),
            recorded_by=self.user,
        )
        resp = self.client.patch(
            f'/api/v1/production-planning/weekly-plans/{self.week.id}'
            f'/daily-entries/{entry.id}/',
            data={'produced_qty': '2500', 'remarks': 'corrected'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.produced_qty, Decimal('2500'))


class AuthTest(BaseAPITest):

    def test_unauthenticated_denied(self):
        self.client.credentials()
        resp = self.client.get('/api/v1/production-planning/')
        self.assertEqual(resp.status_code, 401)

    def test_missing_company_code_denied(self):
        token, _ = Token.objects.get_or_create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        resp = self.client.get('/api/v1/production-planning/')
        self.assertEqual(resp.status_code, 403)
