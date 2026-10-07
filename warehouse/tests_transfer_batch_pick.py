"""Batches picked when a transfer request is raised.

The requester may pin a batch-tracked line to particular batches on the raise
form — a customer asking for one production date, or clearing the older run
first. The pick never reaches SAP's request (which only says what is wanted);
it is kept on the line, shown to the approver, and is where posting starts.
"""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase

from accounts.models import User
from company.models import Company
from sap_client.hana.batch_stock_reader import HanaBatchStockReader
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import (
    WarehouseTransferRequest,
    WarehouseTransferRequestLine,
)
from warehouse.services.transfer_request_service import (
    TransferRequestError,
    TransferRequestService,
)

FG = 'FG0000461'
PM = 'PM0000085'


class _Shelf(HanaBatchStockReader):
    """The real allocation logic over a fixed OIBT, keyed by warehouse."""

    def __init__(self, stock):
        self.stock = stock

    def available_batches(self, item_code, warehouse):
        return [
            {
                'batch_number': number,
                'quantity': Decimal(quantity),
                'status': '0',
                'in_date': None,
                'expiry_date': None,
                'production_date': None,
                'system_number': None,
            }
            for number, quantity in self.stock.get((item_code, warehouse), [])
        ]


class _FakeSAP:
    context = None

    def __init__(self, shelf):
        self.shelf = shelf
        self.raised = []
        self.closed = []

    def batch_managed_flags(self, item_codes):
        return {code: code.startswith('FG') for code in item_codes}

    def create_transfer_request(self, payload):
        self.raised.append(payload)
        return {'DocEntry': 900 + len(self.raised), 'DocNum': 5000 + len(self.raised)}

    def close_transfer_request(self, doc_entry):
        self.closed.append(doc_entry)

    def allocate_batches_fifo(self, item_code, warehouse, quantity):
        return self.shelf.allocate_fifo(item_code, warehouse, quantity)


def _user(tag):
    return User.objects.create_user(
        email=f"{tag}@example.com", full_name=tag, employee_code=f"E-{tag}", password="x",
    )


class _BatchPickCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.sender = _user("sender")
        cls.receiver = _user("receiver")
        UserWarehouse.objects.create(user=cls.sender, company=cls.oil, warehouse_code="BH-PF")
        UserWarehouse.objects.create(user=cls.receiver, company=cls.oil, warehouse_code="BH-BT")

    def setUp(self):
        series = mock.patch('warehouse.services.transfer_request_service.HanaSeriesReader')
        series.start().return_value.resolve_transfer_request.return_value = 7
        self.addCleanup(series.stop)
        # Oldest first, as OIBT is read.
        self.shelf = _Shelf({
            (FG, 'BH-PF'): [('L3003286 102603 02', '60'), ('L3003056 102605 01', '500')],
        })
        self.sap = _FakeSAP(self.shelf)

    def service(self, user):
        svc = TransferRequestService("JIVO_OIL", user)
        svc._client = self.sap
        svc._branches = {"BH-PF": 1, "BH-BT": 1}
        svc._batch_reader = lambda: self.shelf
        return svc

    def raise_request(self, batches=None, quantity='100', item=FG):
        line = {'item_code': item, 'uom': 'PCS', 'quantity': quantity}
        if batches is not None:
            line['batches'] = batches
        return self.service(self.sender).create_request({
            'from_warehouse': 'BH-PF',
            'to_warehouse': 'BH-BT',
            'lines': [line],
        })

    @staticmethod
    def line(request):
        return WarehouseTransferRequestLine.objects.get(request=request, line_num=0)


