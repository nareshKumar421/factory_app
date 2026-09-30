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


GROUP_NAMES = {106: 'RAW MATERIAL', 105: 'PACKAGING MATERIAL', 102: 'FINISHED',
               107: 'TRADING ITEMS', 115: 'SEMI FINISHED GOODS', 101: 'TOOLS'}


def sap_row(code, name, group, on_hand, uom='PCS', batch=False):
    return {'item_code': code, 'item_name': name, 'item_group': group, 'uom': uom,
            'on_hand': Decimal(on_hand), 'item_group_name': GROUP_NAMES.get(group, ''),
            'is_batch': batch}


WAREHOUSE = [
    sap_row('RM0000001', 'CANOLA OIL', 106, '1500.5', 'KG', batch=True),
    sap_row('PM0000010', 'CAPS 28MM RED', 105, '32'),
    sap_row('FG0000100', 'JIVO CANOLA 1L', 102, '400', 'CASE'),
    sap_row('TL0000001', 'SPANNER', 101, '3'),
    sap_row('PM0000020', 'LABEL 1L FRONT', 105, '-12'),
]

AUDITOR = ['can_view_stock_audit', 'can_count_stock_audit']
MANAGER = AUDITOR + ['can_view_audit_sap_qty', 'can_manage_stock_audit']
APPROVER = ['can_view_stock_audit', 'can_view_audit_sap_qty', 'can_approve_stock_audit']
POSTER = ['can_view_stock_audit', 'can_post_stock_audit_to_sap']


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
        self.assertIn('already has an audit in progress', response.data['detail'])

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
        self.assertEqual(summary['by_group']['PACKAGING MATERIAL'],
                         {'lines': 2, 'counted': 1, 'different': 0, 'category': 'PM'})
        self.assertEqual(summary['by_group']['TOOLS']['lines'], 1)

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

    # -- complete, approve, reject -------------------------------------------------

    def _step(self, audit, name, **data):
        return self.client.post(reverse(f'stock-audit-{name}', args=[audit.id]), data,
                                format='json')

    def _complete(self, audit):
        """What an auditor does before Complete: 0 for everything not found."""
        for line in audit.lines.filter(counted_qty__isnull=True):
            self._count(audit, line.item_code, '0')
        return self._step(audit, 'complete')

    def test_every_item_is_counted_before_completing(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '30')
        response = self._step(audit, 'complete')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('4 items are not counted yet', response.data['detail'])
        self.assertIn('press 0', response.data['detail'])
        # 0 for none found is a count.
        for code in ('RM0000001', 'FG0000100', 'TL0000001', 'PM0000020'):
            self._count(audit, code, '0')
        self.assertEqual(self._complete(audit).data['status'], 'SUBMITTED')

    def test_approving_and_rejecting_take_a_comment(self):
        audit = self._audit()
        self._complete(audit)
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self.assertEqual(self._step(audit, 'approve').status_code, status.HTTP_400_BAD_REQUEST)
        response = self._step(audit, 'approve', comment='Rechecked racks A-C')
        self.assertEqual((response.data['status'], response.data['approval_comment']),
                         ('APPROVED', 'Rechecked racks A-C'))

    def test_a_completed_audit_waits_for_an_approver(self):
        audit = self._audit()
        self._as(self.auditor)
        self._count(audit, 'PM0000010', '30')
        response = self._complete(audit)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['status'], 'SUBMITTED')
        # The auditor's part is done.
        self.assertEqual(self._count(audit, 'PM0000010', '1').status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self._step(audit, 'approve').status_code, status.HTTP_403_FORBIDDEN)

    def test_an_approver_may_correct_the_count_before_approving(self):
        audit = self._audit()
        self._as(self.auditor)
        self._count(audit, 'PM0000010', '30')
        self._complete(audit)
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self.assertEqual(self._count(audit, 'PM0000010', '2').data['counted_qty'], '32')
        response = self._step(audit, 'approve', comment='Checked')
        self.assertEqual((response.status_code, response.data['status']), (200, 'APPROVED'))
        self.assertEqual(self._count(audit, 'PM0000010', '1').status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_a_rejected_audit_goes_back_to_the_auditors_with_the_reason(self):
        audit = self._audit()
        self._as(self.auditor)
        self._complete(audit)
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self.assertEqual(self._step(audit, 'reject').status_code, status.HTTP_400_BAD_REQUEST)
        response = self._step(audit, 'reject', comment='Count rack C again')
        self.assertEqual(response.data['status'], 'OPEN')
        self.assertEqual(response.data['rejection_reason'], 'Count rack C again')
        self._as(self.auditor)
        self.assertEqual(self._count(audit, 'PM0000010', '5').status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._complete(audit).data['status'], 'SUBMITTED')

    def test_the_page_is_told_what_each_person_may_do(self):
        audit = self._audit()
        detail = lambda: self.client.get(reverse('stock-audit-detail', args=[audit.id])).data
        self._as(self.auditor)
        self.assertEqual(detail()['actions'], {
            'count': True, 'refresh': False, 'complete': True, 'approve': False,
            'post_to_sap': False, 'void_any': False})
        self._complete(audit)
        self.assertFalse(detail()['actions']['count'])
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self.assertTrue(detail()['actions']['count'])
        self.assertTrue(detail()['actions']['approve'])

    def test_a_warehouse_waiting_for_approval_cannot_be_audited_again_yet(self):
        audit = self._audit()
        self._complete(audit)
        self.assertEqual(self._start().status_code, status.HTTP_400_BAD_REQUEST)
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self._step(audit, 'approve', comment='Checked')
        self._as(self.manager)
        self.assertEqual(self._start().status_code, status.HTTP_201_CREATED)

    def test_lines_filter_by_sap_item_group(self):
        audit = self._audit()
        data = self._lines(audit, group='PACKAGING MATERIAL')
        self.assertEqual([r['item_code'] for r in data['results']], ['PM0000010', 'PM0000020'])
        self.assertEqual(data['results'][0]['item_group_name'], 'PACKAGING MATERIAL')

    # -- the export ---------------------------------------------------------------

    def _export(self, audit):
        response = self.client.get(reverse('stock-audit-export', args=[audit.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return list(csv.reader(io.StringIO(response.content.decode())))

    def test_the_export_has_every_line(self):
        audit = self._audit()
        self._count(audit, 'PM0000010', '30')
        rows = self._export(audit)
        self.assertEqual(rows[0], ['Item code', 'Item name', 'Item group', 'UoM', 'On hand (physical)',
                                   'SAP', 'Difference', 'Not in SAP copy'])
        caps = next(r for r in rows if r[0] == 'PM0000010')
        self.assertEqual(caps[4:7], ['30', '32', '-2'])
        self.assertEqual(len(rows), 6)

    def test_a_counters_export_leaves_sap_out(self):
        audit = self._audit()
        self._as(self.auditor)
        self.assertNotIn('SAP', self._export(audit)[0])

    # -- who ------------------------------------------------------------------------

    def test_a_counter_cannot_start_refresh_approve_or_post(self):
        audit = self._audit()
        self._as(self.auditor)
        self.assertEqual(self._start('BH-RM').status_code, status.HTTP_403_FORBIDDEN)
        for name in ('stock-audit-refresh', 'stock-audit-approve', 'stock-audit-reject',
                     'stock-audit-sap-posting'):
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
    def test_the_approver_group_holds_its_rights(self):
        from importlib import import_module

        from django.apps import apps
        from django.contrib.auth.models import Group

        import_module('stock_audit.migrations.0004_backfill_groups_and_approver').forwards(apps, None)
        rights = lambda name: set(Group.objects.get(name=name).permissions
                                  .values_list('codename', flat=True))
        self.assertEqual(rights('Stock Audit Approver'), set(APPROVER))
        self.assertTrue({'can_approve_stock_audit', 'can_post_stock_audit_to_sap'}
                        <= rights('Stock Audit Manager'))

    def test_the_groups_hold_their_rights(self):
        from importlib import import_module

        from django.apps import apps
        from django.contrib.auth.models import Group

        import_module('stock_audit.migrations.0002_stock_audit_groups').create_groups(apps, None)
        rights = lambda name: set(Group.objects.get(name=name).permissions
                                  .values_list('codename', flat=True))
        self.assertEqual(rights('Stock Auditor'), set(AUDITOR))
        self.assertEqual(rights('Stock Audit Manager'), set(MANAGER))


class BatchSpreadTests(APITestCase):
    """A counted total put onto an item's batches: short from the oldest, excess on the newest."""

    def spread(self, counted, *batches):
        from .services import _spread_over_batches

        rows = [{'batch': f'B{n}', 'qty': Decimal(q)} for n, q in enumerate(batches, start=1)]
        return [(b, str(now), str(new)) for b, now, new in _spread_over_batches(Decimal(counted), rows)]

    def test_a_shortage_comes_off_the_oldest_batch(self):
        self.assertEqual(self.spread('90', '50', '50'), [('B1', '50', '40')])

    def test_a_shortage_bigger_than_a_batch_spills_onto_the_next(self):
        # 110 held, 30 counted: 80 short -- all 50 of B1, then 30 of B2.
        self.assertEqual(self.spread('30', '50', '50', '10'),
                         [('B1', '50', '0'), ('B2', '50', '20')])

    def test_an_excess_goes_onto_the_newest_batch(self):
        # 110 held, 130 counted: the 20 more goes on B3.
        self.assertEqual(self.spread('130', '50', '50', '10'), [('B3', '10', '30')])

    def test_nothing_changes_when_the_count_matches(self):
        self.assertEqual(self.spread('100', '50', '50'), [])

    def test_an_excess_with_no_batch_cannot_be_placed(self):
        from .services import AuditError, _spread_over_batches

        with self.assertRaises(AuditError):
            _spread_over_batches(Decimal('5'), [])


class SapPostingTests(APITestCase):
    """An approved audit's RM and PM differences, posted to SAP once."""

    # The audit fixtures, without inheriting (and so re-running) the audit tests.
    _user, _as, _start, _audit, _line, _count = (
        StockAuditTests._user, StockAuditTests._as, StockAuditTests._start,
        StockAuditTests._audit, StockAuditTests._line, StockAuditTests._count)

    def setUp(self):
        StockAuditTests.setUp(self)
        self.sap.return_value.batches.return_value = [
            {'batch': 'OLD', 'qty': Decimal('1000')}, {'batch': 'NEW', 'qty': Decimal('500.5')}]
        self.sap.return_value.warehouse_branch.return_value = 2
        self.writer = mock.patch(
            'sap_client.service_layer.inventory_posting_writer.InventoryPostingWriter').start()
        self.writer.return_value.create.return_value = {'DocumentEntry': 77, 'DocumentNumber': 1234}
        self.audit = self._audit()
        self._count(self.audit, 'RM0000001', '1400')     # 100.5 short, batch item
        self._count(self.audit, 'PM0000010', '30')       # 2 short
        self._count(self.audit, 'FG0000100', '390')      # FG: shown, not posted
        self._count(self.audit, 'TL0000001', '3')        # matches
        self._count(self.audit, 'PM0000020', '0')        # SAP says -12: 12 more
        self.client.post(reverse('stock-audit-complete', args=[self.audit.id]))
        self._as(self._user('approver@jivo.test', 'P1', APPROVER))
        self.client.post(reverse('stock-audit-approve', args=[self.audit.id]),
                         {'comment': 'Checked'}, format='json')
        self._as(self._user('poster@jivo.test', 'S1', POSTER))
        self.url = reverse('stock-audit-sap-posting', args=[self.audit.id])

    def test_the_preview_is_the_rm_and_pm_differences(self):
        data = self.client.get(self.url).data
        self.assertEqual([(l['item_code'], l['difference']) for l in data['lines']],
                         [('PM0000010', '-2'), ('PM0000020', '12'), ('RM0000001', '-100.5')])
        rm = data['lines'][2]
        # The shortage off the oldest batch.
        self.assertEqual(rm['batches'], [{'batch': 'OLD', 'sap_qty': '1000', 'counted_qty': '899.5'}])
        self.assertEqual(data['blocked'], [])

    def test_posting_sends_one_inventory_posting_and_keeps_its_number(self):
        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data['sap_posting'], response.data['sap_doc_num']), ('DONE', '1234'))
        payload = self.writer.return_value.create.call_args[0][0]
        self.assertEqual(payload['BranchID'], 2)
        self.assertEqual([(l['ItemCode'], l['WarehouseCode'], l['CountedQuantity'])
                          for l in payload['InventoryPostingLines']],
                         [('PM0000010', 'BH-PM', 30.0), ('PM0000020', 'BH-PM', 0.0),
                          ('RM0000001', 'BH-PM', 1400.0)])
        self.assertEqual(payload['InventoryPostingLines'][2]['InventoryPostingBatchNumbers'],
                         [{'BatchNumber': 'OLD', 'Quantity': 899.5, 'BaseLineNumber': 3}])
        # ...and the audit keeps what was posted, to show.
        posted = response.data['sap_posted_lines']
        self.assertEqual([(l['item_code'], l['difference']) for l in posted],
                         [('PM0000010', '-2'), ('PM0000020', '12'), ('RM0000001', '-100.5')])
        self.assertEqual(posted[2]['batches'],
                         [{'batch': 'OLD', 'sap_qty': '1000', 'counted_qty': '899.5'}])
        self.assertNotIn('InventoryPostingBatchNumbers', payload['InventoryPostingLines'][0])

    def test_it_is_never_posted_twice(self):
        self.client.post(self.url, format='json')
        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Already posted', response.data['detail'])
        self.assertEqual(self.writer.return_value.create.call_count, 1)

    def test_sap_refusing_it_can_be_tried_again(self):
        from sap_client.exceptions import SAPValidationError

        self.writer.return_value.create.side_effect = SAPValidationError('Period is locked')
        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.audit.refresh_from_db()
        self.assertEqual((self.audit.sap_posting, self.audit.sap_posting_error),
                         ('FAILED', 'Period is locked'))
        self.writer.return_value.create.side_effect = None
        self.assertEqual(self.client.post(self.url, format='json').data['sap_posting'], 'DONE')

    def test_no_answer_is_not_sent_again_until_sap_is_checked(self):
        from sap_client.exceptions import SAPOutcomeUnknown

        self.writer.return_value.create.side_effect = SAPOutcomeUnknown('timed out')
        self.client.post(self.url, format='json')
        self.audit.refresh_from_db()
        self.assertEqual(self.audit.sap_posting, 'UNKNOWN')
        self.writer.return_value.create.side_effect = None
        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Check SAP', response.data['detail'])
        self.assertEqual(self.client.post(self.url, {'confirm_unknown': True}, format='json')
                         .data['sap_posting'], 'DONE')

    def test_only_an_approved_audit_is_posted(self):
        self._as(self.manager)
        other = StockAudit.objects.create(company=self.company, warehouse_code='BH-RM',
                                          snapshot_at=self.audit.snapshot_at)
        self._as(self._user('poster2@jivo.test', 'S2', POSTER))
        response = self.client.post(reverse('stock-audit-sap-posting', args=[other.id]),
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.writer.return_value.create.assert_not_called()

    def test_posting_needs_its_own_right(self):
        self._as(self.manager)
        self.assertEqual(self.client.post(self.url, format='json').status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_a_batch_item_with_nothing_to_put_an_excess_on_blocks_the_posting(self):
        self.sap.return_value.batches.return_value = []
        data = self.client.get(self.url).data
        self.assertEqual([b['item_code'] for b in data['blocked']], ['RM0000001'])
        response = self.client.post(self.url, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.writer.return_value.create.assert_not_called()

