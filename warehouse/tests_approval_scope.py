"""What the warehouse is actually asked to approve.

Run with:
    python manage.py test warehouse.tests_approval_scope \
        --settings=config.sqlite_test_settings

A run raises **two** requests, and both are narrowed the same way: only what is
not already standing at the line is asked for — what is staged there needs
nobody's permission. What differs is the evidence each is settled against: raw
material against the store keeper's own Raw Material register, packing material
against SAP stock. The run may start only once both are settled.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from company.models import Company
from production_execution.models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    RunStatus,
)
from warehouse.models import BOMMaterialKind, BOMRequest, BOMRequestStatus
from warehouse.models_rm_stock import RawMaterialStock
from warehouse.services import approval_scope
from warehouse.services.warehouse_service import WarehouseService


class SplitPickTests(TestCase):
    """The arithmetic, on its own."""

    def test_bh_pc_covers_the_line_completely(self):
        split = approval_scope.split_pick(4000, 5000)
        self.assertEqual(split['from_production_consumption'], Decimal('4000'))
        self.assertEqual(split['from_other_warehouses'], Decimal('0'))

    def test_bh_pc_covers_part_of_the_line(self):
        split = approval_scope.split_pick(4000, 3000)
        self.assertEqual(split['from_production_consumption'], Decimal('3000'))
        self.assertEqual(split['from_other_warehouses'], Decimal('1000'))

    def test_a_negative_bh_pc_balance_is_treated_as_nothing_there(self):
        """SAP reports negative on-hand; it is not material at the line."""
        split = approval_scope.split_pick(4000, -500)
        self.assertEqual(split['from_production_consumption'], Decimal('0'))
        self.assertEqual(split['from_other_warehouses'], Decimal('4000'))

    def test_the_quantity_key_never_shadows_the_approval_flag(self):
        """`split_pick` is spread into `line_approval`'s result.

        A quantity key called `required` would overwrite the boolean of the same
        name and turn "no approval needed" into a truthy number.
        """
        decision = approval_scope.line_approval('PACKAGING', 4000, 9999)
        self.assertIs(decision['required'], False)
        self.assertEqual(decision['required_qty'], Decimal('4000'))


class LineApprovalTests(TestCase):

    def test_raw_material_not_at_the_line_is_requested_in_full(self):
        decision = approval_scope.line_approval('RAW', 4000, 0)
        self.assertTrue(decision['required'])
        self.assertEqual(decision['qty'], Decimal('4000'))
        self.assertIn('Raw Material register', decision['reason'])

    def test_oil_already_at_the_line_is_netted_off_the_request(self):
        """#476's shape: 24,000 needed, 22,834 already at BH-PC.

        Asked for in full, it went to a register holding 10,000 and could not be
        approved at all.
        """
        decision = approval_scope.line_approval('RAW', 24000, 22834)
        self.assertTrue(decision['required'])
        self.assertEqual(decision['qty'], Decimal('1166'))
        self.assertIn('22,834.000 is already at BH-PC', decision['reason'])
        self.assertIn('Raw Material register', decision['reason'])

    def test_oil_wholly_at_the_line_is_not_requested_at_all(self):
        decision = approval_scope.line_approval('RAW', 4000, 9999)
        self.assertFalse(decision['required'])
        self.assertEqual(decision['qty'], Decimal('0'))

    def test_the_register_s_own_warehouse_is_never_netted_off(self):
        """A bill consuming out of BH-LO is asking for the tank itself.

        Netting it would cancel the request against the very figure the
        approval is then checked against — every oil request would read zero.
        """
        decision = approval_scope.line_approval(
            'RAW', 4000, 90000, consumption_code='BH-LO',
        )
        self.assertTrue(decision['required'])
        self.assertEqual(decision['qty'], Decimal('4000'))
        self.assertIn('BH-LO', decision['reason'])

    def test_packing_material_from_another_godown_is_approved(self):
        decision = approval_scope.line_approval('PACKAGING', 4000, 0)
        self.assertTrue(decision['required'])
        self.assertEqual(decision['qty'], Decimal('4000'))

    def test_only_the_fetched_remainder_is_approved(self):
        decision = approval_scope.line_approval('PACKAGING', 4000, 3000)
        self.assertTrue(decision['required'])
        self.assertEqual(decision['qty'], Decimal('1000'))
        self.assertIn('3,000.000 is already at BH-PC', decision['reason'])

    def test_an_unclassifiable_component_still_goes_to_the_store(self):
        """OTHER is the catch-all bucket, so it is treated like packing material.

        Dropping it would silently stop somebody being asked for material they
        do have to hand over.
        """
        decision = approval_scope.line_approval('OTHER', 500, 0)
        self.assertTrue(decision['required'])


class BOMRequestSplitTests(TestCase):
    """End to end through `create_bom_request`, with SAP faked."""

    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date='2026-09-09',
            line=self.line, product='FG001', item_code='FG001',
            required_qty=Decimal('200'), status=RunStatus.DRAFT,
        )
        self.service = WarehouseService('TEST_CO')

    def usage(self, code, qty, name=None):
        return ProductionMaterialUsage.objects.create(
            production_run=self.run, material_code=code,
            material_name=name or code, opening_qty=Decimal(str(qty)), uom='PCS',
        )

    def create(self, *, material_types, stock, resources=()):
        with patch.object(WarehouseService, '_material_types', return_value=material_types), \
             patch.object(WarehouseService, '_resource_codes', return_value=set(resources)), \
             patch.object(WarehouseService, 'get_stock_for_items', return_value=stock), \
             patch.object(WarehouseService, '_fetch_bom_components', return_value=[]):
            return self.service.create_bom_request(
                {'production_run_id': self.run.id, 'required_qty': Decimal('200')},
                user=None,
            )

    @staticmethod
    def stock_at(code, warehouse, on_hand):
        return {code: {'warehouses': [{'WhsCode': warehouse, 'OnHand': on_hand}]}}

    def test_a_run_raises_one_request_per_half_of_the_bill(self):
        self.usage('RM0000002', 4000)
        self.usage('PM001', 4000)
        raised = self.create(
            material_types={'RM0000002': 'RAW', 'PM001': 'PACKAGING'},
            stock=self.stock_at('PM001', 'BH-PM', 9000),
        )

        self.assertEqual(len(raised), 2)
        by_kind = {r.material_kind: r for r in raised}
        self.assertEqual(
            list(by_kind[BOMMaterialKind.RAW].lines.values_list('item_code', flat=True)),
            ['RM0000002'],
        )
        self.assertEqual(
            list(by_kind[BOMMaterialKind.PACKING].lines.values_list('item_code', flat=True)),
            ['PM001'],
        )

    def test_the_raw_request_covers_only_what_is_not_at_the_line(self):
        """3,000 litres staged at BH-PC against 4,000 needed: the ask is 1,000.

        The register held 10,000 — enough for the balance, nowhere near enough
        for the full 4,000 — so the un-narrowed request was unapprovable.
        """
        self.usage('RM0000002', 4000)
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO', item_code='RM0000002',
            qty=Decimal('10000'), as_of_date='2026-09-09', uom='LTR',
        )
        raised = self.create(
            material_types={'RM0000002': 'RAW'},
            stock=self.stock_at('RM0000002', 'BH-PC', 3000),
        )

        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].material_kind, BOMMaterialKind.RAW)
        line = raised[0].lines.get()
        self.assertEqual(line.required_qty, Decimal('1000.000'))
        self.assertIn('already at BH-PC', line.remarks)

    def test_nothing_is_raised_when_the_oil_is_already_at_the_line(self):
        """The tank has nothing to release: the run can have what is staged."""
        self.usage('RM0000002', 4000)
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO', item_code='RM0000002',
            qty=Decimal('90000'), as_of_date='2026-09-09', uom='LTR',
        )
        raised = self.create(
            material_types={'RM0000002': 'RAW'},
            stock=self.stock_at('RM0000002', 'BH-PC', 99999),
        )

        self.assertEqual(raised, [])
        self.assertEqual(BOMRequest.objects.count(), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.warehouse_approval_status, 'NOT_REQUIRED')

    def test_a_bill_with_no_raw_material_raises_only_the_packing_request(self):
        self.usage('PM001', 4000)
        raised = self.create(
            material_types={'PM001': 'PACKAGING'},
            stock=self.stock_at('PM001', 'BH-PM', 9000),
        )

        self.assertEqual([r.material_kind for r in raised], [BOMMaterialKind.PACKING])

    def test_nothing_is_raised_when_the_bill_is_packing_already_at_bh_pc(self):
        self.usage('PM001', 4000)
        raised = self.create(
            material_types={'PM001': 'PACKAGING'},
            stock=self.stock_at('PM001', 'BH-PC', 9000),
        )

        self.assertEqual(raised, [])
        self.assertEqual(BOMRequest.objects.count(), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.warehouse_approval_status, 'NOT_REQUIRED')

    def test_the_packing_request_covers_only_the_fetched_remainder(self):
        self.usage('PM001', 4000)
        raised = self.create(
            material_types={'PM001': 'PACKAGING'},
            stock={'PM001': {'warehouses': [
                {'WhsCode': 'BH-PC', 'OnHand': 3000},
                {'WhsCode': 'BH-PM', 'OnHand': 9000},
            ]}},
        )

        line = raised[0].lines.get()
        self.assertEqual(line.required_qty, Decimal('1000.000'))
        self.assertIn('already at BH-PC', line.remarks)
        self.run.refresh_from_db()
        self.assertEqual(self.run.warehouse_approval_status, 'PENDING')

    def test_an_open_request_of_one_kind_does_not_block_the_other(self):
        self.usage('RM0000002', 4000)
        first = self.create(
            material_types={'RM0000002': 'RAW'},
            stock=self.stock_at('RM0000002', 'BH-LO', 50000),
        )
        self.assertEqual(len(first), 1)

        self.usage('PM001', 4000)
        second = self.create(
            material_types={'RM0000002': 'RAW', 'PM001': 'PACKAGING'},
            stock=self.stock_at('PM001', 'BH-PM', 9000),
        )

        # The RM request is already open, so only the packing one is raised.
        self.assertEqual([r.material_kind for r in second], [BOMMaterialKind.PACKING])
        self.assertEqual(BOMRequest.objects.count(), 2)

    def test_request_476_end_to_end(self):
        """The request that started this, with its real numbers.

        BOM request #476 asked the store for 24,000 litres of loose olive oil
        while 22,834.610 of it already stood at BH-PC and the register held
        10,000 in the tank. Nothing could approve 24,000, so the approver was
        shown 0.000 with the godown picker greyed out — for a run the plant
        could comfortably have made.
        """
        self.usage('RM0000001', 24000, name='LOOSE REFINED OLIVE OIL')
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO', item_code='RM0000001',
            qty=Decimal('10000'), as_of_date='2026-09-10', uom='LTR',
        )

        raised = self.create(
            material_types={'RM0000001': 'RAW'},
            stock=self.stock_at('RM0000001', 'BH-PC', Decimal('22834.610')),
        )

        line = raised[0].lines.get()
        self.assertEqual(line.required_qty, Decimal('1165.390'))

        sources = self.service.source_options_for_request(raised[0])[line.id]
        self.assertEqual(sources['total_available'], Decimal('10000'))

        approved = self.service.approve_bom_request(raised[0].id, {'lines': [{
            'line_id': line.id, 'approved_qty': Decimal('1165.390'),
            'status': 'APPROVED',
        }]}, user=None)

        self.assertEqual(approved.status, BOMRequestStatus.APPROVED)
        self.assertEqual(
            list(line.sources.values_list('warehouse_code', 'qty')),
            [('BH-LO', Decimal('1165.390'))],
        )

    def test_a_bom_resource_line_is_never_requested(self):
        """`JWPL09240002 Filling Cost Commodities` is conversion cost, not stuff.

        A resource line has no item master, so nothing classifies it and its
        stock reads 0 in every warehouse forever. Requested, it becomes a line
        the store cannot approve — the approve screen refuses a quantity above
        in-stock — and the run behind it never starts.
        """
        self.usage('RM0000003', 20000, name='MUSTARD LOOSE OIL')
        self.usage('JWPL09240002', 20000, name='Filling Cost Commodities')

        raised = self.create(
            material_types={'RM0000003': 'RAW'},
            # In the tank, not staged at the line — so the oil is still a real
            # request and the resource line is the only thing dropped.
            stock=self.stock_at('RM0000003', 'BH-LO', 60000),
            resources={'JWPL09240002'},
        )

        self.assertEqual([r.material_kind for r in raised], [BOMMaterialKind.RAW])
        self.assertEqual(
            list(raised[0].lines.values_list('item_code', flat=True)),
            ['RM0000003'],
        )

    def test_a_bill_of_nothing_but_a_resource_line_needs_no_approval(self):
        """Run #377's shape: the packing is all staged, leaving only the resource."""
        self.usage('PM001', 4000)
        self.usage('JWPL09240002', 20000, name='Filling Cost Commodities')

        raised = self.create(
            material_types={'PM001': 'PACKAGING'},
            stock=self.stock_at('PM001', 'BH-PC', 9000),
            resources={'JWPL09240002'},
        )

        self.assertEqual(raised, [])
        self.run.refresh_from_db()
        self.assertEqual(self.run.warehouse_approval_status, 'NOT_REQUIRED')

    def test_a_component_missing_from_the_item_master_is_still_requested(self):
        """Only a *resource* is dropped — not everything SAP failed to classify.

        An item that has gone missing from the master is still material somebody
        has to pick, and a line nobody is asked for is a line nobody picks.
        """
        self.usage('PM0000914', 1000)

        raised = self.create(material_types={'PM001': 'PACKAGING'}, stock={})

        self.assertEqual([r.material_kind for r in raised], [BOMMaterialKind.PACKING])
        self.assertEqual(
            list(raised[0].lines.values_list('item_code', flat=True)), ['PM0000914'],
        )

    def test_an_unreachable_sap_requests_everything_rather_than_dropping_lines(self):
        """Asking for too much is a conversation; a silently dropped line is not.

        The store would simply never be asked to pick it.
        """
        self.usage('PM001', 4000)
        with patch.object(
            WarehouseService, '_material_types', side_effect=RuntimeError('HANA down')
        ), patch.object(WarehouseService, '_fetch_bom_components', return_value=[]):
            raised = self.service.create_bom_request(
                {'production_run_id': self.run.id, 'required_qty': Decimal('200')},
                user=None,
            )

        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].lines.get().required_qty, Decimal('4000.000'))


