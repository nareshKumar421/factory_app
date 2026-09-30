"""A transfer request stays editable by the person who raised it until it is decided.

Changing the lines replaces SAP's request rather than patching it — a new one
carries the edited lines and the old one is closed — because posting ties each
transfer line to its request line by number. Remarks never reach SAP.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from warehouse.models_manager import UserWarehouse
from warehouse.models_transfer import TransferRequestStatus, WarehouseTransferRequest
from warehouse.services.transfer_guards import TransferGuardError
from warehouse.services.transfer_request_service import (
    TransferRequestError,
    TransferRequestService,
)


class _FakeSAP:
    """SAP's transfer-request side, numbering each new request 901, 902, …"""

    context = None

    def __init__(self):
        self.raised = []
        self.closed = []
        self.refuse_raise = None
        self.refuse_close = set()
        self.open_in_sap = set()

    def batch_managed_flags(self, item_codes):
        return {}

    def create_transfer_request(self, payload):
        if self.refuse_raise:
            raise self.refuse_raise
        self.raised.append(payload)
        doc_entry = 900 + len(self.raised)
        self.open_in_sap.add(doc_entry)
        return {'DocEntry': doc_entry, 'DocNum': 5000 + len(self.raised)}

    def close_transfer_request(self, doc_entry):
        if doc_entry in self.refuse_close:
            raise SAPValidationError(f"Cannot close {doc_entry}")
        self.closed.append(doc_entry)
        self.open_in_sap.discard(doc_entry)

    def summarise_transfer_requests(self, doc_entries):
        return {d: {'is_open': d in self.open_in_sap} for d in doc_entries}


def _user(tag):
    return User.objects.create_user(
        email=f"{tag}@example.com", full_name=tag, employee_code=f"E-{tag}", password="x",
    )


