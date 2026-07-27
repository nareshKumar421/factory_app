"""
Tests for the Warehouse Ops (WMS) backend CRUD API.

Covers the full storage-adapter contract the frontend relies on: list, get,
create (upsert), bulk create, partial update (merge), delete, unknown-collection
handling, authentication, and per-company isolation.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from wms.models import CellPurpose, Location, Pallet, Warehouse, Zone

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

        # Access groups are created by migration 0003_wms_access_groups.
        cls.admin_group = Group.objects.get(name='WMS Admin')
        cls.operator_group = Group.objects.get(name='WMS Operator')

        cls.company = Company.objects.create(name='Company A', code='TC001')
        # Admin = WMS Admin group; full write access to every collection.
        cls.user = User.objects.create_user(
            email='a@example.com', password='pw', full_name='User A',
            employee_code='EMPA',
        )
        cls.user.groups.add(cls.admin_group)
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=cls.role,
            is_default=True, is_active=True,
        )

        # Operator = WMS Operator group; only operational writes (pallets/inventory/movements).
        cls.operator = User.objects.create_user(
            email='op@example.com', password='pw', full_name='Operator',
            employee_code='EMPOP',
        )
        cls.operator.groups.add(cls.operator_group)
        UserCompany.objects.create(
            user=cls.operator, company=cls.company, role=cls.role,
            is_default=False, is_active=True,
        )

        cls.other_company = Company.objects.create(name='Company B', code='TC002')
        cls.other_user = User.objects.create_user(
            email='b@example.com', password='pw', full_name='User B',
            employee_code='EMPB',
        )
        cls.other_user.groups.add(cls.admin_group)
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

    def test_delete_soft_deletes_record(self):
        """DELETE flags the row ``is_deleted`` rather than removing it: the row
        is retained (recoverable) but drops out of every read."""
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        response = self.client.delete(self.url('warehouses', 'wh-1'),
                                      HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Row survives, flagged deleted with a timestamp.
        obj = Warehouse.objects.get(record_id='wh-1')
        self.assertTrue(obj.is_deleted)
        self.assertIsNotNone(obj.deleted_at)

        # …but it is gone from both the detail read and the list.
        detail = self.client.get(self.url('warehouses', 'wh-1'),
                                 HTTP_COMPANY_CODE='TC001')
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)
        listing = self.client.get(self.url('warehouses'), HTTP_COMPANY_CODE='TC001')
        self.assertEqual(listing.json(), [])

    def test_recreating_soft_deleted_record_revives_it(self):
        """POSTing the same id back revives the soft-deleted row (clears flags)."""
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.delete(self.url('warehouses', 'wh-1'), HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('warehouses'), warehouse_doc(),
                         format='json', HTTP_COMPANY_CODE='TC001')
        obj = Warehouse.objects.get(record_id='wh-1')
        self.assertFalse(obj.is_deleted)
        self.assertIsNone(obj.deleted_at)
        listing = self.client.get(self.url('warehouses'), HTTP_COMPANY_CODE='TC001')
        self.assertEqual(len(listing.json()), 1)

    def test_delete_warehouse_cascades_to_its_scoped_rows(self):
        """Deleting a warehouse soft-deletes its zones/purposes/locations, but
        never another warehouse's rows or unrelated collections (orphan-row bug
        fix). Deleted rows survive in the DB but disappear from reads."""
        self.client.post(self.url('warehouses'), warehouse_doc('wh-1'),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('warehouses'), warehouse_doc('wh-2', 'Second', 'WH2'),
                         format='json', HTTP_COMPANY_CODE='TC001')
        # Rows scoped to wh-1 (deleted) and wh-2 (kept).
        self.client.post(self.url('zones', 'bulk'),
                         [{'id': 'z-1', 'warehouseId': 'wh-1', 'code': 'Z1', 'name': 'Z1'},
                          {'id': 'z-2', 'warehouseId': 'wh-2', 'code': 'Z2', 'name': 'Z2'}],
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('cellPurposes', 'bulk'),
                         [{'id': 'cp-1', 'warehouseId': 'wh-1', 'name': 'Path'}],
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('locations', 'bulk'),
                         [{'id': 'l-1', 'warehouseId': 'wh-1', 'code': 'A-01'},
                          {'id': 'l-2', 'warehouseId': 'wh-1', 'code': 'A-02'},
                          {'id': 'l-3', 'warehouseId': 'wh-2', 'code': 'A-01'}],
                         format='json', HTTP_COMPANY_CODE='TC001')
        # A pallet is NOT warehouse-scoped and must survive the cascade.
        self.client.post(self.url('pallets'),
                         {'id': 'p-1', 'licensePlate': 'PLT-1', 'status': 'ACTIVE'},
                         format='json', HTTP_COMPANY_CODE='TC001')

        response = self.client.delete(self.url('warehouses', 'wh-1'),
                                      HTTP_COMPANY_CODE='TC001')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        company = self.company
        # wh-1 and everything it owned is soft-deleted — retained in the DB but
        # flagged, so excluded from every read.
        self.assertTrue(Warehouse.objects.get(company=company, record_id='wh-1').is_deleted)
        self.assertTrue(all(z.is_deleted for z in Zone.objects.filter(
            company=company, data__warehouseId='wh-1')))
        self.assertTrue(all(cp.is_deleted for cp in CellPurpose.objects.filter(
            company=company, data__warehouseId='wh-1')))
        self.assertTrue(all(loc.is_deleted for loc in Location.objects.filter(
            company=company, data__warehouseId='wh-1')))
        # Reads exclude the soft-deleted warehouse and its rows…
        self.assertEqual([w['id'] for w in self.client.get(
            self.url('warehouses'), HTTP_COMPANY_CODE='TC001').json()], ['wh-2'])
        self.assertEqual(self.client.get(
            self.url('locations') + '?warehouseId=wh-1',
            HTTP_COMPANY_CODE='TC001').json(), [])
        # …while wh-2's rows and the pallet are untouched (still live).
        self.assertFalse(Warehouse.objects.get(company=company, record_id='wh-2').is_deleted)
        self.assertEqual(Zone.objects.filter(
            company=company, data__warehouseId='wh-2', is_deleted=False).count(), 1)
        self.assertEqual(Location.objects.filter(
            company=company, data__warehouseId='wh-2', is_deleted=False).count(), 1)
        self.assertFalse(Pallet.objects.get(company=company, record_id='p-1').is_deleted)

    def test_delete_non_warehouse_record_does_not_cascade(self):
        """Deleting a single location (normal op) removes only that row."""
        self.client.post(self.url('warehouses'), warehouse_doc('wh-1'),
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.post(self.url('locations', 'bulk'),
                         [{'id': 'l-1', 'warehouseId': 'wh-1', 'code': 'A-01'},
                          {'id': 'l-2', 'warehouseId': 'wh-1', 'code': 'A-02'}],
                         format='json', HTTP_COMPANY_CODE='TC001')
        self.client.delete(self.url('locations', 'l-1'), HTTP_COMPANY_CODE='TC001')
        self.assertFalse(Warehouse.objects.get(record_id='wh-1').is_deleted)
        # l-1 is soft-deleted; l-2 stays live. The list read shows only l-2.
        self.assertTrue(Location.objects.get(record_id='l-1').is_deleted)
        self.assertFalse(Location.objects.get(record_id='l-2').is_deleted)
        self.assertEqual([loc['id'] for loc in self.client.get(
            self.url('locations'), HTTP_COMPANY_CODE='TC001').json()], ['l-2'])

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


class WmsAuthorizationTests(WmsApiBaseTest):
    """Server-side role enforcement: operators (non-staff) can run operational
    workflows but cannot touch admin collections or the audit log."""

    def setUp(self):
        self.admin = self.auth_client(self.user)
        self.op = self.auth_client(self.operator)

    def test_operator_can_read_admin_collections(self):
        self.admin.post(self.url('warehouses'), warehouse_doc(), format='json',
                        HTTP_COMPANY_CODE='TC001')
        resp = self.op.get(self.url('warehouses'), HTTP_COMPANY_CODE='TC001')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_operator_cannot_create_admin_collection(self):
        resp = self.op.post(self.url('warehouses'), warehouse_doc(), format='json',
                            HTTP_COMPANY_CODE='TC001')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Warehouse.objects.filter(record_id='wh-1').exists())

    def test_operator_cannot_patch_or_delete_admin_collection(self):
        self.admin.post(self.url('warehouses'), warehouse_doc(), format='json',
                        HTTP_COMPANY_CODE='TC001')
        patch = self.op.patch(self.url('warehouses', 'wh-1'), {'name': 'x'},
                              format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(patch.status_code, status.HTTP_403_FORBIDDEN)
        delete = self.op.delete(self.url('warehouses', 'wh-1'),
                                HTTP_COMPANY_CODE='TC001')
        self.assertEqual(delete.status_code, status.HTTP_403_FORBIDDEN)

    def test_operator_can_do_operational_writes(self):
        pallet = {'id': 'p-1', 'licensePlate': 'PLT-1', 'itemCode': 'SKU1',
                  'status': 'ACTIVE', 'boxCount': 4}
        resp = self.op.post(self.url('pallets'), pallet, format='json',
                            HTTP_COMPANY_CODE='TC001')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        # and may delete operational records (normal pick/move flow removes them)
        delete = self.op.delete(self.url('pallets', 'p-1'),
                                HTTP_COMPANY_CODE='TC001')
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)

    def test_operator_cannot_escalate_via_settings(self):
        # Admin seeds settings as OPERATOR role…
        self.admin.post(self.url('settings'),
                        {'id': 'wms-settings', 'role': 'OPERATOR', 'masterEnabled': True},
                        format='json', HTTP_COMPANY_CODE='TC001')
        # …operator cannot flip it to ADMIN (patch) or overwrite it (post).
        patch = self.op.patch(self.url('settings', 'wms-settings'),
                              {'role': 'ADMIN'}, format='json',
                              HTTP_COMPANY_CODE='TC001')
        self.assertEqual(patch.status_code, status.HTTP_403_FORBIDDEN)
        post = self.op.post(self.url('settings'),
                            {'id': 'wms-settings', 'role': 'ADMIN'},
                            format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(post.status_code, status.HTTP_403_FORBIDDEN)

    def test_operator_may_seed_settings_when_absent(self):
        resp = self.op.post(self.url('settings'),
                            {'id': 'wms-settings', 'role': 'OPERATOR'},
                            format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_movements_are_append_only(self):
        move = {'id': 'm-1', 'type': 'PUTAWAY', 'itemCode': 'SKU1', 'quantity': 5}
        # operators create movements during normal flows
        self.op.post(self.url('movements'), move, format='json',
                     HTTP_COMPANY_CODE='TC001')
        # nobody may edit them
        patch = self.admin.patch(self.url('movements', 'm-1'), {'quantity': 99},
                                 format='json', HTTP_COMPANY_CODE='TC001')
        self.assertEqual(patch.status_code, status.HTTP_403_FORBIDDEN)
        # operators may not delete; staff may
        op_del = self.op.delete(self.url('movements', 'm-1'),
                                HTTP_COMPANY_CODE='TC001')
        self.assertEqual(op_del.status_code, status.HTTP_403_FORBIDDEN)
        admin_del = self.admin.delete(self.url('movements', 'm-1'),
                                      HTTP_COMPANY_CODE='TC001')
        self.assertEqual(admin_del.status_code, status.HTTP_204_NO_CONTENT)


class WmsPatchMergeTests(WmsApiBaseTest):
    def setUp(self):
        self.client = self.auth_client()  # staff

    def test_patch_deep_merges_nested_objects(self):
        self.client.post(self.url('warehouses'), warehouse_doc(), format='json',
                         HTTP_COMPANY_CODE='TC001')
        # Patch only ONE key inside namingScheme; siblings must survive.
        resp = self.client.patch(
            self.url('warehouses', 'wh-1'),
            {'namingScheme': {'separator': '/'}},
            format='json', HTTP_COMPANY_CODE='TC001',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['namingScheme']['separator'], '/')
        # sibling keys preserved (shallow merge would have dropped these)
        self.assertEqual(resp.data['namingScheme']['columnStyle'], 'LETTERS')
        self.assertEqual(resp.data['namingScheme']['prefix'], '')


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
