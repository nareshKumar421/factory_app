"""Where an approved quantity is actually drawn from.

Run with:
    python manage.py test warehouse.tests_bom_sources \
        --settings=config.sqlite_test_settings

A request line used to carry one availability figure, summed across every
warehouse the item appeared in — the staging area at the line included, and the
wastage bin and the non-moving godown with it. BOM request #465 approved 1,016
tins on the strength of it, when 1,015 of those were already at the line and the
store held exactly one. An approval now names the godowns, and those rows are
both the picker's instruction and the claim that stops the next run being
approved against the same pallet.
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
from warehouse.models import (
    BOMLineStatus,
    BOMMaterialKind,
    BOMRequest,
    BOMRequestStatus,
)
from warehouse.models_rm_stock import RawMaterialStock
from warehouse.services.warehouse_service import WarehouseService


def whs(*rows):
    """SAP per-warehouse stock, in the shape the reader returns it."""
    return [
        {'WhsCode': code, 'OnHand': Decimal(str(qty)), 'Available': Decimal(str(qty))}
        for code, qty in rows
    ]


class BOMSourceTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code='TEST_CO', name='Test Company')
        self.line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date='2026-09-18',
            line=self.line, product='FG0000372', item_code='FG0000372',
            required_qty=Decimal('541'), status=RunStatus.DRAFT,
        )
        self.service = WarehouseService('TEST_CO')

    def make_request(self, *lines, kind=BOMMaterialKind.PACKING, run=None):
        request = BOMRequest.objects.create(
            company=self.company, production_run=run or self.run,
            material_kind=kind, required_qty=Decimal('541'),
            status=BOMRequestStatus.PENDING,
        )
        for idx, (code, qty, warehouse) in enumerate(lines):
            request.lines.create(
                item_code=code, item_name=code, per_unit_qty=Decimal('1'),
                required_qty=Decimal(str(qty)), warehouse=warehouse,
                uom='PCS', base_line=idx,
            )
        return request

    def approve(self, request, stock, lines_data):
        with patch.object(
            WarehouseService, '_sap_stock_for_lines', return_value=stock
        ):
            return self.service.approve_bom_request(
                request.id, {'lines': lines_data}, user=None,
            )

    def options(self, request, stock):
        with patch.object(
            WarehouseService, '_sap_stock_for_lines', return_value=stock
        ):
            return self.service.source_options_for_request(request)


class ConsumptionWarehouseTests(BOMSourceTestCase):
    """What is already at the line is not stock the store can hand over."""

    def test_the_consumption_warehouse_is_never_offered_as_a_source(self):
        """#465's tin: 1,016 on hand, 1,015 of it already at the line.

        The old figure said the store could supply 1,016. It could supply one.
        """
        request = self.make_request(('PM0000830', 1082, 'BH-PC'))
        line = request.lines.first()

        found = self.options(request, {
            'PM0000830': {'warehouses': whs(('BH-PC', 1015), ('BH-FG', 1))},
        })[line.id]

        self.assertEqual(found['at_consumption'], Decimal('1015'))
        self.assertEqual([o['warehouse'] for o in found['options']], ['BH-FG'])
        self.assertEqual(found['total_available'], Decimal('1'))

    def test_a_quantity_no_godown_can_supply_is_refused(self):
        request = self.make_request(('PM0000830', 1082, 'BH-PC'))
        line = request.lines.first()

        with self.assertRaises(ValueError) as caught:
            self.approve(
                request,
                {'PM0000830': {'warehouses': whs(('BH-PC', 1015), ('BH-FG', 1))}},
                [{'line_id': line.id, 'approved_qty': Decimal('1016'),
                  'status': 'APPROVED'}],
            )

        self.assertIn('1', str(caught.exception))
        self.assertIn('PM0000830', str(caught.exception))

    def test_the_line_names_its_own_consumption_warehouse(self):
        """Every Beverages line consumes at BH-PP, not the configured BH-PC.

        Netting a global BH-PC off a BH-PP line subtracts a warehouse holding
        nothing and offers back the millions of pieces already at the line.
        """
        request = self.make_request(('PM0000654', 5000, 'BH-PP'))
        line = request.lines.first()

        found = self.options(request, {
            'PM0000654': {'warehouses': whs(('BH-PP', 90000), ('BH-PM', 400))},
        })[line.id]

        self.assertEqual(found['consumption_warehouse'], 'BH-PP')
        self.assertEqual(found['at_consumption'], Decimal('90000'))
        self.assertEqual([o['warehouse'] for o in found['options']], ['BH-PM'])

    def test_a_line_naming_no_warehouse_falls_back_to_the_configured_one(self):
        request = self.make_request(('PM0000276', 100, ''))
        line = request.lines.first()

        found = self.options(request, {
            'PM0000276': {'warehouses': whs(('BH-PC', 40), ('BH-PM', 60))},
        })[line.id]

        self.assertEqual(found['consumption_warehouse'], 'BH-PC')
        self.assertEqual([o['warehouse'] for o in found['options']], ['BH-PM'])


class ExactArithmeticTests(BOMSourceTestCase):
    """The gate compares exact quantities, whatever order SAP returns them in."""

    def test_the_offered_quantity_is_the_quantity_the_gate_accepts(self):
        """#465's carton, in the row order that produced 528.2579999999999.

        Summed as floats, 240 + 0.004 + 1.01 + 282.44 + 4.804 lands a
        ten-trillionth below 528.258 and the approval was refused for it; the
        same five in another order land exactly on it and the approval passed.
        HANA chooses the order.
        """
        request = self.make_request(('PM0000276', Decimal('536.196'), 'BH-PC'))
        line = request.lines.first()
        stock = {'PM0000276': {'warehouses': whs(
            ('BH-BT', '240'), ('BH-GR', '0.004'), ('BH-WST', '1.010'),
            ('GP-NM', '282.440'), ('BH-PC', '4.804'),
        )}}

        offered = self.options(request, stock)[line.id]['total_available']
        self.assertEqual(offered, Decimal('523.454'))

        self.approve(request, stock, [
            {'line_id': line.id, 'approved_qty': offered, 'status': 'APPROVED'},
        ])
        line.refresh_from_db()
        self.assertEqual(line.approved_qty, Decimal('523.454'))


class ChosenSourceTests(BOMSourceTestCase):
    """The approver says which godowns the quantity comes out of."""

    def setUp(self):
        super().setUp()
        self.request = self.make_request(('PM0000276', 500, 'BH-PC'))
        self.line = self.request.lines.first()
        self.stock = {'PM0000276': {'warehouses': whs(
            ('BH-PC', 4), ('BH-BT', 240), ('GP-NM', 282), ('BH-WST', 1),
        )}}

    def test_the_chosen_godowns_are_recorded_against_the_line(self):
        self.approve(self.request, self.stock, [{
            'line_id': self.line.id, 'approved_qty': Decimal('300'),
            'status': 'APPROVED',
            'sources': [
                {'warehouse': 'BH-BT', 'qty': Decimal('240')},
                {'warehouse': 'GP-NM', 'qty': Decimal('60')},
            ],
        }])

        self.assertEqual(
            {(s.warehouse_code, s.qty) for s in self.line.sources.all()},
            {('BH-BT', Decimal('240.000')), ('GP-NM', Decimal('60.000'))},
        )

    def test_the_chosen_godowns_must_add_up_to_the_approved_quantity(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.request, self.stock, [{
                'line_id': self.line.id, 'approved_qty': Decimal('300'),
                'status': 'APPROVED',
                'sources': [{'warehouse': 'BH-BT', 'qty': Decimal('240')}],
            }])

        self.assertIn('240', str(caught.exception))
        self.assertIn('300', str(caught.exception))

    def test_a_godown_cannot_be_drawn_beyond_what_it_holds(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.request, self.stock, [{
                'line_id': self.line.id, 'approved_qty': Decimal('300'),
                'status': 'APPROVED',
                'sources': [{'warehouse': 'BH-BT', 'qty': Decimal('300')}],
            }])

        self.assertIn('BH-BT', str(caught.exception))

    def test_the_consumption_warehouse_cannot_be_chosen(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.request, self.stock, [{
                'line_id': self.line.id, 'approved_qty': Decimal('4'),
                'status': 'APPROVED',
                'sources': [{'warehouse': 'BH-PC', 'qty': Decimal('4')}],
            }])

        self.assertIn('BH-PC', str(caught.exception))

    def test_a_caller_that_names_no_godown_is_filled_from_the_fullest_down(self):
        """An older client still cannot approve more than the store can supply."""
        self.approve(self.request, self.stock, [{
            'line_id': self.line.id, 'approved_qty': Decimal('300'),
            'status': 'APPROVED',
        }])

        self.assertEqual(
            {(s.warehouse_code, s.qty) for s in self.line.sources.all()},
            {('GP-NM', Decimal('282.000')), ('BH-BT', Decimal('18.000'))},
        )

    def test_a_revised_approval_does_not_leave_the_old_godowns_holding_stock(self):
        self.approve(self.request, self.stock, [{
            'line_id': self.line.id, 'approved_qty': Decimal('240'),
            'status': 'APPROVED',
            'sources': [{'warehouse': 'BH-BT', 'qty': Decimal('240')}],
        }])
        self.request.status = BOMRequestStatus.PENDING
        self.request.save(update_fields=['status'])

        self.approve(self.request, self.stock, [{
            'line_id': self.line.id, 'approved_qty': Decimal('100'),
            'status': 'APPROVED',
            'sources': [{'warehouse': 'GP-NM', 'qty': Decimal('100')}],
        }])

        self.assertEqual(
            [(s.warehouse_code, s.qty) for s in self.line.sources.all()],
            [('GP-NM', Decimal('100.000'))],
        )

    def test_a_rejected_line_holds_no_godown(self):
        self.approve(self.request, self.stock, [{
            'line_id': self.line.id, 'status': 'REJECTED',
        }])

        self.assertEqual(self.line.sources.count(), 0)


class LiveClaimTests(BOMSourceTestCase):
    """An approved line holds its godown until the material is handed over."""

    def setUp(self):
        super().setUp()
        self.stock = {'PM0000276': {'warehouses': whs(('BH-PC', 0), ('BH-BT', 240))}}
        self.first = self.make_request(('PM0000276', 240, 'BH-PC'))
        self.approve(self.first, self.stock, [{
            'line_id': self.first.lines.first().id,
            'approved_qty': Decimal('240'), 'status': 'APPROVED',
        }])
        self.second_run = ProductionRun.objects.create(
            company=self.company, run_number=2, date='2026-09-18',
            line=self.line, product='FG0000372', item_code='FG0000372',
            required_qty=Decimal('541'), status=RunStatus.DRAFT,
        )
        self.second = self.make_request(
            ('PM0000276', 240, 'BH-PC'), run=self.second_run,
        )

    def test_a_second_run_is_not_offered_stock_the_first_already_holds(self):
        found = self.options(self.second, self.stock)[self.second.lines.first().id]

        self.assertEqual(found['options'][0]['on_hand'], Decimal('240'))
        self.assertEqual(found['options'][0]['claimed'], Decimal('240'))
        self.assertEqual(found['total_available'], Decimal('0'))

    def test_the_refusal_says_the_stock_is_spoken_for_not_absent(self):
        with self.assertRaises(ValueError) as caught:
            self.approve(self.second, self.stock, [{
                'line_id': self.second.lines.first().id,
                'approved_qty': Decimal('240'), 'status': 'APPROVED',
                'sources': [{'warehouse': 'BH-BT', 'qty': Decimal('240')}],
            }])

        self.assertIn('already approved for another run', str(caught.exception))

    def test_a_line_holds_only_what_it_has_not_yet_handed_over(self):
        held = self.first.lines.first()
        held.issued_qty = Decimal('90')
        held.save(update_fields=['issued_qty'])

        found = self.options(self.second, self.stock)[self.second.lines.first().id]

        self.assertEqual(found['options'][0]['claimed'], Decimal('150'))

    def test_a_fully_issued_line_holds_nothing(self):
        """The stock has physically left; SAP's on-hand has already moved."""
        held = self.first.lines.first()
        held.issued_qty = Decimal('240')
        held.save(update_fields=['issued_qty'])

        found = self.options(self.second, self.stock)[self.second.lines.first().id]

        self.assertEqual(found['options'][0]['claimed'], Decimal('0'))
        self.assertEqual(found['total_available'], Decimal('240'))

    def test_a_rejected_request_holds_nothing(self):
        self.first.status = BOMRequestStatus.REJECTED
        self.first.save(update_fields=['status'])

        found = self.options(self.second, self.stock)[self.second.lines.first().id]

        self.assertEqual(found['total_available'], Decimal('240'))

    def test_a_request_never_claims_against_itself(self):
        """Re-approving one request must not read its own previous allocation."""
        self.first.status = BOMRequestStatus.PENDING
        self.first.save(update_fields=['status'])

        found = self.options(self.first, self.stock)[self.first.lines.first().id]

        self.assertEqual(found['total_available'], Decimal('240'))


