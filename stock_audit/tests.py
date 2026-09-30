"""Tests for the stock audit.

Run with:
    .venv/bin/python manage.py test stock_audit --settings=config.sqlite_test_settings

SAP is the fixture: BH-PM holds canola (RM), caps (PM), a finished case (FG), a
tool (other) and a label SAP thinks it is 12 short of.
"""
import csv
import io
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.dtos import WarehouseDTO
from sap_client.exceptions import SAPConnectionError

from .models import StockAudit, StockAuditCount, StockAuditLine

User = get_user_model()


def sap_row(code, name, group, on_hand, uom='PCS'):
    return {'item_code': code, 'item_name': name, 'item_group': group, 'uom': uom,
            'on_hand': Decimal(on_hand)}


WAREHOUSE = [
    sap_row('RM0000001', 'CANOLA OIL', 106, '1500.5', 'KG'),
    sap_row('PM0000010', 'CAPS 28MM RED', 105, '32'),
    sap_row('FG0000100', 'JIVO CANOLA 1L', 102, '400', 'CASE'),
    sap_row('TL0000001', 'SPANNER', 101, '3'),
    sap_row('PM0000020', 'LABEL 1L FRONT', 105, '-12'),
]

AUDITOR = ['can_view_stock_audit', 'can_count_stock_audit']
MANAGER = AUDITOR + ['can_view_audit_sap_qty', 'can_manage_stock_audit']