class RaiseWithBatchesTests(_BatchPickCase):
    def test_picked_batches_are_kept_on_the_line(self):
        request = self.raise_request([
            {'batch_number': 'L3003286 102603 02', 'quantity': Decimal('60.000')},
            {'batch_number': 'L3003056 102605 01', 'quantity': Decimal('40.000')},
        ])
        self.assertEqual(self.line(request).chosen_batches, [
            {'batch_number': 'L3003286 102603 02', 'quantity': '60'},
            {'batch_number': 'L3003056 102605 01', 'quantity': '40'},
        ])

    def test_the_pick_never_reaches_sap_request(self):
        self.raise_request([{'batch_number': 'L3003056 102605 01', 'quantity': '100'}])
        line = self.sap.raised[-1]['StockTransferLines'][0]
        self.assertNotIn('BatchNumbers', line)

    def test_nothing_picked_means_oldest_first(self):
        self.assertEqual(self.line(self.raise_request()).chosen_batches, [])

    def test_a_blank_take_is_not_a_pick(self):
        request = self.raise_request([
            {'batch_number': 'L3003286 102603 02', 'quantity': '0'},
            {'batch_number': 'L3003056 102605 01', 'quantity': '100'},
        ])
        self.assertEqual(self.line(request).chosen_batches, [
            {'batch_number': 'L3003056 102605 01', 'quantity': '100'},
        ])

    def test_a_pick_must_add_up_to_the_line(self):
        with self.assertRaisesMessage(TransferRequestError, 'add up to 60, but 100 is asked for'):
            self.raise_request([{'batch_number': 'L3003286 102603 02', 'quantity': '60'}])
        self.assertFalse(WarehouseTransferRequest.objects.exists())
        self.assertEqual(self.sap.raised, [])

    def test_a_batch_cannot_give_more_than_it_holds(self):
        # A user-fixable refusal, so the request error (400), not SAP's (502).
        with self.assertRaisesMessage(TransferRequestError, 'holds 60'):
            self.raise_request([{'batch_number': 'L3003286 102603 02', 'quantity': '100'}])

    def test_a_batch_that_is_not_on_the_shelf_is_refused(self):
        with self.assertRaisesMessage(TransferRequestError, 'not available'):
            self.raise_request([{'batch_number': 'L9999999', 'quantity': '100'}])

    def test_an_untracked_item_has_no_batches_to_pick(self):
        with self.assertRaisesMessage(TransferRequestError, 'not batch-tracked'):
            self.raise_request([{'batch_number': 'X', 'quantity': '100'}], item=PM)


class EditBatchesTests(_BatchPickCase):
    def edit(self, request, batches, quantity='100'):
        line = {'item_code': FG, 'uom': 'PCS', 'quantity': quantity}
        if batches is not None:
            line['batches'] = batches
        return self.service(self.sender).update_request(request.id, {'lines': [line]})

    def test_changing_only_the_batches_leaves_sap_alone(self):
        request = self.raise_request()
        before = request.updated_at
        edited = self.edit(request, [{'batch_number': 'L3003056 102605 01', 'quantity': '100'}])
        self.assertEqual(self.line(edited).chosen_batches, [
            {'batch_number': 'L3003056 102605 01', 'quantity': '100'},
        ])
        self.assertEqual(len(self.sap.raised), 1)
        self.assertEqual(self.sap.closed, [])
        # Moves, so an approver still looking at the old picks is refused.
        self.assertGreater(edited.updated_at, before)

    def test_leaving_the_batches_out_clears_the_pick(self):
        request = self.raise_request([{'batch_number': 'L3003056 102605 01', 'quantity': '100'}])
        edited = self.edit(request, None)
        self.assertEqual(self.line(edited).chosen_batches, [])

    def test_a_new_quantity_needs_the_pick_to_follow(self):
        request = self.raise_request([{'batch_number': 'L3003056 102605 01', 'quantity': '100'}])
        with self.assertRaisesMessage(TransferRequestError, 'add up to 100, but 120 is asked for'):
            self.edit(request, [{'batch_number': 'L3003056 102605 01', 'quantity': '100'}], '120')