class _TransferEditCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.sender = _user("sender")
        cls.receiver = _user("receiver")
        UserWarehouse.objects.create(user=cls.sender, company=cls.oil, warehouse_code="BH-LO")
        UserWarehouse.objects.create(user=cls.receiver, company=cls.oil, warehouse_code="BH-PC")

    def setUp(self):
        series = mock.patch('warehouse.services.transfer_request_service.HanaSeriesReader')
        series.start().return_value.resolve_transfer_request.return_value = 7
        self.addCleanup(series.stop)
        self.sap = _FakeSAP()
        self.request = self.service(self.sender).create_request({
            'from_warehouse': 'BH-LO',
            'to_warehouse': 'BH-PC',
            'remarks': 'For line 3',
            'lines': [
                {'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1500'},
                {'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '40'},
            ],
        })

    def service(self, user):
        svc = TransferRequestService("JIVO_OIL", user)
        svc._client = self.sap
        svc._branches = {"BH-LO": 1, "BH-PC": 1}
        return svc

    def edit(self, data, user=None):
        return self.service(user or self.sender).update_request(self.request.id, data)

    def lines(self, request=None):
        request = request or WarehouseTransferRequest.objects.get(pk=self.request.pk)
        return [
            (line.line_num, line.item_code, line.requested_qty)
            for line in request.lines.order_by('line_num')
        ]


class EditTransferRequestTests(_TransferEditCase):
    def test_changing_a_quantity_replaces_sap_request(self):
        edited = self.edit({'lines': [
            {'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1200'},
            {'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '40'},
        ]})
        self.assertEqual(self.lines(edited), [
            (0, 'RM0000002', Decimal('1200')), (1, 'PM0000019', Decimal('40')),
        ])
        self.assertEqual(edited.sap_request_doc_entry, 902)
        self.assertEqual(edited.sap_request_doc_num, '5002')
        self.assertEqual(self.sap.closed, [901])
        self.assertEqual(
            [line['Quantity'] for line in self.sap.raised[-1]['StockTransferLines']],
            [Decimal('1200'), Decimal('40')],
        )

    def test_removing_and_adding_lines_numbers_them_from_zero(self):
        # SAP's new request numbers its lines 0, 1, … and posting draws each
        # transfer line down against the request line of the same number.
        edited = self.edit({'lines': [
            {'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '40'},
            {'item_code': 'PM0000020', 'uom': 'PCS', 'quantity': '12'},
        ]})
        self.assertEqual(self.lines(edited), [
            (0, 'PM0000019', Decimal('40')), (1, 'PM0000020', Decimal('12')),
        ])
        self.assertEqual(
            [line['ItemCode'] for line in self.sap.raised[-1]['StockTransferLines']],
            ['PM0000019', 'PM0000020'],
        )

    def test_remarks_alone_never_reach_sap(self):
        edited = self.edit({'remarks': 'For line 4 instead'})
        self.assertEqual(edited.remarks, 'For line 4 instead')
        self.assertEqual(len(self.sap.raised), 1)
        self.assertEqual(self.sap.closed, [])
        self.assertEqual(edited.sap_request_doc_entry, 901)

    def test_the_same_lines_sent_back_leave_sap_alone(self):
        self.edit({'remarks': 'Same items', 'lines': [
            {'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1500.000'},
            {'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': 40},
        ]})
        self.assertEqual(len(self.sap.raised), 1)
        self.assertEqual(self.sap.closed, [])

    def test_only_the_requester_may_edit(self):
        with self.assertRaises(PermissionDenied):
            self.edit({'remarks': 'Mine now'}, user=self.receiver)
        self.assertEqual(
            WarehouseTransferRequest.objects.get(pk=self.request.pk).remarks, 'For line 3'
        )

    def test_a_decided_request_is_no_longer_editable(self):
        self.service(self.receiver).approve(self.request.id, {})
        with self.assertRaises(TransferRequestError) as ctx:
            self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '1'}]})
        self.assertIn('approved', str(ctx.exception))
        self.assertEqual(len(self.sap.raised), 1)

    def test_an_edit_needs_at_least_one_line(self):
        with self.assertRaises(TransferRequestError):
            self.edit({'lines': []})

    def test_a_fraction_of_a_piece_is_refused_and_nothing_changes(self):
        with self.assertRaises(TransferGuardError):
            self.edit({'lines': [{'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '0.5'}]})
        self.assertEqual(len(self.lines()), 2)
        self.assertEqual(len(self.sap.raised), 1)

    def test_sap_refusing_the_new_request_changes_nothing(self):
        self.sap.refuse_raise = SAPValidationError("Series closed")
        with self.assertRaises(SAPValidationError):
            self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '900'}]})
        request = WarehouseTransferRequest.objects.get(pk=self.request.pk)
        self.assertEqual(request.sap_request_doc_entry, 901)
        self.assertEqual(len(self.lines(request)), 2)
        self.assertEqual(self.sap.closed, [])

    def test_old_request_sap_will_not_close_retires_the_new_one(self):
        # Both would otherwise reserve the same stock. Rolled back, the app
        # points at the old request, which is still the one holding it.
        self.sap.refuse_close.add(901)
        with self.assertRaises(TransferRequestError) as ctx:
            self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '900'}]})
        self.assertIn('5001', str(ctx.exception))
        self.assertEqual(self.sap.closed, [902])
        request = WarehouseTransferRequest.objects.get(pk=self.request.pk)
        self.assertEqual(request.sap_request_doc_entry, 901)
        self.assertIsNone(request.sap_request_closed_at)
        self.assertEqual(len(self.lines(request)), 2)

    def test_old_request_already_closed_in_sap_does_not_block_the_edit(self):
        # Closed by hand in SAP, so a second close is refused — but there is
        # nothing left to release, and the edit is how the stock gets reserved.
        self.sap.refuse_close.add(901)
        self.sap.open_in_sap.discard(901)
        edited = self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '900'}]})
        self.assertEqual(edited.sap_request_doc_entry, 902)
        self.assertEqual(self.sap.closed, [])

    def test_old_request_counts_as_open_when_sap_cannot_say(self):
        self.sap.refuse_close.add(901)
        with mock.patch.object(
            self.sap, 'summarise_transfer_requests', side_effect=SAPConnectionError("down")
        ):
            with self.assertRaises(TransferRequestError):
                self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '9'}]})
        self.assertEqual(self.sap.closed, [902])

    def test_request_the_sweep_already_closed_gets_a_new_reservation(self):
        WarehouseTransferRequest.objects.filter(pk=self.request.pk).update(
            sap_request_closed_at='2026-09-01T00:00:00Z'
        )
        edited = self.edit({'lines': [{'item_code': 'RM0000002', 'uom': 'LTR', 'quantity': '9'}]})
        self.assertEqual(edited.sap_request_doc_entry, 902)
        self.assertIsNone(edited.sap_request_closed_at)
        self.assertEqual(self.sap.closed, [])