class RunApprovalStatusTests(TestCase):
    """One run, two requests, one status — and the run waits for both."""

    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date='2026-09-09',
            line=self.line, product='FG001', item_code='FG001',
            required_qty=Decimal('200'), status=RunStatus.DRAFT,
        )
        self.service = WarehouseService('TEST_CO')

    def request(self, kind, status):
        return BOMRequest.objects.create(
            company=self.company, production_run=self.run,
            material_kind=kind, required_qty=Decimal('200'), status=status,
        )

    def test_one_pending_half_holds_the_whole_run(self):
        self.request(BOMMaterialKind.RAW, BOMRequestStatus.APPROVED)
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.PENDING)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), BOMRequestStatus.PENDING
        )

    def test_the_run_is_approved_only_when_both_halves_are(self):
        self.request(BOMMaterialKind.RAW, BOMRequestStatus.APPROVED)
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.APPROVED)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), BOMRequestStatus.APPROVED
        )

    def test_a_partial_approval_shows_through(self):
        self.request(BOMMaterialKind.RAW, BOMRequestStatus.APPROVED)
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.PARTIALLY_APPROVED)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run),
            BOMRequestStatus.PARTIALLY_APPROVED,
        )

    def test_a_run_with_only_one_half_needed_settles_on_that_half(self):
        self.request(BOMMaterialKind.RAW, BOMRequestStatus.APPROVED)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), BOMRequestStatus.APPROVED
        )

    def test_a_rejection_that_has_been_re_requested_is_not_the_live_story(self):
        """A follow-up request supersedes the rejection it was raised against."""
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.REJECTED)
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.PENDING)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), BOMRequestStatus.PENDING
        )

    def test_a_standing_rejection_with_nothing_else_blocks(self):
        self.request(BOMMaterialKind.PACKING, BOMRequestStatus.REJECTED)

        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), BOMRequestStatus.REJECTED
        )

    def test_a_run_with_no_requests_has_nothing_outstanding(self):
        self.assertEqual(
            self.service.recompute_run_approval_status(self.run), 'NOT_REQUIRED'
        )