class PostFromThePickTests(_BatchPickCase):
    def setUp(self):
        super().setUp()
        request = self.raise_request([
            {'batch_number': 'L3003286 102603 02', 'quantity': '60'},
            {'batch_number': 'L3003056 102605 01', 'quantity': '40'},
        ])
        # The receiver takes 80 of the 100.
        self.request = self.service(self.receiver).approve(
            request.id, {'lines': [{'line_num': 0, 'approved_qty': Decimal('80')}]}
        )

    def test_preview_starts_from_the_pick_cut_to_what_was_approved(self):
        line = self.service(self.sender).allocation_preview(self.request.id)['lines'][0]
        expected = [
            {'BatchNumber': 'L3003286 102603 02', 'Quantity': 60.0},
            {'BatchNumber': 'L3003056 102605 01', 'Quantity': 20.0},
        ]
        self.assertEqual(line['chosen'], expected)
        self.assertEqual(line['proposed'], expected)
        self.assertEqual(line['note'], '')

    def test_preview_says_when_a_picked_batch_has_moved(self):
        self.shelf.stock[(FG, 'BH-PF')] = [('L3003056 102605 01', '500')]
        line = self.service(self.sender).allocation_preview(self.request.id)['lines'][0]
        # Never proposes a batch that is not there; the gap is left to fill.
        self.assertEqual(line['proposed'], [
            {'BatchNumber': 'L3003056 102605 01', 'Quantity': 20.0},
        ])
        self.assertIn('L3003286 102603 02 is no longer in BH-PF', line['note'])
        self.assertEqual(line['error'], '')

    def test_posting_without_a_split_takes_the_pick(self):
        lines = self.service(self.sender)._build_transfer_lines(
            WarehouseTransferRequest.objects.get(pk=self.request.pk), 'BH-BT'
        )
        self.assertEqual(lines[0]['batches'], [
            {'BatchNumber': 'L3003286 102603 02', 'Quantity': 60.0},
            {'BatchNumber': 'L3003056 102605 01', 'Quantity': 20.0},
        ])

    def test_the_poster_split_still_wins(self):
        lines = self.service(self.sender)._build_transfer_lines(
            WarehouseTransferRequest.objects.get(pk=self.request.pk), 'BH-BT',
            {0: [{'BatchNumber': 'L3003056 102605 01', 'Quantity': 80}]},
        )
        self.assertEqual(lines[0]['batches'], [
            {'BatchNumber': 'L3003056 102605 01', 'Quantity': 80.0},
        ])


class ItemBatchesTests(_BatchPickCase):
    def test_lists_the_shelf_with_what_other_requests_picked(self):
        mine = self.raise_request([{'batch_number': 'L3003056 102605 01', 'quantity': '100'}])
        self.raise_request([{'batch_number': 'L3003056 102605 01', 'quantity': '30'}], '30')
        self.raise_request()  # oldest first, holding no batch in particular

        found = self.service(self.sender).item_batches('BH-PF', FG)
        self.assertTrue(found['is_batch_managed'])
        self.assertEqual(
            [(b['batch_number'], b['held_by_requests']) for b in found['batches']],
            [('L3003286 102603 02', Decimal('0')), ('L3003056 102605 01', Decimal('130'))],
        )

        editing = self.service(self.sender).item_batches(
            'BH-PF', FG, exclude_request_id=mine.id
        )
        self.assertEqual(editing['batches'][1]['held_by_requests'], Decimal('30'))

    def test_an_untracked_item_lists_nothing(self):
        found = self.service(self.sender).item_batches('BH-PF', PM)
        self.assertEqual((found['is_batch_managed'], found['batches']), (False, []))


class ChosenSplitTests(SimpleTestCase):
    def line(self, *picks):
        return WarehouseTransferRequestLine(chosen_batches=[
            {'batch_number': number, 'quantity': quantity} for number, quantity in picks
        ])

    def test_cut_in_the_order_picked(self):
        line = self.line(('A', '60'), ('B', '40'))
        self.assertEqual(line.chosen_split(Decimal('70')), [
            {'BatchNumber': 'A', 'Quantity': 60.0}, {'BatchNumber': 'B', 'Quantity': 10.0},
        ])

    def test_whole_pick_when_nothing_was_cut(self):
        line = self.line(('A', '60'), ('B', '40'))
        self.assertEqual(sum(b['Quantity'] for b in line.chosen_split(100)), 100)

    def test_nothing_picked_or_nothing_left(self):
        self.assertEqual(self.line().chosen_split(100), [])
        self.assertEqual(self.line(('A', '60')).chosen_split(0), [])
