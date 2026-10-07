"""A transfer SAP made whose reply never reached the app is recorded, not posted twice.

TR-20261003-0002: SAP committed the transfer, the Service Layer never answered,
a restart killed the worker still waiting, and the request sat "not posted"
while its stock had moved. The next press of Post was refused — the first had
used SAP's request up. Posting now looks for the app's own transfer first, and
again when SAP refuses or times out, and records the one it finds.
"""

from datetime import date, datetime, timezone as dtz
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase

from accounts.models import User
from company.models import Company
from sap_client.exceptions import SAPConnectionError, SAPOutcomeUnknown, SAPValidationError
from sap_client.hana.stock_transfer_reader import HanaStockTransferReader
from sap_client.service_layer.stock_transfer_writer import (
    BASE_TYPE_STOCK_TRANSFER,
    BASE_TYPE_TRANSFER_REQUEST,
)
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import (
    TransferPostingStatus,
    TransferRouteType,
    WarehouseTransferRequest,
)
from warehouse.services.transfer_request_service import TransferRequestService

SERVICE = 'warehouse.services.transfer_request_service'

LOST = {
    'doc_entry': 22619,
    'doc_num': '1026676516',
    'doc_date': date(2026, 10, 3),
    'create_ts': 155905,
    'lines': [{
        'line_num': 0, 'base_line': 0, 'item_code': 'FG0000461',
        'quantity': Decimal('6900'),
        'batches': [
            {'BatchNumber': 'L3003286 102601 01', 'Quantity': 940.0},
            {'BatchNumber': 'L3003686 102603 01', 'Quantity': 5960.0},
        ],
    }],
}


class _FakeSAP:
    context = None

    def __init__(self):
        self.posted = []
        self.post_fails_with = None

    def batch_managed_flags(self, item_codes):
        return {}

    def create_transfer_request(self, payload):
        return {'DocEntry': 2871, 'DocNum': 1026656502}

    def create_stock_transfer(self, payload):
        self.posted.append(payload)
        if self.post_fails_with:
            raise self.post_fails_with
        return {'DocEntry': 30001, 'DocNum': 1026690001}