class RawMaterialApprovalStockTests(TestCase):
    """An RM request is settled against the register, not against SAP."""

    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date='2026-09-09',
            line=self.line, product='FG001', item_code='FG001',
            required_qty=Decimal('200'), status=RunStatus.DRAFT,
        )
        self.service = WarehouseService('TEST_CO')
        self.request = BOMRequest.objects.create(
            company=self.company, production_run=self.run,
            material_kind=BOMMaterialKind.RAW,
            required_qty=Decimal('200'), status=BOMRequestStatus.PENDING,
        )
        self.request.lines.create(
            item_code='RM0000002', item_name='Canola oil',
            per_unit_qty=Decimal('20'), required_qty=Decimal('4000'), base_line=0,
        )

    def test_the_register_figure_is_what_the_approver_is_checked_against(self):
        """SAP said 143.846 on a day the keeper had registered 32,000.

        Checking RM against SAP would auto-reject a request the tank can fill.
        """
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO', item_code='RM0000002',
            qty=Decimal('32000'), as_of_date='2026-09-09', uom='LTR',
        )
        stock = self.service._get_stock_for_lines(self.request)

        self.assertEqual(stock['RM0000002']['OnHand'], Decimal('32000'))

    def test_every_register_warehouse_counts_towards_the_approval(self):
        for whs, qty in (('BH-LO', 1200), ('BH-OT', 800)):
            RawMaterialStock.objects.create(
                company=self.company, warehouse_code=whs, item_code='RM0000002',
                qty=Decimal(str(qty)), as_of_date='2026-09-09', uom='LTR',
            )
        stock = self.service._get_stock_for_lines(self.request)

        self.assertEqual(stock['RM0000002']['OnHand'], Decimal('2000'))

    def test_an_unregistered_item_has_no_figure_to_approve_against(self):
        stock = self.service._get_stock_for_lines(self.request)

        self.assertNotIn('RM0000002', stock)

    def test_a_packing_request_still_goes_to_sap(self):
        self.request.material_kind = BOMMaterialKind.PACKING
        self.request.save()
        with patch.object(
            WarehouseService, '_sap_stock_for_lines', return_value={'sentinel': {}}
        ) as sap:
            stock = self.service._get_stock_for_lines(self.request)

        sap.assert_called_once()
        self.assertEqual(stock, {'sentinel': {}})

    def test_the_screen_shows_the_same_figure_the_gate_will_check(self):
        """The detail screen used to read SAP while the gate read the register.

        An approver was shown 30,827 LTR, typed the 24,052 the run needed, and
        was told it exceeded an in-stock qty of 10,000 they had never seen.
        """
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-PC', item_code='RM0000002',
            qty=Decimal('10000'), as_of_date='2026-09-09', uom='LTR',
        )
        with patch.object(
            WarehouseService, 'get_stock_for_items',
            return_value={'RM0000002': {'total_on_hand': 30827.8}},
        ):
            shown = self.service.get_stock_for_bom_request(self.request)

        self.assertEqual(shown['RM0000002']['total_on_hand'], 10000)
        self.assertEqual(shown['RM0000002']['source'], 'RM_REGISTER')

    def test_the_screen_names_each_register_warehouse(self):
        for whs, qty in (('BH-PC', 1200), ('BH-LO', 800)):
            RawMaterialStock.objects.create(
                company=self.company, warehouse_code=whs, item_code='RM0000002',
                qty=Decimal(str(qty)), as_of_date='2026-09-09', uom='LTR',
            )
        shown = self.service.get_stock_for_bom_request(self.request)

        self.assertEqual(shown['RM0000002']['total_on_hand'], 2000)
        self.assertEqual(
            sorted(w['WhsCode'] for w in shown['RM0000002']['warehouses']),
            ['BH-LO', 'BH-PC'],
        )

    def test_a_packing_screen_is_told_its_figure_came_from_sap(self):
        self.request.material_kind = BOMMaterialKind.PACKING
        self.request.save()
        with patch.object(
            WarehouseService, '_sap_stock_for_lines',
            return_value={'RM0000002': {'OnHand': 143.846, 'warehouses': []}},
        ):
            shown = self.service.get_stock_for_bom_request(self.request)

        self.assertEqual(shown['RM0000002']['source'], 'SAP')
        self.assertAlmostEqual(shown['RM0000002']['total_on_hand'], 143.846)

    def test_a_code_typed_in_a_different_case_still_finds_its_stock(self):
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-PC', item_code='RM0000002',
            qty=Decimal('500'), as_of_date='2026-09-09', uom='LTR',
        )
        line = self.request.lines.first()
        line.item_code = ' rm0000002 '
        line.save()

        stock = self.service._get_stock_for_lines(self.request)

        self.assertEqual(stock['RM0000002']['OnHand'], Decimal('500'))
