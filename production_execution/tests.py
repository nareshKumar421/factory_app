"""
API tests for production_execution app.
Run with: python manage.py test production_execution -v2
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient
from rest_framework import status

from company.models import Company, UserCompany, UserRole
from production_planning.models import ProductionPlan, PlanStatus

User = get_user_model()

BASE_URL = '/api/v1/production-execution'


class BaseTestCase(TestCase):
    """Common setup for all test classes."""

    def setUp(self):
        self.company = Company.objects.create(
            code='TEST_CO', name='Test Company'
        )
        self.user = User.objects.create_user(
            email='testuser@test.com', password='testpass123'
        )
        self.role = UserRole.objects.create(name='Admin')
        UserCompany.objects.create(
            user=self.user, company=self.company,
            role=self.role, is_active=True
        )
        perms = Permission.objects.filter(
            content_type__app_label='production_execution'
        )
        self.user.user_permissions.set(perms)
        self.user.save()
        self.user = User.objects.get(pk=self.user.pk)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE='TEST_CO')

        self.plan = ProductionPlan.objects.create(
            company=self.company,
            item_code='FG-OIL-1L',
            item_name='Oil 1L',
            uom='BTL',
            planned_qty=Decimal('1000'),
            target_start_date=date.today() - timedelta(days=30),
            due_date=date.today() + timedelta(days=30),
            status=PlanStatus.OPEN,
            created_by=self.user,
        )


class ProductionLineTests(BaseTestCase):

    def test_create_line(self):
        resp = self.client.post(f'{BASE_URL}/lines/', {
            'name': 'Line-1', 'description': 'Main production line',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['name'], 'Line-1')

    def test_list_lines(self):
        self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-2'})
        resp = self.client.get(f'{BASE_URL}/lines/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 2)

    def test_update_line(self):
        create_resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        line_id = create_resp.data['id']
        resp = self.client.patch(f'{BASE_URL}/lines/{line_id}/', {
            'description': 'Updated',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['description'], 'Updated')

    def test_delete_line(self):
        create_resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        line_id = create_resp.data['id']
        resp = self.client.delete(f'{BASE_URL}/lines/{line_id}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_filter_active_lines(self):
        self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        r2 = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-2'})
        self.client.delete(f'{BASE_URL}/lines/{r2.data["id"]}/')
        resp = self.client.get(f'{BASE_URL}/lines/?is_active=true')
        self.assertEqual(len(resp.data), 1)


class MachineTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']

    def test_create_machine(self):
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': '10-Head Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_machines(self):
        self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        resp = self.client.get(f'{BASE_URL}/machines/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_filter_by_type(self):
        self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Capper', 'machine_type': 'CAPPER', 'line_id': self.line_id,
        })
        resp = self.client.get(f'{BASE_URL}/machines/?machine_type=FILLER')
        self.assertEqual(len(resp.data), 1)

    def test_update_machine(self):
        r = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        resp = self.client.patch(f'{BASE_URL}/machines/{r.data["id"]}/', {
            'name': 'Updated',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_machine(self):
        r = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        resp = self.client.delete(f'{BASE_URL}/machines/{r.data["id"]}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ChecklistTemplateTests(BaseTestCase):

    def test_create_template(self):
        resp = self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'FILLER', 'task': 'Clean tank', 'frequency': 'DAILY', 'sort_order': 1,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_templates(self):
        self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'FILLER', 'task': 'Task 1', 'frequency': 'DAILY',
        })
        resp = self.client.get(f'{BASE_URL}/checklist-templates/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_filter_templates(self):
        self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'FILLER', 'task': 'T1', 'frequency': 'DAILY',
        })
        self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'CAPPER', 'task': 'T2', 'frequency': 'WEEKLY',
        })
        resp = self.client.get(f'{BASE_URL}/checklist-templates/?machine_type=FILLER')
        self.assertEqual(len(resp.data), 1)

    def test_delete_template(self):
        r = self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'FILLER', 'task': 'Task', 'frequency': 'DAILY',
        })
        resp = self.client.delete(f'{BASE_URL}/checklist-templates/{r.data["id"]}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ProductionRunTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']

    def _create_run(self):
        return self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()), 'brand': 'Test', 'rated_speed': '150.00',
        })

    def test_create_run(self):
        resp = self._create_run()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['run_number'], 1)

    def test_auto_increment_run_number(self):
        self._create_run()
        resp = self._create_run()
        self.assertEqual(resp.data['run_number'], 2)

    def test_list_runs(self):
        self._create_run()
        resp = self.client.get(f'{BASE_URL}/runs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_get_run_detail(self):
        r = self._create_run()
        resp = self.client.get(f'{BASE_URL}/runs/{r.data["id"]}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_run(self):
        r = self._create_run()
        resp = self.client.patch(f'{BASE_URL}/runs/{r.data["id"]}/', {'brand': 'Updated'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['brand'], 'Updated')

    def test_complete_run(self):
        r = self._create_run()
        resp = self.client.post(f'{BASE_URL}/runs/{r.data["id"]}/complete/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'COMPLETED')

    def test_cannot_edit_completed_run(self):
        r = self._create_run()
        self.client.post(f'{BASE_URL}/runs/{r.data["id"]}/complete/')
        resp = self.client.patch(f'{BASE_URL}/runs/{r.data["id"]}/', {'brand': 'X'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_draft_plan(self):
        draft_plan = ProductionPlan.objects.create(
            company=self.company, item_code='FG-2', item_name='P2',
            planned_qty=Decimal('100'), target_start_date=date.today(),
            due_date=date.today() + timedelta(days=10), status=PlanStatus.DRAFT,
        )
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': draft_plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class HourlyLogTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def test_create_logs_bulk(self):
        logs = [
            {'time_slot': '07:00-08:00', 'time_start': '07:00', 'time_end': '08:00',
             'produced_cases': 90, 'recd_minutes': 55},
            {'time_slot': '08:00-09:00', 'time_start': '08:00', 'time_end': '09:00',
             'produced_cases': 85, 'recd_minutes': 50},
        ]
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', logs, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(resp.data), 2)

    def test_get_logs(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', [{
            'time_slot': '07:00-08:00', 'time_start': '07:00', 'time_end': '08:00',
            'produced_cases': 90, 'recd_minutes': 55,
        }], format='json')
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/logs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_log(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', [{
            'time_slot': '07:00-08:00', 'time_start': '07:00', 'time_end': '08:00',
            'produced_cases': 90, 'recd_minutes': 55,
        }], format='json')
        log_id = r.data[0]['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/logs/{log_id}/', {
            'produced_cases': 100,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['produced_cases'], 100)


class BreakdownTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def test_create_breakdown(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id, 'start_time': '2026-03-07T14:00:00',
            'end_time': '2026-03-07T14:35:00', 'breakdown_minutes': 35,
            'type': 'LINE', 'reason': 'Power cut',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_breakdowns(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id, 'start_time': '2026-03-07T14:00:00',
            'breakdown_minutes': 35, 'type': 'LINE', 'reason': 'Power cut',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/breakdowns/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_breakdown(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id, 'start_time': '2026-03-07T14:00:00',
            'breakdown_minutes': 35, 'type': 'LINE', 'reason': 'Cut',
        })
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/breakdowns/{r.data["id"]}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_machine_must_belong_to_line(self):
        r2 = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-2'})
        r3 = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Other', 'machine_type': 'FILLER', 'line_id': r2.data['id'],
        })
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': r3.data['id'], 'start_time': '2026-03-07T14:00:00',
            'breakdown_minutes': 35, 'type': 'LINE', 'reason': 'Test',
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class MaterialUsageTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def test_create_material(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_name': 'Bottle', 'opening_qty': '5000',
            'issued_qty': '2000', 'closing_qty': '4800', 'uom': 'PCS',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data[0]['wastage_qty']), 2200.0)

    def test_update_material(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_name': 'Bottle', 'opening_qty': '100',
            'issued_qty': '50', 'closing_qty': '80',
        })
        mat_id = r.data[0]['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/materials/{mat_id}/', {
            'closing_qty': '90',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(float(resp.data['wastage_qty']), 60.0)


class MachineRuntimeTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def test_create_runtime_bulk(self):
        data = [
            {'machine_type': 'FILLER', 'runtime_minutes': 420, 'downtime_minutes': 10},
            {'machine_type': 'CAPPER', 'runtime_minutes': 415, 'downtime_minutes': 15},
        ]
        resp = self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', data, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(resp.data), 2)

    def test_list_runtime(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', [
            {'machine_type': 'FILLER', 'runtime_minutes': 420, 'downtime_minutes': 10},
        ], format='json')
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class ManpowerTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def test_create_manpower(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING', 'worker_count': 12, 'supervisor': 'Ramesh',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_upsert_manpower(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING', 'worker_count': 12,
        })
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING', 'worker_count': 15,
        })
        self.assertEqual(resp.data['worker_count'], 15)


class LineClearanceTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']

    def _create_clearance(self):
        return self.client.post(f'{BASE_URL}/line-clearance/', {
            'date': str(date.today()), 'line_id': self.line_id,
            'production_plan_id': self.plan.id, 'document_id': 'PRD-OIL-FRM-15',
        })

    def test_create_clearance(self):
        resp = self._create_clearance()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(resp.data['items']), 9)
        self.assertEqual(resp.data['status'], 'DRAFT')

    def test_submit_and_approve(self):
        r = self._create_clearance()
        cl_id = r.data['id']
        items = r.data['items']

        item_updates = [{'id': i['id'], 'result': 'YES'} for i in items]
        self.client.patch(f'{BASE_URL}/line-clearance/{cl_id}/', {
            'items': item_updates, 'production_supervisor_sign': 'Supervisor',
        }, format='json')

        resp = self.client.post(f'{BASE_URL}/line-clearance/{cl_id}/submit/')
        self.assertEqual(resp.data['status'], 'SUBMITTED')

        resp = self.client.post(f'{BASE_URL}/line-clearance/{cl_id}/approve/', {
            'approved': True,
        })
        self.assertEqual(resp.data['status'], 'CLEARED')
        self.assertTrue(resp.data['qa_approved'])

    def test_submit_without_results_fails(self):
        r = self._create_clearance()
        resp = self.client.post(f'{BASE_URL}/line-clearance/{r.data["id"]}/submit/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class MachineChecklistTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/checklist-templates/', {
            'machine_type': 'FILLER', 'task': 'Clean tank', 'frequency': 'DAILY',
        })
        self.template_id = resp.data['id']

    def test_create_entry(self):
        resp = self.client.post(f'{BASE_URL}/machine-checklists/', {
            'machine_id': self.machine_id, 'template_id': self.template_id,
            'date': str(date.today()), 'status': 'OK',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_bulk_upsert(self):
        data = [{
            'machine_id': self.machine_id, 'template_id': self.template_id,
            'date': str(date.today()), 'status': 'OK', 'operator': 'Ramesh',
        }]
        resp = self.client.post(
            f'{BASE_URL}/machine-checklists/bulk/', data, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)


class WasteManagementTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = resp.data['id']

    def _create_waste(self):
        return self.client.post(f'{BASE_URL}/waste/', {
            'production_run_id': self.run_id, 'material_name': 'Bottle',
            'wastage_qty': '200', 'uom': 'PCS', 'reason': 'Dented',
        })

    def test_create_waste(self):
        resp = self._create_waste()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['wastage_approval_status'], 'PENDING')

    def test_sequential_approval(self):
        r = self._create_waste()
        wid = r.data['id']

        resp = self.client.post(f'{BASE_URL}/waste/{wid}/approve/engineer/', {'sign': 'Eng'})
        self.assertEqual(resp.data['wastage_approval_status'], 'PARTIALLY_APPROVED')

        resp = self.client.post(f'{BASE_URL}/waste/{wid}/approve/am/', {'sign': 'AM'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self.client.post(f'{BASE_URL}/waste/{wid}/approve/store/', {'sign': 'Store'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self.client.post(f'{BASE_URL}/waste/{wid}/approve/hod/', {'sign': 'HOD'})
        self.assertEqual(resp.data['wastage_approval_status'], 'FULLY_APPROVED')

    def test_cannot_skip_levels(self):
        r = self._create_waste()
        wid = r.data['id']
        resp = self.client.post(f'{BASE_URL}/waste/{wid}/approve/am/', {'sign': 'AM'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ReportTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']

    def test_daily_production_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/daily-production/?date={date.today()}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_daily_report_requires_date(self):
        resp = self.client.get(f'{BASE_URL}/reports/daily-production/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_yield_report(self):
        r = self.client.post(f'{BASE_URL}/runs/', {
            'production_plan_id': self.plan.id, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        resp = self.client.get(f'{BASE_URL}/reports/yield/{r.data["id"]}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('materials', resp.data)

    def test_line_clearance_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/line-clearance/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_analytics(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('total_runs', resp.data)