class _LostPostCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.gautam = User.objects.create_user(
            email="gautam@example.com", full_name="Gautam", employee_code="E-g", password="x",
        )
        for code in ("BH-PF", "BH-PTD"):
            UserWarehouse.objects.create(user=cls.gautam, company=cls.oil, warehouse_code=code)

    def setUp(self):
        for target, kwargs in (
            ('HanaSeriesReader', {}),
            ('build_transfer_request_payload', {'return_value': {}}),
            ('build_stock_transfer_payload', {'return_value': {}}),
        ):
            patcher = mock.patch(f'{SERVICE}.{target}', **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        reader = mock.patch(f'{SERVICE}.HanaStockTransferReader')
        self.find = reader.start().return_value.find_by_base
        self.find.return_value = None
        self.addCleanup(reader.stop)

        self.sap = _FakeSAP()
        created = self.service().create_request({
            'from_warehouse': 'BH-PF',
            'to_warehouse': 'BH-PTD',
            'lines': [{'item_code': 'FG0000461', 'uom': 'PCS', 'quantity': '6900'}],
        })
        self.request = self.service().approve(created.id, {})

    def service(self):
        svc = TransferRequestService("JIVO_OIL", self.gautam)
        svc._client = self.sap
        svc._branches = {"BH-PF": 1, "BH-PTD": 1}
        return svc

    def reload(self):
        return WarehouseTransferRequest.objects.get(pk=self.request.pk)


class AdoptLostLegOneTests(_LostPostCase):
    def test_a_transfer_sap_already_made_is_recorded_not_posted_again(self):
        self.find.return_value = LOST
        self.service().post_transfer(self.request.id)

        self.assertEqual(self.sap.posted, [])
        self.find.assert_called_once_with(
            BASE_TYPE_TRANSFER_REQUEST, 2871, f"App transfer request {self.request.entry_no}",
        )
        request = self.reload()
        self.assertEqual(
            (request.posting_status, request.sap_transfer_doc_entry, request.sap_transfer_doc_num),
            (TransferPostingStatus.POSTED, 22619, '1026676516'),
        )
        # When SAP made it (15:59:05 IST), not when the app found out.
        self.assertEqual(request.posted_at, datetime(2026, 10, 3, 10, 29, 5, tzinfo=dtz.utc))
        line = request.lines.get()
        self.assertEqual(line.transferred_qty, Decimal('6900'))
        self.assertEqual([b['BatchNumber'] for b in line.batch_allocation],
                         ['L3003286 102601 01', 'L3003686 102603 01'])

    def test_with_nothing_in_sap_it_posts(self):
        self.service().post_transfer(self.request.id)
        self.assertEqual(len(self.sap.posted), 1)
        self.assertEqual(self.reload().sap_transfer_doc_entry, 30001)

    def test_refused_because_the_first_post_landed_meanwhile(self):
        # "One of the base documents has already been closed": the lost post
        # finished between the check and this post.
        self.sap.post_fails_with = SAPValidationError(
            "One of the base documents has already been closed  (SAP -10)"
        )
        self.find.side_effect = [None, LOST]
        self.service().post_transfer(self.request.id)

        request = self.reload()
        self.assertEqual(request.posting_status, TransferPostingStatus.POSTED)
        self.assertEqual(request.sap_transfer_doc_entry, 22619)
        self.assertEqual(request.posting_error, '')

    def test_a_timeout_that_did_post_is_recorded(self):
        self.sap.post_fails_with = SAPOutcomeUnknown("SAP did not answer within 180s")
        self.find.side_effect = [None, LOST]
        self.service().post_transfer(self.request.id)
        self.assertEqual(self.reload().sap_transfer_doc_entry, 22619)

    def test_a_timeout_that_did_not_post_still_fails(self):
        self.sap.post_fails_with = SAPOutcomeUnknown("SAP did not answer within 180s")
        with self.assertRaises(SAPOutcomeUnknown):
            self.service().post_transfer(self.request.id)
        request = self.reload()
        self.assertEqual(request.posting_status, TransferPostingStatus.FAILED)
        self.assertIn("did not answer", request.posting_error)

    def test_a_failed_lookup_never_hides_the_posts_own_error(self):
        self.sap.post_fails_with = SAPOutcomeUnknown("SAP did not answer within 180s")
        self.find.side_effect = [None, SAPConnectionError("HANA down")]
        with self.assertRaises(SAPOutcomeUnknown):
            self.service().post_transfer(self.request.id)
        self.assertEqual(self.reload().posting_status, TransferPostingStatus.FAILED)


class AdoptLostLegTwoTests(_LostPostCase):
    def setUp(self):
        super().setUp()
        WarehouseTransferRequest.objects.filter(pk=self.request.pk).update(
            route_type=TransferRouteType.CROSS_BRANCH, intransit_warehouse='BH-INT',
            posting_status=TransferPostingStatus.IN_TRANSIT,
            sap_transfer_doc_entry=22000, sap_transfer_doc_num='1026670000',
        )
        self.request.lines.update(transferred_qty=Decimal('6900'))

    def test_a_lost_second_leg_is_recorded(self):
        self.find.return_value = {**LOST, 'doc_entry': 22700, 'doc_num': '1026676600'}
        self.service().post_second_leg(self.request.id)

        self.assertEqual(self.sap.posted, [])
        self.find.assert_called_once_with(
            BASE_TYPE_STOCK_TRANSFER, 22000,
            f"App transfer request {self.request.entry_no} — leg 2",
        )
        request = self.reload()
        self.assertEqual(
            (request.posting_status, request.sap_leg2_doc_entry, request.sap_leg2_doc_num),
            (TransferPostingStatus.POSTED, 22700, '1026676600'),
        )
        # Leg 1's moved quantity stands; leg 2 adds nothing to it.
        self.assertEqual(request.lines.get().transferred_qty, Decimal('6900'))


class FindByBaseTests(SimpleTestCase):
    """The reader turns OWTR/WTR1 + IBT1 rows into the one transfer."""

    def reader(self, header_rows, batch_rows):
        cursor = mock.Mock()
        cursor.fetchall.side_effect = [header_rows, batch_rows]
        connection = mock.Mock(schema='JIVO_OIL_HANADB')
        connection.connect.return_value.cursor.return_value = cursor
        reader = HanaStockTransferReader.__new__(HanaStockTransferReader)
        reader.connection = connection
        return reader, cursor

    def test_reads_lines_and_batches(self):
        reader, cursor = self.reader(
            [(22619, 1026676516, datetime(2026, 10, 3), 155905, 0, 0, 'FG0000461', Decimal('6900'))],
            [(0, 'L3003286 102601 01', Decimal('940')), (0, 'L3003686 102603 01', Decimal('5960'))],
        )
        found = reader.find_by_base(1250000001, 2871, "App transfer request TR-20261003-0002")
        self.assertEqual((found['doc_entry'], found['doc_num'], found['doc_date'], found['create_ts']),
                         (22619, '1026676516', date(2026, 10, 3), 155905))
        self.assertEqual(found['lines'][0]['base_line'], 0)
        self.assertEqual(found['lines'][0]['batches'], [
            {'BatchNumber': 'L3003286 102601 01', 'Quantity': 940.0},
            {'BatchNumber': 'L3003686 102603 01', 'Quantity': 5960.0},
        ])
        # Matched on base AND the app's own comment, never on either alone.
        self.assertEqual(cursor.execute.call_args_list[0].args[1],
                         (1250000001, 2871, "App transfer request TR-20261003-0002"))

    def test_nothing_found(self):
        reader, _ = self.reader([], [])
        self.assertIsNone(reader.find_by_base(1250000001, 2871, "App transfer request X"))
