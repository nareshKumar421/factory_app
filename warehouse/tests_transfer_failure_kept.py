"""A transfer SAP did not take keeps the reason, after the rollback.

`_post_and_record` writes FAILED and SAP's message as it re-raises, but inside
the posting's atomic block, so the write used to roll back with everything else:
the request came back looking untried, and a timeout's "it may still have been
created, check SAP" was lost with it. `_keeps_posting_failure` writes it again
from outside the block.
"""

from unittest import mock

from django.db import transaction
from django.test import TestCase

from accounts.models import User
from company.models import Company
from sap_client.exceptions import SAPOutcomeUnknown, SAPUnavailable
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import TransferPostingStatus, WarehouseTransferRequest
from warehouse.services.transfer_request_service import (
    TransferRequestService,
    _keeps_posting_failure,
)


class _FakeSAP:
    context = None

    def __init__(self, fail_with=None):
        self.fail_with = fail_with

    def batch_managed_flags(self, item_codes):
        return {}

    def create_transfer_request(self, payload):
        return {'DocEntry': 901, 'DocNum': 5001}

    def create_stock_transfer(self, payload):
        raise self.fail_with


class _PostingService(TransferRequestService):
    """The real record-and-raise, wrapped the way `post_transfer` is."""

    @_keeps_posting_failure
    @transaction.atomic
    def post(self, request_id):
        request = WarehouseTransferRequest.objects.get(pk=request_id)
        request.remarks = "touched inside the posting"  # rolls back
        request.save(update_fields=['remarks', 'updated_at'])
        return self._post_and_record(request, {}, is_second_leg=False)

    @_keeps_posting_failure
    @transaction.atomic
    def fail_before_posting(self, request_id):
        raise SAPUnavailable("Unable to connect to SAP HANA.")


class PostingFailureKeptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.sender = User.objects.create_user(
            email="sender@example.com", full_name="sender",
            employee_code="E-sender", password="x",
        )
        cls.receiver = User.objects.create_user(
            email="receiver@example.com", full_name="receiver",
            employee_code="E-receiver", password="x",
        )
        UserWarehouse.objects.create(user=cls.sender, company=cls.oil, warehouse_code="BH-LO")
        UserWarehouse.objects.create(user=cls.receiver, company=cls.oil, warehouse_code="BH-PC")

    def setUp(self):
        for target, kwargs in (
            ('warehouse.services.transfer_request_service.HanaSeriesReader', {}),
            ('warehouse.services.transfer_request_service.build_transfer_request_payload',
             {'return_value': {}}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        created = self.service(self.sender).create_request({
            'from_warehouse': 'BH-LO',
            'to_warehouse': 'BH-PC',
            'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1500'}],
        })
        self.request = self.service(self.receiver).approve(created.id, {})
        self.status_before = self.request.posting_status

    def service(self, user, fail_with=None, cls=TransferRequestService):
        svc = cls("JIVO_OIL", user)
        svc._client = _FakeSAP(fail_with)
        svc._branches = {"BH-LO": 1, "BH-PC": 1}
        return svc

    def test_sap_not_answering_leaves_failed_and_the_reason(self):
        svc = self.service(
            self.sender, SAPOutcomeUnknown("The document may still have been created"),
            cls=_PostingService,
        )
        with self.assertRaises(SAPOutcomeUnknown):
            svc.post(self.request.id)

        self.request.refresh_from_db()
        self.assertEqual(self.request.posting_status, TransferPostingStatus.FAILED)
        self.assertIn("may still have been created", self.request.posting_error)
        # Only the failure is kept; the rest of the posting still rolled back.
        self.assertNotEqual(self.request.remarks, "touched inside the posting")

    def test_a_read_that_failed_before_posting_changes_nothing(self):
        svc = self.service(self.sender, cls=_PostingService)
        with self.assertRaises(SAPUnavailable):
            svc.fail_before_posting(self.request.id)

        self.request.refresh_from_db()
        self.assertEqual(self.request.posting_status, self.status_before)
        self.assertEqual(self.request.posting_error, "")

    def test_the_real_entry_points_carry_it(self):
        # The outermost layer must be the keeper: `transaction.atomic` sets
        # `__wrapped__` too, so compare the wrapper's code, not its presence.
        keeper = _keeps_posting_failure(lambda self, request_id: None).__code__
        for name in ("post_transfer", "post_second_leg"):
            self.assertIs(
                getattr(TransferRequestService, name).__code__, keeper,
                f"{name} lost _keeps_posting_failure",
            )
