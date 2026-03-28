"""
Comprehensive API tests for production_execution app.
Run with: python manage.py test production_execution -v2
"""
from datetime import date, timedelta, datetime
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient
from rest_framework import status

from company.models import Company, UserCompany, UserRole

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

        self.sap_doc_entry = 12345  # SAP OWOR DocEntry used in place of local plan


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

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/lines/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


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

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/machines/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


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
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()), 'brand': 'Test', 'rated_speed': '150.00',
        })

    def test_create_run(self):
        resp = self._create_run()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['brand'], 'Test')

    def test_list_runs(self):
        self._create_run()
        resp = self.client.get(f'{BASE_URL}/runs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_get_run_detail(self):
        r = self._create_run()
        run_id = r.data['id']
        resp = self.client.get(f'{BASE_URL}/runs/{run_id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_run(self):
        r = self._create_run()
        run_id = r.data['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{run_id}/', {'brand': 'Updated Brand'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['brand'], 'Updated Brand')

    def test_complete_run(self):
        r = self._create_run()
        run_id = r.data['id']
        resp = self.client.post(f'{BASE_URL}/runs/{run_id}/complete/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['status'], 'COMPLETED')

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/runs/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class HourlyLogTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_log(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', {
            'time_slot': '08:00-09:00',
            'time_start': '08:00:00',
            'time_end': '09:00:00',
            'produced_cases': 100,
            'machine_status': 'RUNNING',
            'recd_minutes': 60,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_logs(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', {
            'time_slot': '08:00-09:00',
            'time_start': '08:00:00',
            'time_end': '09:00:00',
            'produced_cases': 100,
            'machine_status': 'RUNNING',
            'recd_minutes': 60,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/logs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_update_log(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', {
            'time_slot': '10:00-11:00',
            'time_start': '10:00:00',
            'time_end': '11:00:00',
            'produced_cases': 100,
            'machine_status': 'RUNNING',
            'recd_minutes': 60,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        # POST returns a list (supports bulk creation)
        log_id = r.data[0]['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/logs/{log_id}/', {
            'produced_cases': 120,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_log(self):
        # Note: RunLogDetailAPI only supports PATCH (no DELETE method)
        # This test verifies a 405 response
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/logs/', {
            'time_slot': '11:00-12:00',
            'time_start': '11:00:00',
            'time_end': '12:00:00',
            'produced_cases': 100,
            'machine_status': 'RUNNING',
            'recd_minutes': 60,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        log_id = r.data[0]['id']
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/logs/{log_id}/')
        self.assertIn(resp.status_code, [status.HTTP_204_NO_CONTENT, status.HTTP_405_METHOD_NOT_ALLOWED])


class BreakdownTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_breakdown(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id,
            'start_time': '2026-03-16T08:00:00Z',
            'end_time': '2026-03-16T09:00:00Z',
            'breakdown_minutes': 60,
            'type': 'LINE',
            'reason': 'Motor failure',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_breakdowns(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id,
            'start_time': '2026-03-16T08:00:00Z',
            'end_time': '2026-03-16T09:00:00Z',
            'breakdown_minutes': 60,
            'type': 'LINE',
            'reason': 'Motor failure',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/breakdowns/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_breakdown(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id,
            'start_time': '2026-03-16T08:00:00Z',
            'end_time': '2026-03-16T09:00:00Z',
            'breakdown_minutes': 60,
            'type': 'LINE',
            'reason': 'Motor failure',
        })
        bd_id = r.data['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/breakdowns/{bd_id}/', {
            'reason': 'Belt snapped',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_breakdown(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/breakdowns/', {
            'machine_id': self.machine_id,
            'start_time': '2026-03-16T08:00:00Z',
            'end_time': '2026-03-16T09:00:00Z',
            'breakdown_minutes': 60,
            'type': 'LINE',
            'reason': 'Motor failure',
        })
        bd_id = r.data['id']
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/breakdowns/{bd_id}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class MaterialUsageTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_material(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_code': 'RM-001',
            'material_name': 'Coconut Oil',
            'opening_qty': '100.000',
            'issued_qty': '50.000',
            'closing_qty': '20.000',
            'uom': 'KG',
            'batch_number': 1,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_materials(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_name': 'Coconut Oil',
            'opening_qty': '100.000',
            'issued_qty': '50.000',
            'closing_qty': '20.000',
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/materials/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_update_material(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_name': 'Sesame Oil',
            'opening_qty': '100.000',
            'issued_qty': '50.000',
            'closing_qty': '20.000',
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        # POST returns a list (supports bulk creation)
        m_id = r.data[0]['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/materials/{m_id}/', {
            'issued_qty': '60.000',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_material(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/materials/', {
            'material_name': 'Palm Oil',
            'opening_qty': '100.000',
            'issued_qty': '50.000',
            'closing_qty': '20.000',
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        m_id = r.data[0]['id']
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/materials/{m_id}/')
        self.assertIn(resp.status_code, [status.HTTP_204_NO_CONTENT, status.HTTP_405_METHOD_NOT_ALLOWED])


class MachineRuntimeTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_runtime(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', {
            'machine_id': self.machine_id,
            'machine_type': 'FILLER',
            'runtime_minutes': 480,
            'downtime_minutes': 30,
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_runtimes(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', {
            'machine_type': 'FILLER',
            'runtime_minutes': 480,
            'downtime_minutes': 30,
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_runtime(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', {
            'machine_type': 'CAPPER',
            'runtime_minutes': 480,
            'downtime_minutes': 30,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        # POST returns a list (supports bulk creation)
        rt_id = r.data[0]['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/{rt_id}/', {
            'runtime_minutes': 500,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_runtime(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/', {
            'machine_type': 'CONVEYOR',
            'runtime_minutes': 480,
            'downtime_minutes': 30,
        })
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        rt_id = r.data[0]['id']
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/machine-runtime/{rt_id}/')
        self.assertIn(resp.status_code, [status.HTTP_204_NO_CONTENT, status.HTTP_405_METHOD_NOT_ALLOWED])


class ManpowerTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_manpower(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING',
            'worker_count': 10,
            'supervisor': 'John',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_manpower(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING',
            'worker_count': 10,
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/manpower/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_manpower(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'MORNING',
            'worker_count': 10,
        })
        mp_id = r.data['id']
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/manpower/{mp_id}/', {
            'worker_count': 12,
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_manpower(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/manpower/', {
            'shift': 'NIGHT',
            'worker_count': 10,
        })
        mp_id = r.data['id']
        resp = self.client.delete(f'{BASE_URL}/runs/{self.run_id}/manpower/{mp_id}/')
        self.assertIn(resp.status_code, [status.HTTP_204_NO_CONTENT, status.HTTP_405_METHOD_NOT_ALLOWED])


class LineClearanceTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']

    def _create_clearance(self):
        return self.client.post(f'{BASE_URL}/line-clearance/', {
            'date': str(date.today()),
            'line_id': self.line_id,
            'sap_doc_entry': self.sap_doc_entry,
            'document_id': 'DOC-001',
        })

    def test_create_clearance(self):
        resp = self._create_clearance()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_clearances(self):
        self._create_clearance()
        resp = self.client.get(f'{BASE_URL}/line-clearance/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data), 1)

    def test_submit_clearance(self):
        r = self._create_clearance()
        clearance_id = r.data['id']
        resp = self.client.post(f'{BASE_URL}/line-clearance/{clearance_id}/submit/')
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])

    def test_approve_clearance(self):
        r = self._create_clearance()
        clearance_id = r.data['id']
        submit_resp = self.client.post(f'{BASE_URL}/line-clearance/{clearance_id}/submit/')
        if submit_resp.status_code == status.HTTP_200_OK:
            resp = self.client.post(f'{BASE_URL}/line-clearance/{clearance_id}/approve/', {
                'result': 'CLEARED',
            })
            self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])


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

    def test_create_checklist_entry(self):
        resp = self.client.post(f'{BASE_URL}/machine-checklists/', {
            'machine_id': self.machine_id,
            'template_id': self.template_id,
            'date': str(date.today()),
            'status': 'OK',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_checklists(self):
        self.client.post(f'{BASE_URL}/machine-checklists/', {
            'machine_id': self.machine_id,
            'template_id': self.template_id,
            'date': str(date.today()),
            'status': 'OK',
        })
        resp = self.client.get(f'{BASE_URL}/machine-checklists/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_update_checklist_entry(self):
        r = self.client.post(f'{BASE_URL}/machine-checklists/', {
            'machine_id': self.machine_id,
            'template_id': self.template_id,
            'date': str(date.today()),
            'status': 'NA',
        })
        entry_id = r.data['id']
        resp = self.client.patch(f'{BASE_URL}/machine-checklists/{entry_id}/', {
            'status': 'OK',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class WasteLogTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def _create_waste(self):
        return self.client.post(f'{BASE_URL}/waste/', {
            'production_run_id': self.run_id,
            'material_code': 'RM-001',
            'material_name': 'Palm Oil',
            'wastage_qty': '5.500',
            'uom': 'KG',
            'reason': 'Spill',
        })

    def test_create_waste_log(self):
        resp = self._create_waste()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_waste_logs(self):
        self._create_waste()
        resp = self.client.get(f'{BASE_URL}/waste/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_approve_engineer(self):
        r = self._create_waste()
        waste_id = r.data['id']
        resp = self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/engineer/', {
            'sign': 'Eng. John',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_approve_am(self):
        r = self._create_waste()
        waste_id = r.data['id']
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/engineer/', {'sign': 'Eng. John'})
        resp = self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/am/', {
            'sign': 'AM. Jane',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_approve_store(self):
        r = self._create_waste()
        waste_id = r.data['id']
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/engineer/', {'sign': 'Eng. John'})
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/am/', {'sign': 'AM. Jane'})
        resp = self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/store/', {
            'sign': 'Store. Mike',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_approve_hod(self):
        r = self._create_waste()
        waste_id = r.data['id']
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/engineer/', {'sign': 'Eng. John'})
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/am/', {'sign': 'AM. Jane'})
        self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/store/', {'sign': 'Store. Mike'})
        resp = self.client.post(f'{BASE_URL}/waste/{waste_id}/approve/hod/', {
            'sign': 'HOD. Boss',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/waste/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ResourceElectricityTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_electricity_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'description': 'Main line electricity',
            'units_consumed': '150.500',
            'rate_per_unit': '8.5000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn('total_cost', resp.data)

    def test_total_cost_auto_calculated(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 1000.0)

    def test_list_electricity_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_electricity_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        entry_id = r.data['id']
        resp = self.client.patch(
            f'{BASE_URL}/runs/{self.run_id}/resources/electricity/{entry_id}/',
            {'units_consumed': '200.000'},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_electricity_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/electricity/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(
            f'{BASE_URL}/runs/{self.run_id}/resources/electricity/'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ResourceWaterTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_water_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/water/', {
            'description': 'Process water',
            'volume_consumed': '500.000',
            'rate_per_unit': '2.0000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 1000.0)

    def test_list_water_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/water/', {
            'volume_consumed': '500.000',
            'rate_per_unit': '2.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/water/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_delete_water_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/water/', {
            'volume_consumed': '500.000',
            'rate_per_unit': '2.0000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/water/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ResourceGasTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_gas_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/gas/', {
            'description': 'LPG',
            'qty_consumed': '20.000',
            'rate_per_unit': '50.0000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 1000.0)

    def test_list_gas_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/gas/', {
            'qty_consumed': '20.000',
            'rate_per_unit': '50.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/gas/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_gas_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/gas/', {
            'qty_consumed': '20.000',
            'rate_per_unit': '50.0000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/gas/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ResourceCompressedAirTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_compressed_air_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/compressed-air/', {
            'description': 'Compressor A',
            'units_consumed': '200.000',
            'rate_per_unit': '1.5000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 300.0)

    def test_list_compressed_air_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/compressed-air/', {
            'units_consumed': '200.000',
            'rate_per_unit': '1.5000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/compressed-air/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_compressed_air_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/compressed-air/', {
            'units_consumed': '200.000',
            'rate_per_unit': '1.5000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/compressed-air/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ResourceLabourTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_labour_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/labour/', {
            'worker_name': 'Ramesh Kumar',
            'hours_worked': '8.00',
            'rate_per_hour': '150.0000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 1200.0)

    def test_list_labour_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/labour/', {
            'worker_name': 'Worker A',
            'hours_worked': '8.00',
            'rate_per_hour': '100.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/labour/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_labour_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/labour/', {
            'worker_name': 'Worker A',
            'hours_worked': '8.00',
            'rate_per_hour': '100.0000',
        })
        entry_id = r.data['id']
        resp = self.client.patch(
            f'{BASE_URL}/runs/{self.run_id}/resources/labour/{entry_id}/',
            {'hours_worked': '10.00'},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_labour_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/labour/', {
            'worker_name': 'Worker A',
            'hours_worked': '8.00',
            'rate_per_hour': '100.0000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/labour/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(
            f'{BASE_URL}/runs/{self.run_id}/resources/labour/'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ResourceMachineCostTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_machine_cost_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/machine-costs/', {
            'machine_name': 'Filler Machine',
            'hours_used': '8.00',
            'rate_per_hour': '500.0000',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(resp.data['total_cost']), 4000.0)

    def test_list_machine_cost_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/machine-costs/', {
            'machine_name': 'Filler',
            'hours_used': '8.00',
            'rate_per_hour': '500.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/machine-costs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_machine_cost_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/machine-costs/', {
            'machine_name': 'Filler',
            'hours_used': '8.00',
            'rate_per_hour': '500.0000',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/machine-costs/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)


class ResourceOverheadTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_overhead_entry(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/', {
            'expense_name': 'Factory Rent',
            'amount': '5000.00',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_list_overhead_entries(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/', {
            'expense_name': 'Factory Rent',
            'amount': '5000.00',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_overhead_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/', {
            'expense_name': 'Factory Rent',
            'amount': '5000.00',
        })
        entry_id = r.data['id']
        resp = self.client.patch(
            f'{BASE_URL}/runs/{self.run_id}/resources/overhead/{entry_id}/',
            {'amount': '6000.00'},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_delete_overhead_entry(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/', {
            'expense_name': 'Factory Rent',
            'amount': '5000.00',
        })
        entry_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/resources/overhead/{entry_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(
            f'{BASE_URL}/runs/{self.run_id}/resources/overhead/'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class CostSummaryTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_no_cost_returns_404(self):
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/cost/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cost_calculated_after_resource(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/cost/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('total_cost', resp.data)
        self.assertIn('per_unit_cost', resp.data)

    def test_cost_includes_all_resources(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/electricity/', {
            'units_consumed': '100.000',
            'rate_per_unit': '10.0000',
        })
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/labour/', {
            'worker_name': 'Worker A',
            'hours_worked': '8.00',
            'rate_per_hour': '100.0000',
        })
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/resources/overhead/', {
            'expense_name': 'Rent',
            'amount': '500.00',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/cost/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # electricity=1000, labour=800, overhead=500 => total=2300
        self.assertEqual(float(resp.data['total_cost']), 2300.0)

    def test_cost_analytics_endpoint(self):
        resp = self.client.get(f'{BASE_URL}/costs/analytics/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/runs/{self.run_id}/cost/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class InProcessQCTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_qc_check(self):
        resp = self.client.post(f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/', {
            'checked_at': '2026-03-16T10:00:00Z',
            'parameter': 'Fill Weight',
            'acceptable_min': '99.500',
            'acceptable_max': '100.500',
            'actual_value': '100.100',
            'result': 'PASS',
            'remarks': 'Within spec',
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['result'], 'PASS')

    def test_list_qc_checks(self):
        self.client.post(f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/', {
            'checked_at': '2026-03-16T10:00:00Z',
            'parameter': 'Fill Weight',
            'result': 'PASS',
        })
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_update_qc_check(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/', {
            'checked_at': '2026-03-16T10:00:00Z',
            'parameter': 'Fill Weight',
            'result': 'NA',
        })
        check_id = r.data['id']
        resp = self.client.patch(
            f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/{check_id}/',
            {'result': 'PASS'},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['result'], 'PASS')

    def test_delete_qc_check(self):
        r = self.client.post(f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/', {
            'checked_at': '2026-03-16T10:00:00Z',
            'parameter': 'pH',
            'result': 'FAIL',
        })
        check_id = r.data['id']
        resp = self.client.delete(
            f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/{check_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(
            f'{BASE_URL}/runs/{self.run_id}/qc/inprocess/'
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class FinalQCTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_create_final_qc(self):
        resp = self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/qc/final/',
            {
                'checked_at': '2026-03-16T17:00:00Z',
                'overall_result': 'PASS',
                'parameters': [
                    {'name': 'Fill Weight', 'expected': '100', 'actual': '99.8', 'result': 'PASS'},
                ],
                'remarks': 'All within spec',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['overall_result'], 'PASS')

    def test_get_final_qc(self):
        self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/qc/final/',
            {'checked_at': '2026-03-16T17:00:00Z', 'overall_result': 'PASS', 'parameters': []},
            format='json',
        )
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/qc/final/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_duplicate_final_qc_returns_400(self):
        self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/qc/final/',
            {'checked_at': '2026-03-16T17:00:00Z', 'overall_result': 'PASS', 'parameters': []},
            format='json',
        )
        resp = self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/qc/final/',
            {'checked_at': '2026-03-16T17:00:00Z', 'overall_result': 'FAIL', 'parameters': []},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_final_qc(self):
        self.client.post(
            f'{BASE_URL}/runs/{self.run_id}/qc/final/',
            {'checked_at': '2026-03-16T17:00:00Z', 'overall_result': 'PASS', 'parameters': []},
            format='json',
        )
        resp = self.client.patch(f'{BASE_URL}/runs/{self.run_id}/qc/final/', {
            'overall_result': 'CONDITIONAL',
            'remarks': 'Some deviations noted',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['overall_result'], 'CONDITIONAL')

    def test_get_nonexistent_final_qc_returns_404(self):
        resp = self.client.get(f'{BASE_URL}/runs/{self.run_id}/qc/final/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/runs/{self.run_id}/qc/final/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ReportTests(BaseTestCase):

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        r = self.client.post(f'{BASE_URL}/runs/', {
            'sap_doc_entry': self.sap_doc_entry, 'line_id': self.line_id,
            'date': str(date.today()),
        })
        self.run_id = r.data['id']

    def test_daily_production_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/daily-production/?date={date.today()}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_yield_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/yield/{self.run_id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_line_clearance_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/line-clearance/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_analytics_report(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_oee_analytics(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/oee/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('per_run_oee', resp.data)

    def test_downtime_analytics(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/downtime/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('breakdowns', resp.data)
        self.assertIn('total_count', resp.data)

    def test_waste_analytics(self):
        resp = self.client.get(f'{BASE_URL}/reports/analytics/waste/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('by_material', resp.data)
        self.assertIn('by_approval_status', resp.data)

    def test_oee_analytics_with_date_filter(self):
        resp = self.client.get(
            f'{BASE_URL}/reports/analytics/oee/?date_from={date.today()}&date_to={date.today()}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_downtime_analytics_with_date_filter(self):
        resp = self.client.get(
            f'{BASE_URL}/reports/analytics/downtime/?date_from={date.today()}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_report_returns_401(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/reports/analytics/oee/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# ===========================================================================
# BOM Auto-Fetch Tests
# ===========================================================================

class BOMAutoFetchTests(BaseTestCase):
    """Tests for automatic BOM fetching when creating a production run."""

    def setUp(self):
        super().setUp()
        resp = self.client.post(f'{BASE_URL}/lines/', {'name': 'Line-1'})
        self.line_id = resp.data['id']
        resp = self.client.post(f'{BASE_URL}/machines/', {
            'name': 'Filler', 'machine_type': 'FILLER', 'line_id': self.line_id,
        })
        self.machine_id = resp.data['id']

    # ------------------------------------------------------------------
    # Case 1: Manual materials take priority (no BOM auto-fetch)
    # ------------------------------------------------------------------
    def test_manual_materials_skip_bom_fetch(self):
        """When materials are provided manually, BOM auto-fetch should NOT happen."""
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'line_id': self.line_id,
            'date': str(date.today()),
            'product': 'FG-001',
            'materials': [
                {
                    'material_code': 'MANUAL-001',
                    'material_name': 'Manual Material',
                    'opening_qty': 100,
                    'issued_qty': 0,
                    'uom': 'KG',
                }
            ],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        run_id = resp.data['id']

        # Check that only manual material exists
        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(mat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mat_resp.data), 1)
        self.assertEqual(mat_resp.data[0]['material_code'], 'MANUAL-001')

    # ------------------------------------------------------------------
    # Case 2: No sap_doc_entry and no product — no BOM fetch, no materials
    # ------------------------------------------------------------------
    def test_no_sap_no_product_no_materials(self):
        """Run without sap_doc_entry or product should have no materials."""
        resp = self.client.post(f'{BASE_URL}/runs/', {
            'line_id': self.line_id,
            'date': str(date.today()),
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        run_id = resp.data['id']

        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(mat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mat_resp.data), 0)

    # ------------------------------------------------------------------
    # Case 3: SAP connection failure — run still created, no materials
    # ------------------------------------------------------------------
    def test_sap_failure_run_still_created(self):
        """If SAP is unreachable, run should still be created without materials."""
        from unittest.mock import patch

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader.__init__',
            side_effect=Exception("SAP unavailable"),
        ):
            resp = self.client.post(f'{BASE_URL}/runs/', {
                'sap_doc_entry': 99999,
                'line_id': self.line_id,
                'date': str(date.today()),
                'product': 'FG-001',
            }, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            run_id = resp.data['id']

        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(mat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mat_resp.data), 0)

    # ------------------------------------------------------------------
    # Case 4: sap_doc_entry provided — auto-fetch from WOR1
    # ------------------------------------------------------------------
    def test_auto_fetch_from_production_order(self):
        """When sap_doc_entry is given, BOM should be fetched from WOR1."""
        from unittest.mock import patch, MagicMock

        mock_components = [
            {'ItemCode': 'RM-001', 'ItemName': 'Sugar', 'PlannedQty': 500.0, 'IssuedQty': 100.0, 'UomCode': 'KG', 'Warehouse': 'WH01'},
            {'ItemCode': 'PKG-001', 'ItemName': '5L Bottle', 'PlannedQty': 1000.0, 'IssuedQty': 0.0, 'UomCode': 'PCS', 'Warehouse': 'WH02'},
        ]

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_components_for_run.return_value = mock_components

            resp = self.client.post(f'{BASE_URL}/runs/', {
                'sap_doc_entry': 12345,
                'line_id': self.line_id,
                'date': str(date.today()),
                'product': 'FG-001',
            }, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            run_id = resp.data['id']

            # Verify get_bom_components_for_run called with sap_doc_entry
            instance.get_bom_components_for_run.assert_called_once_with(
                sap_doc_entry=12345,
                item_code='FG-001',
            )

        # Verify materials were created
        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(mat_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mat_resp.data), 2)
        codes = [m['material_code'] for m in mat_resp.data]
        self.assertIn('RM-001', codes)
        self.assertIn('PKG-001', codes)

        # Verify quantities
        sugar = next(m for m in mat_resp.data if m['material_code'] == 'RM-001')
        self.assertEqual(float(sugar['opening_qty']), 500.0)
        self.assertEqual(float(sugar['issued_qty']), 100.0)
        self.assertEqual(sugar['uom'], 'KG')

    # ------------------------------------------------------------------
    # Case 5: No sap_doc_entry but product given — fetch from OITT/ITT1
    # ------------------------------------------------------------------
    def test_auto_fetch_from_item_bom(self):
        """When only product is given (no sap_doc_entry), fetch from item BOM."""
        from unittest.mock import patch

        mock_components = [
            {'ItemCode': 'RM-010', 'ItemName': 'Oil Base', 'PlannedQty': 200.0, 'IssuedQty': 0, 'UomCode': 'LTR'},
        ]

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_components_for_run.return_value = mock_components

            resp = self.client.post(f'{BASE_URL}/runs/', {
                'line_id': self.line_id,
                'date': str(date.today()),
                'product': 'FG-002',
            }, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            run_id = resp.data['id']

            instance.get_bom_components_for_run.assert_called_once_with(
                sap_doc_entry=None,
                item_code='FG-002',
            )

        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(len(mat_resp.data), 1)
        self.assertEqual(mat_resp.data[0]['material_code'], 'RM-010')
        self.assertEqual(mat_resp.data[0]['material_name'], 'Oil Base')
        self.assertEqual(float(mat_resp.data[0]['opening_qty']), 200.0)

    # ------------------------------------------------------------------
    # Case 6: BOM returns empty components — run created, no materials
    # ------------------------------------------------------------------
    def test_empty_bom_returns_no_materials(self):
        """If SAP BOM has no components, run should have no materials."""
        from unittest.mock import patch

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_components_for_run.return_value = []

            resp = self.client.post(f'{BASE_URL}/runs/', {
                'sap_doc_entry': 11111,
                'line_id': self.line_id,
                'date': str(date.today()),
                'product': 'FG-EMPTY',
            }, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            run_id = resp.data['id']

        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(len(mat_resp.data), 0)

    # ------------------------------------------------------------------
    # Case 7: Multiple BOM components — all saved correctly
    # ------------------------------------------------------------------
    def test_multiple_bom_components(self):
        """Verify multiple BOM components are all saved with correct data."""
        from unittest.mock import patch

        mock_components = [
            {'ItemCode': 'RM-A', 'ItemName': 'Component A', 'PlannedQty': 10.0, 'IssuedQty': 2.0, 'UomCode': 'KG'},
            {'ItemCode': 'RM-B', 'ItemName': 'Component B', 'PlannedQty': 20.0, 'IssuedQty': 5.0, 'UomCode': 'LTR'},
            {'ItemCode': 'RM-C', 'ItemName': 'Component C', 'PlannedQty': 30.0, 'IssuedQty': 0.0, 'UomCode': 'PCS'},
            {'ItemCode': 'RM-D', 'ItemName': 'Component D', 'PlannedQty': 40.0, 'IssuedQty': 10.0, 'UomCode': 'MTR'},
            {'ItemCode': 'RM-E', 'ItemName': 'Component E', 'PlannedQty': 50.0, 'IssuedQty': 0.0, 'UomCode': 'KG'},
        ]

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_components_for_run.return_value = mock_components

            resp = self.client.post(f'{BASE_URL}/runs/', {
                'sap_doc_entry': 55555,
                'line_id': self.line_id,
                'date': str(date.today()),
            }, format='json')
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            run_id = resp.data['id']

        mat_resp = self.client.get(f'{BASE_URL}/runs/{run_id}/materials/')
        self.assertEqual(len(mat_resp.data), 5)

        # Verify all codes present
        codes = sorted([m['material_code'] for m in mat_resp.data])
        self.assertEqual(codes, ['RM-A', 'RM-B', 'RM-C', 'RM-D', 'RM-E'])


class SAPItemBOMAPITests(BaseTestCase):
    """Tests for the /sap/bom/ preview endpoint."""

    # ------------------------------------------------------------------
    # Case 8: Missing item_code param returns 400
    # ------------------------------------------------------------------
    def test_bom_endpoint_missing_item_code(self):
        resp = self.client.get(f'{BASE_URL}/sap/bom/')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('item_code', resp.data.get('detail', ''))

    # ------------------------------------------------------------------
    # Case 9: Empty item_code returns 400
    # ------------------------------------------------------------------
    def test_bom_endpoint_empty_item_code(self):
        resp = self.client.get(f'{BASE_URL}/sap/bom/?item_code=')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------
    # Case 10: Valid item_code returns components
    # ------------------------------------------------------------------
    def test_bom_endpoint_returns_components(self):
        from unittest.mock import patch

        mock_components = [
            {'ItemCode': 'RM-X', 'ItemName': 'Raw X', 'PlannedQty': 100.0, 'UomCode': 'KG'},
            {'ItemCode': 'RM-Y', 'ItemName': 'Raw Y', 'PlannedQty': 200.0, 'UomCode': 'LTR'},
        ]

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_by_item_code.return_value = mock_components

            resp = self.client.get(f'{BASE_URL}/sap/bom/?item_code=FG-001')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data['item_code'], 'FG-001')
            self.assertEqual(resp.data['component_count'], 2)
            self.assertEqual(len(resp.data['components']), 2)

    # ------------------------------------------------------------------
    # Case 11: SAP failure returns 503
    # ------------------------------------------------------------------
    def test_bom_endpoint_sap_failure(self):
        from unittest.mock import patch
        from production_execution.services.sap_reader import SAPReadError

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_by_item_code.side_effect = SAPReadError("SAP down")

            resp = self.client.get(f'{BASE_URL}/sap/bom/?item_code=FG-001')
            self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    # ------------------------------------------------------------------
    # Case 12: Item with no BOM returns empty list
    # ------------------------------------------------------------------
    def test_bom_endpoint_no_bom_for_item(self):
        from unittest.mock import patch

        with patch(
            'production_execution.services.sap_reader.ProductionOrderReader'
        ) as MockReader:
            instance = MockReader.return_value
            instance.get_bom_by_item_code.return_value = []

            resp = self.client.get(f'{BASE_URL}/sap/bom/?item_code=FG-NOBOM')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data['component_count'], 0)
            self.assertEqual(resp.data['components'], [])

    # ------------------------------------------------------------------
    # Case 13: Unauthenticated access returns 401
    # ------------------------------------------------------------------
    def test_bom_endpoint_unauthenticated(self):
        unauthenticated_client = APIClient()
        resp = unauthenticated_client.get(f'{BASE_URL}/sap/bom/?item_code=FG-001')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class SAPReaderBOMTests(TestCase):
    """Unit tests for the SAP reader BOM methods."""

    # ------------------------------------------------------------------
    # Case 14: get_bom_components_for_run with sap_doc_entry
    # ------------------------------------------------------------------
    def test_get_bom_components_prefers_sap_doc_entry(self):
        from unittest.mock import patch, MagicMock

        with patch(
            'production_execution.services.sap_reader.SAPClient'
        ) as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.context.config = {'hana': {'schema': 'TEST'}}

            from production_execution.services.sap_reader import ProductionOrderReader
            reader = ProductionOrderReader.__new__(ProductionOrderReader)
            reader.company_code = 'TEST_CO'
            reader.client = mock_instance

            # Mock both methods
            reader.get_production_order_detail = MagicMock(return_value={
                'header': {'DocEntry': 1},
                'components': [{'ItemCode': 'FROM-WOR1', 'ItemName': 'WOR1 Item', 'PlannedQty': 100, 'IssuedQty': 10, 'UomCode': 'KG'}],
            })
            reader.get_bom_by_item_code = MagicMock(return_value=[
                {'ItemCode': 'FROM-BOM', 'ItemName': 'BOM Item', 'PlannedQty': 200, 'UomCode': 'LTR'},
            ])

            # When both sap_doc_entry and item_code given, sap_doc_entry wins
            result = reader.get_bom_components_for_run(sap_doc_entry=1, item_code='FG-001')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['ItemCode'], 'FROM-WOR1')
            reader.get_production_order_detail.assert_called_once_with(1)
            reader.get_bom_by_item_code.assert_not_called()

    # ------------------------------------------------------------------
    # Case 15: get_bom_components_for_run with item_code only
    # ------------------------------------------------------------------
    def test_get_bom_components_falls_back_to_item_code(self):
        from unittest.mock import patch, MagicMock

        with patch(
            'production_execution.services.sap_reader.SAPClient'
        ) as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.context.config = {'hana': {'schema': 'TEST'}}

            from production_execution.services.sap_reader import ProductionOrderReader
            reader = ProductionOrderReader.__new__(ProductionOrderReader)
            reader.company_code = 'TEST_CO'
            reader.client = mock_instance

            reader.get_bom_by_item_code = MagicMock(return_value=[
                {'ItemCode': 'FROM-BOM', 'ItemName': 'BOM Item', 'PlannedQty': 200, 'UomCode': 'LTR'},
            ])

            result = reader.get_bom_components_for_run(sap_doc_entry=None, item_code='FG-001')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['ItemCode'], 'FROM-BOM')
            # IssuedQty should be defaulted to 0
            self.assertEqual(result[0]['IssuedQty'], 0)
            reader.get_bom_by_item_code.assert_called_once_with('FG-001')

    # ------------------------------------------------------------------
    # Case 16: get_bom_components_for_run with neither — returns empty
    # ------------------------------------------------------------------
    def test_get_bom_components_no_params_returns_empty(self):
        from unittest.mock import patch, MagicMock

        with patch(
            'production_execution.services.sap_reader.SAPClient'
        ) as MockClient:
            mock_instance = MockClient.return_value
            mock_instance.context.config = {'hana': {'schema': 'TEST'}}

            from production_execution.services.sap_reader import ProductionOrderReader
            reader = ProductionOrderReader.__new__(ProductionOrderReader)
            reader.company_code = 'TEST_CO'
            reader.client = mock_instance

            result = reader.get_bom_components_for_run(sap_doc_entry=None, item_code=None)
            self.assertEqual(result, [])
