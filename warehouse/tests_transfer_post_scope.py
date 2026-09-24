"""Only the people on a transfer request may post its stock.

Posting is what moves stock in SAP. It belongs to the one who raised the request
and the one who approved it — not to anyone else who holds the post permission.
"""

from unittest import mock

from django.test import TestCase
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from company.models import Company
from warehouse.models_manager import UserWarehouse
from warehouse.services.transfer_request_service import TransferRequestService


class _FakeSAP:
    context = None

    def batch_managed_flags(self, item_codes):
        return {}

    def create_transfer_request(self, payload):
        return {'DocEntry': 901, 'DocNum': 5001}


class PostScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")

        def user(tag):
            return User.objects.create_user(
                email=f"{tag}@example.com", full_name=tag,
                employee_code=f"E-{tag}", password="x",
            )

        cls.sender = user("sender")
        cls.receiver = user("receiver")
        cls.bystander = user("bystander")
        UserWarehouse.objects.create(user=cls.sender, company=cls.oil, warehouse_code="BH-LO")
        UserWarehouse.objects.create(user=cls.receiver, company=cls.oil, warehouse_code="BH-PC")
        # Manages both ends and could approve either — but is on neither request.
        UserWarehouse.objects.create(user=cls.bystander, company=cls.oil, warehouse_code="BH-LO")
        UserWarehouse.objects.create(user=cls.bystander, company=cls.oil, warehouse_code="BH-PC")

    def setUp(self):
        for target, kwargs in (
            ('warehouse.services.transfer_request_service.HanaSeriesReader', {}),
            ('warehouse.services.transfer_request_service.build_transfer_request_payload',
             {'return_value': {}}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        request = self.service(self.sender).create_request({
            'from_warehouse': 'BH-LO',
            'to_warehouse': 'BH-PC',
            'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1500'}],
        })
        self.request = self.service(self.receiver).approve(request.id, {})

    def service(self, user):
        svc = TransferRequestService("JIVO_OIL", user)
        svc._client = _FakeSAP()
        svc._branches = {"BH-LO": 1, "BH-PC": 1}
        return svc

    def test_the_requester_and_the_approver_may_post(self):
        for person in (self.sender, self.receiver):
            self.service(person)._assert_can_post(self.request)

    def test_nobody_else_may_post(self):
        with self.assertRaises(PermissionDenied):
            self.service(self.bystander).post_transfer(self.request.id)

    def test_nobody_else_may_open_the_batch_split(self):
        with self.assertRaises(PermissionDenied):
            self.service(self.bystander).allocation_preview(self.request.id)

    def test_before_approval_only_the_requester_is_on_it(self):
        pending = self.service(self.sender).create_request({
            'from_warehouse': 'BH-LO',
            'to_warehouse': 'BH-PC',
            'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '10'}],
        })
        self.service(self.sender)._assert_can_post(pending)
        with self.assertRaises(PermissionDenied):
            self.service(self.receiver)._assert_can_post(pending)