class ApprovalAfterEditTests(_TransferEditCase):
    def test_approving_an_older_copy_is_refused(self):
        seen = WarehouseTransferRequest.objects.get(pk=self.request.pk).updated_at
        self.edit({'lines': [{'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '40'}]})
        with self.assertRaises(TransferRequestError) as ctx:
            self.service(self.receiver).approve(self.request.id, {
                'updated_at': seen,
                'lines': [{'line_num': 0, 'approved_qty': '1000'}],
            })
        self.assertIn('changed after you opened it', str(ctx.exception))
        request = WarehouseTransferRequest.objects.get(pk=self.request.pk)
        self.assertEqual(request.status, TransferRequestStatus.PENDING)

    def test_approving_the_current_copy_goes_through(self):
        edited = self.edit({'lines': [{'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '40'}]})
        approved = self.service(self.receiver).approve(self.request.id, {
            'updated_at': edited.updated_at,
            'lines': [{'line_num': 0, 'approved_qty': '30'}],
        })
        self.assertEqual(approved.status, TransferRequestStatus.PARTIALLY_APPROVED)


class EditTransferRequestApiTests(_TransferEditCase):
    def setUp(self):
        super().setUp()
        role = UserRole.objects.create(name="Stores")
        for user in (self.sender, self.receiver):
            UserCompany.objects.create(user=user, company=self.oil, role=role)
            for codename in ("can_view_transfer_request", "can_create_transfer_request",
                             "can_approve_transfer_request"):
                user.user_permissions.add(Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename,
                ))
        client_patch = mock.patch(
            'warehouse.services.transfer_request_service.SAPClient', return_value=self.sap
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        branches = mock.patch.object(
            TransferRequestService, 'branch_map', {"BH-LO": 1, "BH-PC": 1}
        )
        branches.start()
        self.addCleanup(branches.stop)

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user=User.objects.get(pk=user.pk))
        client.credentials(HTTP_COMPANY_CODE=self.oil.code)
        return client

    def url(self, suffix=''):
        return f"/api/v1/warehouse/transfer-requests/{self.request.id}/{suffix}"

    def test_the_requester_patches_lines_and_remarks(self):
        response = self.api(self.sender).patch(self.url(), {
            'remarks': 'Line 4',
            'lines': [{'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '25'}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['remarks'], 'Line 4')
        self.assertEqual(response.data['sap_request_doc_num'], '5002')
        self.assertEqual(
            [(line['item_code'], line['requested_qty']) for line in response.data['lines']],
            [('PM0000019', '25.000')],
        )

    def test_the_same_item_twice_is_refused(self):
        line = {'item_code': 'PM0000019', 'uom': 'PCS', 'quantity': '25'}
        response = self.api(self.sender).patch(
            self.url(), {'lines': [line, line]}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_someone_else_is_forbidden(self):
        response = self.api(self.receiver).patch(
            self.url(), {'remarks': 'Mine'}, format='json'
        )
        self.assertEqual(response.status_code, 403)

    def test_a_stale_approval_is_a_bad_request(self):
        stale = self.api(self.receiver).get(self.url()).data['updated_at']
        self.api(self.sender).patch(self.url(), {'remarks': 'Line 4'}, format='json')
        response = self.api(self.receiver).post(
            self.url('approve/'), {'updated_at': stale}, format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('changed after you opened it', response.data['error'])
        current = self.api(self.receiver).get(self.url()).data['updated_at']
        response = self.api(self.receiver).post(
            self.url('approve/'), {'updated_at': current}, format='json'
        )
        self.assertEqual(response.status_code, 200, response.data)
