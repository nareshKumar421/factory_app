"""Tests for a transfer request raised by the receiving warehouse.

The production floor runs BH-PC and has to ask the oil store, BH-LO, for oil.
So the receiving side may raise a request, and the side that did not raise it
decides: the sender agrees to hand over stock that was asked for, exactly as
the receiver accepts stock that was offered.
"""

from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from company.models import Company
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import (
    TransferRaisedBy,
    TransferRequestStatus,
    WarehouseTransferRequest,
)
from warehouse.services.transfer_request_service import TransferRequestService


class _FakeSAP:
    context = None

    def __init__(self):
        self.closed = []

    def batch_managed_flags(self, item_codes):
        return {}

    def create_transfer_request(self, payload):
        return {'DocEntry': 901, 'DocNum': 5001}

    def close_transfer_request(self, doc_entry):
        self.closed.append(doc_entry)


class ReceiverRaisedTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.floor = User.objects.create_user(
            email="pc@example.com", full_name="Production Floor",
            employee_code="E-PC", password="x",
        )
        cls.store = User.objects.create_user(
            email="lo@example.com", full_name="Oil Store",
            employee_code="E-LO", password="x",
        )
        UserWarehouse.objects.create(user=cls.floor, company=cls.oil, warehouse_code="BH-PC")
        UserWarehouse.objects.create(user=cls.store, company=cls.oil, warehouse_code="BH-LO")

    def setUp(self):
        patches = [
            mock.patch(
                'warehouse.services.transfer_request_service.HanaSeriesReader'
            ),
            mock.patch(
                'warehouse.services.transfer_request_service.build_transfer_request_payload',
                return_value={},
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def service(self, user):
        svc = TransferRequestService("JIVO_OIL", user)
        svc._client = _FakeSAP()
        svc._branches = {"BH-LO": 1, "BH-PC": 1}
        return svc

    def ask_for_oil(self, user=None, side=TransferRaisedBy.RECEIVER):
        return self.service(user or self.floor).create_request({
            'from_warehouse': 'BH-LO',
            'to_warehouse': 'BH-PC',
            'raised_by_side': side,
            'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1500'}],
        })

    # ---- raising ----------------------------------------------------------

    def test_the_receiving_manager_can_ask_for_stock(self):
        request = self.ask_for_oil()
        self.assertEqual(request.raised_by_side, TransferRaisedBy.RECEIVER)
        self.assertEqual(request.requested_by, self.floor)
        self.assertEqual(request.sap_request_doc_entry, 901)
        self.assertEqual(request.deciding_warehouse, 'BH-LO')

    def test_asking_needs_the_receiving_warehouse(self):
        with self.assertRaises(PermissionDenied):
            self.ask_for_oil(user=self.store)
        self.assertFalse(WarehouseTransferRequest.objects.exists())

    def test_offering_still_needs_the_sending_warehouse(self):
        with self.assertRaises(PermissionDenied):
            self.ask_for_oil(user=self.floor, side=TransferRaisedBy.SENDER)
        offered = self.ask_for_oil(user=self.store, side=TransferRaisedBy.SENDER)
        self.assertEqual(offered.deciding_warehouse, 'BH-PC')

    # ---- deciding ---------------------------------------------------------

    def test_the_sender_decides_a_request_that_was_asked_for(self):
        request = self.ask_for_oil()
        with self.assertRaises(PermissionDenied):
            self.service(self.floor).approve(request.id, {})
        approved = self.service(self.store).approve(
            request.id, {'lines': [{'line_num': 0, 'approved_qty': '1000'}]}
        )
        self.assertEqual(approved.status, TransferRequestStatus.PARTIALLY_APPROVED)
        self.assertEqual(approved.lines.get().approved_qty, Decimal('1000'))

    def test_the_sender_can_refuse_it(self):
        request = self.ask_for_oil()
        with self.assertRaises(PermissionDenied):
            self.service(self.floor).reject(request.id, 'changed my mind')
        store = self.service(self.store)
        rejected = store.reject(request.id, 'Tank is low')
        self.assertEqual(rejected.status, TransferRequestStatus.REJECTED)
        self.assertEqual(store.client.closed, [901])

    def test_the_receiver_still_decides_an_offer(self):
        offered = self.ask_for_oil(user=self.store, side=TransferRaisedBy.SENDER)
        with self.assertRaises(PermissionDenied):
            self.service(self.store).approve(offered.id, {})
        self.assertEqual(
            self.service(self.floor).approve(offered.id, {}).status,
            TransferRequestStatus.APPROVED,
        )

    # ---- the requester's own list ----------------------------------------

    def test_mine_lists_only_what_this_user_raised(self):
        mine = self.ask_for_oil()
        self.ask_for_oil(user=self.store, side=TransferRaisedBy.SENDER)
        listed = self.service(self.floor).list_requests(
            mine=True, raised_by_side=TransferRaisedBy.RECEIVER,
        )
        self.assertEqual([r.id for r in listed], [mine.id])
