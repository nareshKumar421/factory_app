"""
Tests for the Warehouse Ops (WMS) backend CRUD API.

Covers the full storage-adapter contract the frontend relies on: list, get,
create (upsert), bulk create, partial update (merge), delete, unknown-collection
handling, authentication, and per-company isolation.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from wms.models import Warehouse, Zone

User = get_user_model()


def warehouse_doc(record_id='wh-1', name='Main', code='WH1'):
    """A representative camelCase warehouse document, like the frontend sends."""
    return {
        'id': record_id,
        'code': code,
        'name': name,
        'description': '',
        'enabled': True,
        'columns': 5,
        'rows': 4,
        'levels': 1,
        'namingScheme': {
            'columnStyle': 'LETTERS', 'rowStyle': 'NUMBERS',
            'levelStyle': 'NUMBERS', 'prefix': '', 'separator': '-',
        },
        'createdAt': '2026-06-29T10:00:00.000Z',
        'updatedAt': '2026-06-29T10:00:00.000Z',
    }


class WmsApiBaseTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = UserRole.objects.create(name='WMS Operator')

        cls.company = Company.objects.create(name='Company A', code='TC001')
        cls.user = User.objects.create_user(
            email='a@example.com', password='pw', full_name='User A',
            employee_code='EMPA',
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=cls.role,
            is_default=True, is_active=True,
        )

        cls.other_company = Company.objects.create(name='Company B', code='TC002')
        cls.other_user = User.objects.create_user(
            email='b@example.com', password='pw', full_name='User B',
            employee_code='EMPB',
        )
        UserCompany.objects.create(
            user=cls.other_user, company=cls.other_company, role=cls.role,
            is_default=True, is_active=True,
        )

    def auth_client(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        return client

    def url(self, *parts):
        return '/api/v1/wms/' + '/'.join(parts) + '/'


class WmsAuthTests(WmsApiBaseTest):
    def test_unauthenticated_is_rejected(self):
        response = APIClient().get(self.url('warehouses'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_company_header_is_rejected(self):
        client = self.auth_client()
        response = client.get(self.url('warehouses'))  # no Company-Code header
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class WmsCrudTests(WmsApiBaseTest):
    def setUp(self):
        self.client = self.auth_client()

    def test_create_then_persists_in_db_and_returns_document(self):
        response = self.client.post(
            self.url('warehouses'), warehouse_doc(), format='json',
            HTTP_COMPANY_CODE='TC001',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['id'], 'wh-1')
        self.assertEqual(response.data['name'], 'Main')
        # Round-trips nested structure untouched.
        self.assertEqual(response.data['namingScheme']['separator'], '-')

        row = Warehouse.objects.get(company=self.company, record_id='wh-1')
        self.assertEqual(row.data['code'], 'WH1')

    def test_list_returns_created_records(self):
        self.client.post(self.url('warehouses'), warehouse_doc('wh-1'),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('warehouses'), warehouse_doc('wh-2', 'Second', 'WH2'),
                         format='json', HTTP_COMPANY_CODE='TC001')

        response = self.client.get(self.url('warehouses'), HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {r['id'] for r in response.data}
        self.assertEqual(ids, {'wh-1', 'wh-2'})

    def test_get_single_record(self):
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        response = self.client.get(self.url('warehouses', 'wh-1'),
                                   HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], 'wh-1')

    def test_get_missing_record_returns_404(self):
        response = self.client.get(self.url('warehouses', 'nope'),
                                   HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_patch_merges_fields_and_preserves_id(self):
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        response = self.client.patch(
            self.url('warehouses', 'wh-1'),
            {'name': 'Renamed', 'updatedAt': '2026-06-29T11:00:00.000Z'},
            format='json', HTTP_COMPANY_CODE='TC001',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'Renamed')
        self.assertEqual(response.data['id'], 'wh-1')
        # Untouched fields survive the merge.
        self.assertEqual(response.data['code'], 'WH1')
        self.assertEqual(response.data['columns'], 5)

    def test_patch_missing_record_returns_404(self):
        response = self.client.patch(self.url('warehouses', 'nope'),
                                     {'name': 'x'}, format='json',
                                     HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_removes_record(self):
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        response = self.client.delete(self.url('warehouses', 'wh-1'),
                                      HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Warehouse.objects.filter(record_id='wh-1').exists())

    def test_create_is_idempotent_upsert(self):
        """POSTing the same id twice updates, never duplicates (matches put())."""
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('warehouses'), warehouse_doc(name='Updated'),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(Warehouse.objects.filter(record_id='wh-1').count(), 1)
        row = Warehouse.objects.get(record_id='wh-1')
        self.assertEqual(row.data['name'], 'Updated')

    def test_bulk_create(self):
        records = [
            {'id': 'z-1', 'warehouseId': 'wh-1', 'code': 'Z1', 'name': 'Zone 1'},
            {'id': 'z-2', 'warehouseId': 'wh-1', 'code': 'Z2', 'name': 'Zone 2'},
        ]
        response = self.client.post(self.url('zones', 'bulk'), records,
                                    format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(Zone.objects.filter(company=self.company).count(), 2)

    def test_unknown_collection_returns_404(self):
        response = self.client.get(self.url('bogus'), HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_settings_singleton_roundtrip(self):
        doc = {'id': 'wms-settings', 'masterEnabled': True,
               'storageAdapter': 'api', 'updatedAt': '2026-06-29T10:00:00.000Z'}
        self.client.post(self.url('settings'), doc, format='json',
                         HTTP_COMPANY_CODE='TC001')
        response = self.client.get(self.url('settings', 'wms-settings'),
                                   HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['masterEnabled'])


class WmsCompanyIsolationTests(WmsApiBaseTest):
    def test_records_are_scoped_per_company(self):
        # Company A creates a warehouse.
        a = self.auth_client(self.user)
        a.post(self.url('warehouses'), warehouse_doc('wh-a', 'A WH'),
               format='json', HTTP_COMPANY_CODE='TC001')

        # Company B sees none of A's data and can hold the same id independently.
        b = self.auth_client(self.other_user)
        list_b = b.get(self.url('warehouses'), HTTP_COMPANY_CODE='TC002')
        self.assertEqual(list_b.data, [])

        get_b = b.get(self.url('warehouses', 'wh-a'), HTTP_COMPANY_CODE='TC002')
        self.assertEqual(get_b.status_code, status.HTTP_404_NOT_FOUND)

        b.post(self.url('warehouses'), warehouse_doc('wh-a', 'B WH'),
               format='json', HTTP_COMPANY_CODE='TC002')
        # Each company keeps its own document under the same id.
        self.assertEqual(
            Warehouse.objects.get(company=self.company, record_id='wh-a').data['name'],
            'A WH',
        )
        self.assertEqual(
            Warehouse.objects.get(company=self.other_company, record_id='wh-a').data['name'],
            'B WH',
        )