class StockAuditTests(APITestCase):
    maxDiff = None

    def setUp(self):
        self.company = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        self.role = UserRole.objects.create(name='Stores')
        self.manager = self._user('manager@jivo.test', 'M1', MANAGER)
        self.auditor = self._user('auditor@jivo.test', 'A1', AUDITOR)
        self.sap = mock.patch('stock_audit.services.StockAuditReader').start()
        self.addCleanup(mock.patch.stopall)
        self.sap.return_value.warehouse_stock.return_value = list(WAREHOUSE)
        self._as(self.manager)

    def _user(self, email, code, codenames, company=None):
        user = User.objects.create_user(email=email, password='pw', full_name=email.split('@')[0],
                                        employee_code=code)
        UserCompany.objects.create(user=user, company=company or self.company, role=self.role,
                                   is_default=True)
        user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='stock_audit', codename__in=codenames))
        return User.objects.get(pk=user.pk)

    def _as(self, user, company=None):
        self.client.force_authenticate(user)
        self.client.credentials(HTTP_COMPANY_CODE=(company or self.company).code)

    def _start(self, code='BH-PM'):
        return self.client.post(reverse('stock-audit-list'),
                                {'warehouse_code': code, 'warehouse_name': 'PM Store'},
                                format='json')

    def _audit(self):
        response = self._start()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return StockAudit.objects.get(pk=response.data['id'])

    def _line(self, audit, code):
        return audit.lines.get(item_code=code)

    def _count(self, audit, code, qty, note=''):
        line = self._line(audit, code)
        return self.client.post(
            reverse('stock-audit-line-counts', args=[audit.id, line.id]),
            {'qty': qty, 'note': note}, format='json')

    def _lines(self, audit, **params):
        response = self.client.get(reverse('stock-audit-lines', args=[audit.id]), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    # -- starting --------------------------------------------------------------

    def test_starting_copies_every_item_sap_holds_by_category(self):
        audit = self._audit()
        lines = {l.item_code: l for l in audit.lines.all()}
        self.assertEqual(
            {code: (l.category, l.sap_qty) for code, l in lines.items()}, {
                'RM0000001': ('RM', Decimal('1500.500')),
                'PM0000010': ('PM', Decimal('32.000')),
                'FG0000100': ('FG', Decimal('400.000')),
                'TL0000001': ('OTHER', Decimal('3.000')),
                'PM0000020': ('PM', Decimal('-12.000')),   # SAP short: still audited
            })
        self.assertTrue(all(l.counted_qty is None for l in lines.values()))
        self.sap.return_value.warehouse_stock.assert_called_once_with('BH-PM')
        self.sap.assert_called_with('JIVO_OIL')

    def test_a_warehouse_has_one_open_audit_at_a_time(self):
        self._audit()
        response = self._start()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already has an open audit', response.data['detail'])

    def test_sap_down_starts_nothing(self):
        self.sap.return_value.warehouse_stock.side_effect = SAPConnectionError('SAP is down')
        self.assertEqual(self._start().status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(StockAudit.objects.exists())

    def test_the_warehouse_list_shows_which_are_being_audited(self):
        audit = self._audit()
        with mock.patch('stock_audit.views.SAPClient') as client:
            client.return_value.get_active_warehouses.return_value = [
                WarehouseDTO(warehouse_code='BH-PM', warehouse_name='PM Store'),
                WarehouseDTO(warehouse_code='BH-RM', warehouse_name='RM Store')]
            data = self.client.get(reverse('stock-audit-warehouses')).data
        self.assertEqual(data, [
            {'code': 'BH-PM', 'name': 'PM Store', 'open_audit_id': audit.id},
            {'code': 'BH-RM', 'name': 'RM Store', 'open_audit_id': None}])

    # -- counting ----------------------------------------------------------------

    def test_counts_add_up(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10', 'rack A')
        response = self._count(audit, 'PM0000010', '20', 'rack B')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['counted_qty'], '30')
        self.assertEqual(response.data['count_entries'], 2)
        self.assertEqual(response.data['difference'], '-2')          # 30 against 32

    def test_a_count_can_take_some_off(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '30')
        self.assertEqual(self._count(audit, 'PM0000010', '-5').data['counted_qty'], '25')

    def test_on_hand_cannot_be_taken_below_nothing(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10')
        response = self._count(audit, 'PM0000010', '-11')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only 10 on hand', response.data['detail'])
        self.assertEqual(self._count(audit, 'PM0000010', '-10').data['counted_qty'], '0')
        # Nothing counted yet: nothing to remove.
        self.assertEqual(self._count(audit, 'RM0000001', '-1').status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_taking_back_an_add_cannot_leave_a_removal_below_nothing(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10')
        self._count(audit, 'PM0000010', '-5')
        added = StockAuditCount.objects.get(qty=10)
        response = self.client.post(reverse('stock-audit-count-void', args=[audit.id, added.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._line(audit, 'PM0000010').counted_qty, Decimal('5'))

    def test_a_count_must_be_a_figure(self):
        audit = self._audit()
        for bad in ('', 'ten', 'NaN'):
            self.assertEqual(self._count(audit, 'PM0000010', bad).status_code,
                             status.HTTP_400_BAD_REQUEST, bad)
        self.assertIsNone(self._line(audit, 'PM0000010').counted_qty)

    def test_found_none_is_a_count(self):
        # Looked, found none: the line reads 0 counted, not blank.
        audit = self._audit()
        self.assertEqual(self._count(audit, 'TL0000001', '0').status_code, status.HTTP_201_CREATED)
        line = self._line(audit, 'TL0000001')
        self.assertEqual(line.counted_qty, Decimal('0'))
        self.assertEqual(line.difference, Decimal('-3'))

    def test_a_count_taken_back_comes_off_the_line(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10')
        self._count(audit, 'PM0000010', '200')      # meant 20
        wrong = StockAuditCount.objects.get(qty=200)
        response = self.client.post(reverse('stock-audit-count-void', args=[audit.id, wrong.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['counted_qty'], '10')
        wrong.refresh_from_db()
        self.assertIsNotNone(wrong.voided_at)     # kept, not deleted

    def test_taking_back_every_count_leaves_the_line_uncounted(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10')
        count = StockAuditCount.objects.get()
        self.client.post(reverse('stock-audit-count-void', args=[audit.id, count.id]))
        self.assertIsNone(self._line(audit, 'PM0000010').counted_qty)

    def test_only_the_counter_or_a_manager_takes_a_count_back(self):
        audit = self._audit()
        self._as(self.auditor)
        self._count(audit, 'PM0000010', '10')
        count = StockAuditCount.objects.get()
        other = self._user('other@jivo.test', 'A2', AUDITOR)
        self._as(other)
        self.assertEqual(
            self.client.post(reverse('stock-audit-count-void', args=[audit.id, count.id]))
            .status_code, status.HTTP_400_BAD_REQUEST)
        self._as(self.manager)
        self.assertEqual(
            self.client.post(reverse('stock-audit-count-void', args=[audit.id, count.id]))
            .status_code, status.HTTP_200_OK)

    def test_the_count_history_is_oldest_first(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '10', 'rack A')
        self._count(audit, 'PM0000010', '20', 'rack B')
        line = self._line(audit, 'PM0000010')
        history = self.client.get(reverse('stock-audit-line-counts', args=[audit.id, line.id])).data
        self.assertEqual([(h['qty'], h['note'], h['mine']) for h in history],
                         [('10', 'rack A', True), ('20', 'rack B', True)])

    # -- what a counter sees -------------------------------------------------------

    def test_a_counter_does_not_see_sap_or_the_difference(self):
        audit = self._audit()
        self._as(self.auditor)
        self._count(audit, 'PM0000010', '30')
        data = self._lines(audit, search='CAPS')
        self.assertFalse(data['sees_sap'])
        self.assertEqual(data['results'][0]['counted_qty'], '30')
        self.assertNotIn('sap_qty', data['results'][0])
        self.assertNotIn('difference', data['results'][0])
        self.assertEqual(self.client.get(reverse('stock-audit-lines', args=[audit.id]),
                                         {'state': 'different'}).status_code,
                         status.HTTP_400_BAD_REQUEST)
        summary = self.client.get(reverse('stock-audit-detail', args=[audit.id])).data['summary']
        self.assertNotIn('different', summary['total'])

    def test_the_sap_right_alone_shows_sap(self):
        audit = self._audit()
        peeker = self._user('peek@jivo.test', 'A3', AUDITOR + ['can_view_audit_sap_qty'])
        self._as(peeker)
        self.assertEqual(self._lines(audit, search='CAPS')['results'][0]['sap_qty'], '32')

    # -- finding lines -----------------------------------------------------------

    def test_lines_are_searched_and_filtered(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '32')      # matches SAP
        self._count(audit, 'RM0000001', '1400')    # 100.5 short
        codes = lambda data: [r['item_code'] for r in data['results']]
        self.assertEqual(codes(self._lines(audit, search='canola')), ['RM0000001', 'FG0000100'])
        self.assertEqual(codes(self._lines(audit, category='PM')), ['PM0000010', 'PM0000020'])
        self.assertEqual(codes(self._lines(audit, state='counted')), ['RM0000001', 'PM0000010'])
        self.assertEqual(len(self._lines(audit, state='uncounted')['results']), 3)
        self.assertEqual(codes(self._lines(audit, state='different')), ['RM0000001'])

    def test_lines_come_a_page_at_a_time(self):
        self.sap.return_value.warehouse_stock.return_value = [
            sap_row(f'PM{n:07d}', f'ITEM {n}', 105, '1') for n in range(120)]
        audit = self._audit()
        data = self._lines(audit, page=3)
        self.assertEqual((data['count'], data['page_size'], len(data['results'])), (120, 50, 20))

    def test_the_summary_counts_progress_by_category(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '32')
        self._count(audit, 'RM0000001', '1400')
        summary = self.client.get(reverse('stock-audit-detail', args=[audit.id])).data['summary']
        self.assertEqual(summary['total'], {'lines': 5, 'counted': 2, 'different': 1})
        self.assertEqual(summary['by_category']['PM'], {'lines': 2, 'counted': 1, 'different': 0})

    # -- items SAP did not list ----------------------------------------------------

    def test_an_item_found_on_the_floor_is_added_with_sap_s_figure(self):
        audit = self._audit()
        self.sap.return_value.item.return_value = sap_row('PM0000099', 'CAPS 38MM', 105, '0')
        response = self.client.post(reverse('stock-audit-items', args=[audit.id]),
                                    {'item_code': 'PM0000099'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual((response.data['in_sap'], response.data['sap_qty']), (False, '0'))
        self.sap.return_value.item.assert_called_once_with('BH-PM', 'PM0000099')

    def test_adding_an_item_already_on_the_audit_returns_it(self):
        audit = self._audit()
        response = self.client.post(reverse('stock-audit-items', args=[audit.id]),
                                    {'item_code': 'PM0000010'}, format='json')
        self.assertEqual(response.data['id'], self._line(audit, 'PM0000010').id)
        self.sap.return_value.item.assert_not_called()

    def test_an_item_sap_does_not_know_is_refused(self):
        audit = self._audit()
        self.sap.return_value.item.return_value = None
        response = self.client.post(reverse('stock-audit-items', args=[audit.id]),
                                    {'item_code': 'NOPE'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sap_is_searched_for_items_to_add(self):
        audit = self._audit()
        self.sap.return_value.search_items.return_value = [
            sap_row('PM0000010', 'CAPS 28MM RED', 105, '32'),
            sap_row('PM0000099', 'CAPS 38MM', 105, '0')]
        data = self.client.get(reverse('stock-audit-items', args=[audit.id]), {'search': 'caps'}).data
        self.assertEqual([(r['item_code'], r['on_audit']) for r in data],
                         [('PM0000010', True), ('PM0000099', False)])

    # -- re-reading SAP, and closing ------------------------------------------------

    def test_sap_can_be_read_again_until_counting_starts(self):
        audit = self._audit()
        self.sap.return_value.warehouse_stock.return_value = [
            sap_row('PM0000010', 'CAPS 28MM RED', 105, '40')]
        response = self.client.post(reverse('stock-audit-refresh', args=[audit.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(list(audit.lines.values_list('item_code', 'sap_qty')),
                         [('PM0000010', Decimal('40.000'))])

        self._count(audit, 'PM0000010', '1')
        response = self.client.post(reverse('stock-audit-refresh', args=[audit.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Counting has started', response.data['detail'])

    def test_a_closed_audit_takes_no_counts(self):
        audit = self._audit()
        self.assertEqual(self.client.post(reverse('stock-audit-close', args=[audit.id]))
                         .status_code, status.HTTP_200_OK)
        self.assertEqual(self._count(audit, 'PM0000010', '1').status_code,
                         status.HTTP_400_BAD_REQUEST)
        # ...and the warehouse can be audited again.
        self.assertEqual(self._start().status_code, status.HTTP_201_CREATED)

    # -- the export ---------------------------------------------------------------

    def _export(self, audit):
        response = self.client.get(reverse('stock-audit-export', args=[audit.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return list(csv.reader(io.StringIO(response.content.decode())))

    def test_the_export_has_every_line(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '30')
        rows = self._export(audit)
        self.assertEqual(rows[0], ['Item code', 'Item name', 'Category', 'UoM', 'On hand (physical)',
                                   'SAP', 'Difference', 'Not in SAP copy'])
        caps = next(r for r in rows if r[0] == 'PM0000010')
        self.assertEqual(caps[4:7], ['30', '32', '-2'])
        self.assertEqual(len(rows), 6)

    def test_a_counters_export_leaves_sap_out(self):
        audit = self._audit()
        self._as(self.auditor)
        self.assertNotIn('SAP', self._export(audit)[0])

    # -- who ------------------------------------------------------------------------

    def test_a_counter_cannot_start_refresh_or_close(self):
        audit = self._audit()
        self._as(self.auditor)
        self.assertEqual(self._start('BH-RM').status_code, status.HTTP_403_FORBIDDEN)
        for name in ('stock-audit-refresh', 'stock-audit-close'):
            self.assertEqual(self.client.post(reverse(name, args=[audit.id])).status_code,
                             status.HTTP_403_FORBIDDEN)

    def test_nobody_else_reaches_an_audit(self):
        audit = self._audit()
        self._as(self._user('nobody@jivo.test', 'N1', []))
        self.assertEqual(self.client.get(reverse('stock-audit-detail', args=[audit.id]))
                         .status_code, status.HTTP_403_FORBIDDEN)

    def test_another_companys_audit_is_not_found(self):
        audit = self._audit()
        mart = Company.objects.create(name='Jivo Mart', code='JIVO_MART')
        self._as(self._user('mart@jivo.test', 'M9', MANAGER, company=mart), company=mart)
        self.assertEqual(self.client.get(reverse('stock-audit-detail', args=[audit.id]))
                         .status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(reverse('stock-audit-list')).data, [])


class StockAuditGroupsTests(APITestCase):
    def test_the_groups_hold_their_rights(self):
        from importlib import import_module

        from django.apps import apps
        from django.contrib.auth.models import Group

        import_module('stock_audit.migrations.0002_stock_audit_groups').create_groups(apps, None)
        rights = lambda name: set(Group.objects.get(name=name).permissions
                                  .values_list('codename', flat=True))
        self.assertEqual(rights('Stock Auditor'), set(AUDITOR))
        self.assertEqual(rights('Stock Audit Manager'), set(MANAGER))