class RepeatedComponentTests(BOMSourceTestCase):
    """A bill can name the same component twice."""

    def test_two_lines_of_one_item_do_not_each_get_the_whole_godown(self):
        request = self.make_request(
            ('PM0000276', 200, 'BH-PC'), ('PM0000276', 200, 'BH-PC'),
        )
        first, second = list(request.lines.all())
        stock = {'PM0000276': {'warehouses': whs(('BH-BT', 300))}}

        with self.assertRaises(ValueError) as caught:
            self.approve(request, stock, [
                {'line_id': first.id, 'approved_qty': Decimal('200'),
                 'status': 'APPROVED'},
                {'line_id': second.id, 'approved_qty': Decimal('200'),
                 'status': 'APPROVED'},
            ])

        self.assertIn('100', str(caught.exception))


class RawMaterialSourceTests(BOMSourceTestCase):
    """Raw material is settled against the register, which has no staging."""

    def test_the_register_warehouse_is_not_treated_as_a_consumption_warehouse(self):
        """BH-LO is both the bill's warehouse and the register's.

        Excluding "the consumption warehouse" here would exclude the only
        evidence an RM approval has, and every oil request would read zero.
        """
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code='BH-LO',
            item_code='RM0000001', qty=Decimal('32000'),
            as_of_date='2026-09-18', uom='LTR',
        )
        request = self.make_request(
            ('RM0000001', 3278, 'BH-LO'), kind=BOMMaterialKind.RAW,
        )
        line = request.lines.first()

        found = self.service.source_options_for_request(request)[line.id]

        self.assertEqual([o['warehouse'] for o in found['options']], ['BH-LO'])
        self.assertEqual(found['total_available'], Decimal('32000'))


