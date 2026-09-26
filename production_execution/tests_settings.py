"""The production settings — the RM, PM and FG warehouses — and what obeys them.

Run with (name the label; see CLAUDE.md):
    python manage.py test production_execution.tests_settings
"""
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from production_execution.models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    ProductionSettings,
    RunStatus,
)
from production_execution.services import settings_service
from production_execution.services.plan_check_service import (
    STATUS_OK,
    STATUS_SHORT,
    ProductionPlanCheckService,
)
from production_execution.tests_plan_check import (
    FakeItemReader,
    FakePlanReader,
    bom_row,
    stock_row,
)
from sap_client.exceptions import SAPConnectionError
from warehouse.models import FGReceiptStatus, FinishedGoodsReceipt
from warehouse.services.warehouse_service import WarehouseService

SAP_WAREHOUSES = {'BH-PC', 'BH-PF', 'BH-PP', 'BH-LO', 'BH-PM', 'BH-FG'}


def known_to_sap(codes=SAP_WAREHOUSES):
    return patch.object(settings_service, 'active_sap_warehouses', return_value=set(codes))


class DefaultsTests(TestCase):
    def test_oil_starts_on_bh_pc_and_bh_pf(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        row = settings_service.get_settings(oil)
        self.assertEqual(
            (row.rm_warehouse, row.pm_warehouse, row.fg_warehouse),
            ('BH-PC', 'BH-PC', 'BH-PF'),
        )

    def test_beverages_starts_on_bh_pp_where_its_bills_consume(self):
        bev = Company.objects.create(code='JIVO_BEVERAGES', name='Beverages')
        row = settings_service.get_settings(bev)
        self.assertEqual(
            (row.rm_warehouse, row.pm_warehouse, row.fg_warehouse),
            ('BH-PP', 'BH-PP', 'BH-PF'),
        )

    def test_reading_the_defaults_stores_nothing(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        self.assertIsNone(settings_service.get_settings(oil).pk)
        self.assertFalse(ProductionSettings.objects.exists())

    def test_a_saved_row_wins_over_the_defaults(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        ProductionSettings.objects.create(
            company=oil, rm_warehouse='BH-LO', pm_warehouse='BH-PM', fg_warehouse='BH-FG',
        )
        row = settings_service.get_settings('JIVO_OIL')
        self.assertEqual(row.material_warehouse('RAW'), 'BH-LO')
        self.assertEqual(row.material_warehouse('PACKAGING'), 'BH-PM')
        # Anything SAP files as neither raw nor packing is handled like packing.
        self.assertEqual(row.material_warehouse('OTHER'), 'BH-PM')
        self.assertEqual(row.fg_warehouse, 'BH-FG')


class UpdateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code='JIVO_OIL', name='Oil')

    def test_codes_are_saved_trimmed_and_upper_cased(self):
        with known_to_sap():
            row = settings_service.update_settings(
                self.company, {'pm_warehouse': ' bh-pm '}, user=None,
            )
        self.assertEqual(row.pm_warehouse, 'BH-PM')
        # Fields not sent keep their defaults.
        self.assertEqual((row.rm_warehouse, row.fg_warehouse), ('BH-PC', 'BH-PF'))

    def test_a_warehouse_sap_does_not_know_is_refused(self):
        with known_to_sap(), self.assertRaisesMessage(ValueError, 'BH-XX'):
            settings_service.update_settings(
                self.company, {'fg_warehouse': 'BH-XX'}, user=None,
            )
        self.assertFalse(ProductionSettings.objects.exists())

    def test_a_blank_warehouse_is_refused(self):
        with self.assertRaisesMessage(ValueError, 'RM warehouse is required'):
            settings_service.update_settings(
                self.company, {'rm_warehouse': '  '}, user=None,
            )

    def test_an_unchanged_save_does_not_need_sap(self):
        with patch.object(
            settings_service, 'active_sap_warehouses', side_effect=SAPConnectionError('down'),
        ):
            row = settings_service.update_settings(
                self.company,
                {'rm_warehouse': 'BH-PC', 'pm_warehouse': 'bh-pc', 'fg_warehouse': 'BH-PF'},
                user=None,
            )
        self.assertIsNotNone(row.pk)


class SettingsEndpointTests(TestCase):
    URL = '/api/v1/production-execution/settings/'

    def setUp(self):
        User = get_user_model()
        self.company = Company.objects.create(code='JIVO_OIL', name='Oil')
        self.user = User.objects.create_user(email='hod@test.com', password='pw123456')
        UserCompany.objects.create(
            user=self.user, company=self.company,
            role=UserRole.objects.create(name='Staff'), is_active=True,
        )
        self.client = APIClient()
        self.client.credentials(HTTP_COMPANY_CODE='JIVO_OIL')

    def grant(self, *codenames):
        self.user.user_permissions.set(Permission.objects.filter(
            content_type__app_label='production_execution', codename__in=codenames,
        ))
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)

    def test_anyone_who_sees_production_reads_the_defaults(self):
        self.grant('can_view_production_run')
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['rm_warehouse'], 'BH-PC')
        self.assertEqual(resp.data['pm_warehouse'], 'BH-PC')
        self.assertEqual(resp.data['fg_warehouse'], 'BH-PF')
        self.assertFalse(resp.data['is_saved'])

    def test_a_viewer_cannot_change_them(self):
        self.grant('can_view_production_run')
        resp = self.client.patch(self.URL, {'fg_warehouse': 'BH-FG'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_the_manage_permission_saves_them(self):
        self.grant('can_manage_production_settings')
        with known_to_sap():
            resp = self.client.patch(self.URL, {'fg_warehouse': 'bh-fg'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['fg_warehouse'], 'BH-FG')
        self.assertTrue(resp.data['is_saved'])
        self.assertEqual(resp.data['updated_by_name'], self.user.full_name or str(self.user))
        self.assertEqual(ProductionSettings.objects.get().fg_warehouse, 'BH-FG')

    def test_an_unknown_warehouse_is_a_400(self):
        self.grant('can_manage_production_settings')
        with known_to_sap():
            resp = self.client.patch(self.URL, {'rm_warehouse': 'NOPE'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('NOPE', resp.data['detail'])

    def test_sap_down_while_checking_a_change_is_a_503(self):
        self.grant('can_manage_production_settings')
        with patch.object(
            settings_service, 'active_sap_warehouses', side_effect=SAPConnectionError('down'),
        ):
            resp = self.client.patch(self.URL, {'rm_warehouse': 'BH-LO'}, format='json')
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(ProductionSettings.objects.exists())

    def test_the_hod_group_is_given_the_manage_permission(self):
        from production_execution.management.commands.setup_production_groups import (
            PRODUCTION_GROUPS,
        )
        holders = [
            group for group, perms in PRODUCTION_GROUPS.items()
            if 'production_execution.can_manage_production_settings' in perms
        ]
        self.assertEqual(holders, ['Production HOD'])


class PlanCheckWarehouseTests(TestCase):
    """The BOM draws from the RM/PM warehouses, whatever a bill line names."""

    def setUp(self):
        self.day = date(2026, 9, 8)

    def check(self, company_code, bom_rows, stock_rows):
        company = Company.objects.get(code=company_code)
        line = ProductionLine.objects.create(company=company, name='Line-1')
        return ProductionPlanCheckService(
            company_code,
            plan_reader=FakePlanReader(bom_rows, stock_rows),
            item_reader=FakeItemReader(),
        ).check(line_id=line.id, item_code='FG001', required_qty=100, date=self.day)

    def test_oil_counts_raw_at_the_rm_warehouse_and_packing_at_the_pm_one(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        ProductionSettings.objects.create(
            company=oil, rm_warehouse='BH-LO', pm_warehouse='BH-PM', fg_warehouse='BH-PF',
        )
        result = self.check('JIVO_OIL', [
            bom_row('PM001', 'Caps', 20),
            bom_row('RM0000002', 'Canola oil', 20, group='RAW MATERIAL', uom='LTR'),
        ], [
            # BH-PC is no longer the line: what stands there is not counted.
            stock_row('PM001', 'BH-PC', 99999),
            stock_row('PM001', 'BH-PM', 2000),
            stock_row('RM0000002', 'BH-LO', 2000, group='RAW MATERIAL'),
        ])
        rows = {r['item_code']: r for r in result['materials']['rows']}
        self.assertEqual(rows['PM001']['on_hand'], 2000)
        self.assertEqual(rows['PM001']['searched_warehouses'], ['BH-PM'])
        self.assertEqual(rows['PM001']['status'], STATUS_OK)
        self.assertEqual(rows['RM0000002']['on_hand'], 2000)
        self.assertEqual(rows['RM0000002']['searched_warehouses'], ['BH-LO'])
        self.assertIn('BH-LO', rows['RM0000002']['approval_reason'])
        self.assertEqual(result['materials']['warehouses'], ['BH-LO', 'BH-PM'])
        self.assertEqual(result['materials']['warehouse_scope']['RAW'], ['BH-LO'])
        self.assertEqual(result['materials']['warehouse_scope']['PACKAGING'], ['BH-PM'])

    def test_oil_with_nothing_at_its_pm_warehouse_is_short(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        ProductionSettings.objects.create(
            company=oil, rm_warehouse='BH-PC', pm_warehouse='BH-PM', fg_warehouse='BH-PF',
        )
        row = self.check('JIVO_OIL', [bom_row('PM001', 'Caps', 20)], [
            stock_row('PM001', 'BH-PC', 5000),
        ])['materials']['rows'][0]
        self.assertEqual(row['on_hand'], 0)
        self.assertEqual(row['status'], STATUS_SHORT)

    def test_beverages_nets_its_request_against_its_pm_warehouse(self):
        """The bill line says BH-PC; the settings say BH-PP, and they win."""
        Company.objects.create(code='JIVO_BEVERAGES', name='Beverages')
        row = self.check('JIVO_BEVERAGES', [
            bom_row('PM001', 'Caps', 20, issue_warehouse='BH-PC'),
        ], [
            stock_row('PM001', 'BH-PC', 5000),
            stock_row('PM001', 'BH-PP', 1500),
            stock_row('PM001', 'BH-PM', 9000),
        ])['materials']['rows'][0]
        self.assertEqual(row['qty_at_production_consumption'], 1500)
        self.assertTrue(row['approval_required'])
        self.assertEqual(row['approval_qty'], 500)
        self.assertIn('BH-PP', row['approval_reason'])


class BOMRequestWarehouseTests(TestCase):
    """The warehouse request is narrowed by the same RM/PM warehouses."""

    def setUp(self):
        self.company = Company.objects.create(code='JIVO_BEVERAGES', name='Beverages')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date(2026, 9, 18),
            line=self.line, product='FG0000372', item_code='FG0000372',
            required_qty=Decimal('541'), status=RunStatus.DRAFT,
        )
        ProductionMaterialUsage.objects.create(
            production_run=self.run, material_code='PM0000654', material_name='Label',
            opening_qty=Decimal('5000'), uom='PCS',
        )
        self.service = WarehouseService('JIVO_BEVERAGES')

    def raise_request(self, stock, component_warehouse):
        with patch.object(WarehouseService, '_material_types',
                          return_value={'PM0000654': 'PACKAGING'}), \
             patch.object(WarehouseService, '_resource_codes', return_value=set()), \
             patch.object(WarehouseService, 'get_stock_for_items', return_value=stock), \
             patch.object(WarehouseService, '_fetch_bom_components', return_value=[{
                 'ItemCode': 'PM0000654', 'ItemName': 'Label',
                 'Warehouse': component_warehouse, 'UomCode': 'PCS', 'LineNum': 0,
             }]):
            return self.service.create_bom_request(
                {'production_run_id': self.run.id, 'required_qty': Decimal('541')},
                user=None,
            )

    def test_the_request_is_netted_by_the_pm_warehouse_not_the_bill_line(self):
        ProductionSettings.objects.create(
            company=self.company, rm_warehouse='BH-PP', pm_warehouse='BH-PM2',
            fg_warehouse='BH-PF',
        )
        raised = self.raise_request({'PM0000654': {'warehouses': [
            {'WhsCode': 'BH-PP', 'OnHand': 4900},
            {'WhsCode': 'BH-PM2', 'OnHand': 3000},
        ]}}, component_warehouse='BH-PP')

        line = raised[0].lines.first()
        self.assertEqual(line.required_qty, Decimal('2000.000'))
        self.assertIn('BH-PM2', line.remarks)

    def test_the_approver_is_not_offered_the_pm_warehouse_as_a_source(self):
        ProductionSettings.objects.create(
            company=self.company, rm_warehouse='BH-PP', pm_warehouse='BH-PM2',
            fg_warehouse='BH-PF',
        )
        raised = self.raise_request({'PM0000654': {'warehouses': []}},
                                    component_warehouse='BH-PP')
        request = raised[0]
        stock = {'PM0000654': {'warehouses': [
            {'WhsCode': 'BH-PM2', 'OnHand': Decimal('300')},
            {'WhsCode': 'BH-PP', 'OnHand': Decimal('700')},
        ]}}
        with patch.object(WarehouseService, '_sap_stock_for_lines', return_value=stock):
            found = self.service.source_options_for_request(request)[request.lines.first().id]
        self.assertEqual(found['consumption_warehouse'], 'BH-PM2')
        self.assertEqual(found['at_consumption'], Decimal('300'))
        self.assertEqual([o['warehouse'] for o in found['options']], ['BH-PP'])


class FGWarehouseTests(TestCase):
    """Finished goods go into the FG warehouse."""

    def setUp(self):
        from quality_control.models.production_qc_session import (
            ProductionQCSession,
            ProductionQCSessionType,
            ProductionQCWorkflowStatus,
        )
        self.company = Company.objects.create(code='JIVO_OIL', name='Oil')
        line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date(2026, 9, 18), line=line,
            product='FG0000372', item_code='FG0000372',
            total_production=Decimal('100'), status=RunStatus.COMPLETED,
        )
        ProductionQCSession.objects.create(
            production_run=self.run, session_number=1, checked_at=timezone.now(),
            session_type=ProductionQCSessionType.FINAL,
            workflow_status=ProductionQCWorkflowStatus.APPROVED,
            overall_result='PASS', is_active=True,
        )
        self.service = WarehouseService('JIVO_OIL')

    def test_a_receipt_naming_no_warehouse_goes_to_the_fg_warehouse(self):
        receipt = self.service.create_fg_receipt(
            {'production_run_id': self.run.id}, user=None,
        )
        self.assertEqual(receipt.warehouse, 'BH-PF')

    def test_a_changed_fg_warehouse_is_what_the_receipt_gets(self):
        ProductionSettings.objects.create(
            company=self.company, rm_warehouse='BH-PC', pm_warehouse='BH-PC',
            fg_warehouse='BH-FG',
        )
        receipt = self.service.create_fg_receipt(
            {'production_run_id': self.run.id}, user=None,
        )
        self.assertEqual(receipt.warehouse, 'BH-FG')

    def test_a_warehouse_named_on_the_receipt_still_wins(self):
        receipt = self.service.create_fg_receipt(
            {'production_run_id': self.run.id, 'warehouse': 'BH-PP'}, user=None,
        )
        self.assertEqual(receipt.warehouse, 'BH-PP')

    def test_the_sap_receipt_is_posted_into_the_receipts_warehouse(self):
        receipt = FinishedGoodsReceipt.objects.create(
            company=self.company, production_run=self.run, sap_doc_entry=42,
            item_code='FG0000372', produced_qty=Decimal('100'),
            good_qty=Decimal('100'), warehouse='BH-FG',
            posting_date=date(2026, 9, 18), status=FGReceiptStatus.RECEIVED,
        )
        session = MagicMock()
        session.post.return_value = MagicMock(ok=True, json=lambda: {'DocEntry': 7})
        client = MagicMock()
        client.context.service_layer = {
            'base_url': 'https://sap', 'company_db': 'DB', 'username': 'u', 'password': 'p',
        }
        with patch('sap_client.client.SAPClient', return_value=client), \
             patch('requests.Session', return_value=session), \
             patch.object(WarehouseService, '_get_branch_for_order', return_value=None):
            self.service.post_fg_receipt_to_sap(receipt.id)

        payload = session.post.call_args_list[-1].kwargs['json']
        self.assertEqual(payload['DocumentLines'][0]['WarehouseCode'], 'BH-FG')


class ReconciliationWarehouseTests(TestCase):
    """The reconciliation reads SAP at the configured warehouses by default."""

    def service(self, company):
        from production_execution.services.reconciliation_service import (
            ReconciliationService,
        )
        with patch(
            'production_execution.services.reconciliation_service.ReconciliationReader'
        ) as reader_cls:
            svc = ReconciliationService(company)
        reader = reader_cls.return_value
        reader.fg_by_item.return_value = []
        reader.material_issues_by_item.return_value = []
        reader.litre_items.return_value = []
        return svc, reader

    def test_fg_is_read_at_the_fg_warehouse(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        ProductionSettings.objects.create(
            company=oil, rm_warehouse='BH-PC', pm_warehouse='BH-PC', fg_warehouse='BH-FG',
        )
        svc, reader = self.service(oil)
        data = svc.get_production_reconciliation(date_from='2026-09-01', date_to='2026-09-02')
        self.assertEqual(reader.fg_by_item.call_args.args[0], 'BH-FG')
        self.assertEqual(data['meta']['warehouse'], 'BH-FG')

    def test_material_is_read_at_both_rm_and_pm_warehouses(self):
        oil = Company.objects.create(code='JIVO_OIL', name='Oil')
        ProductionSettings.objects.create(
            company=oil, rm_warehouse='BH-LO', pm_warehouse='BH-PC', fg_warehouse='BH-PF',
        )
        svc, reader = self.service(oil)
        reader.material_issues_by_item.side_effect = lambda d_from, d_to, whs: [{
            'item_code': 'PM001', 'item_name': 'Caps', 'sap_qty': 10, 'uom': 'PCS',
        }]
        data = svc.get_material_reconciliation(date_from='2026-09-01', date_to='2026-09-02')
        read_at = [c.args[2] for c in reader.material_issues_by_item.call_args_list]
        self.assertEqual(read_at, ['BH-LO', 'BH-PC'])
        self.assertEqual(data['meta']['warehouse'], 'BH-LO, BH-PC')
        self.assertEqual(data['by_sku'][0]['sap_issued'], 20)