class ConsumptionSplitTests(BOMSourceTestCase):
    """The request is raised against the line's own consumption warehouse."""

    def usage(self, code, qty):
        return ProductionMaterialUsage.objects.create(
            production_run=self.run, material_code=code, material_name=code,
            opening_qty=Decimal(str(qty)), uom='PCS',
        )

    def create(self, *, material_types, stock, components):
        with patch.object(WarehouseService, '_material_types',
                          return_value=material_types), \
             patch.object(WarehouseService, '_resource_codes', return_value=set()), \
             patch.object(WarehouseService, 'get_stock_for_items', return_value=stock), \
             patch.object(WarehouseService, '_fetch_bom_components',
                          return_value=components):
            return self.service.create_bom_request(
                {'production_run_id': self.run.id, 'required_qty': Decimal('541')},
                user=None,
            )

    def test_a_bh_pp_line_is_narrowed_by_bh_pp_not_by_bh_pc(self):
        """Beverages stages at BH-PP. The old rule asked for the lot regardless."""
        self.usage('PM0000654', 5000)
        raised = self.create(
            material_types={'PM0000654': 'PACKAGING'},
            stock={'PM0000654': {'warehouses': [
                {'WhsCode': 'BH-PP', 'OnHand': 4000},
                {'WhsCode': 'BH-PM', 'OnHand': 9000},
            ]}},
            components=[{
                'ItemCode': 'PM0000654', 'ItemName': 'Bev label',
                'Warehouse': 'BH-PP', 'UomCode': 'PCS', 'LineNum': 0,
            }],
        )

        line = raised[0].lines.first()
        self.assertEqual(line.warehouse, 'BH-PP')
        self.assertEqual(line.required_qty, Decimal('1000.000'))
        self.assertIn('BH-PP', line.remarks)
