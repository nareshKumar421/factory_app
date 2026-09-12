"""Activation: printed labels are not stock until someone proves they exist.

The behaviour under test is a refusal as much as an action, so these cover both
halves: what activation lets through, and what every other flow now refuses.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse

from .models import (
    ActivationSource,
    BarcodeActivationRequest,
    BarcodeActivationRequestStatus,
    BarcodeActivationSettings,
    BarcodeAuditLog,
    BarcodeAuditTransactionType,
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    PalletStatus,
    PalletVerifyRequest,
    PalletVerifyRequestSource,
    ScanLog,
    ScanResult,
    ScanType,
)
from .services.activation_request_service import BarcodeActivationRequestService
from .services.activation_service import (
    ACCEPTED,
    NEEDS_BOX_SCAN,
    NEEDS_COUNT,
    REJECTED,
    REJECT_COUNT_MISMATCH,
    REJECT_NOT_PENDING,
    REJECT_WAREHOUSE_MISMATCH,
    ActivationService,
)
from .services.barcode_service import BarcodeService

RECEIVING_WAREHOUSE = 'BH-PF'
OTHER_WAREHOUSE = 'BH-FG'


class ActivationTestBase(TestCase):
    """One company, activation enforced on BH-PF only."""

    def setUp(self):
        self.company = Company.objects.create(name='Activation Co', code='ACTCO')
        self.user = User.objects.create_user(
            email='receiver@example.com',
            password='test-pass',
            full_name='Godown Receiver',
            employee_code='EMP-ACT-001',
        )
        self.user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=('barcode', 'warehouse')
            )
        )
        role, _ = UserRole.objects.get_or_create(name='Tester')
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        UserWarehouse.objects.create(
            user=self.user, company=self.company, warehouse_code=RECEIVING_WAREHOUSE
        )
        BarcodeActivationSettings.objects.create(
            company=self.company,
            is_enabled=True,
            enforced_warehouses=[RECEIVING_WAREHOUSE],
        )
        self.service = BarcodeService(company_code=self.company.code)
        self.activation = ActivationService(company_code=self.company.code)

    def generate_boxes(self, count=3, warehouse=RECEIVING_WAREHOUSE, batch='BATCH-1'):
        return self.service.generate_boxes(
            {
                'item_code': 'FG001',
                'item_name': 'Test Finished Good',
                'batch_number': batch,
                'qty': Decimal('12.50'),
                'box_count': count,
                'uom': 'PCS',
                'mfg_date': date(2026, 9, 1),
                'exp_date': date(2027, 9, 1),
                'warehouse': warehouse,
                'production_line': 'Line 1',
            },
            user=self.user,
        )

    def make_pallet(self, box_count=4, warehouse=RECEIVING_WAREHOUSE):
        """A pallet the way the print workflow builds one: empty, then labels."""
        pallet = self.service.create_pallet(
            {
                'warehouse': warehouse,
                'production_line': 'Line 1',
                'mfg_date': date(2026, 9, 1),
                'exp_date': date(2027, 9, 1),
                'max_box_count': box_count,
            },
            user=self.user,
        )
        boxes = self.generate_boxes(count=box_count, warehouse=warehouse)
        self.service.add_boxes_to_pallet(
            pallet.id, [box.id for box in boxes], user=self.user
        )
        pallet.refresh_from_db()
        return pallet


class LabelGenerationTests(ActivationTestBase):

    def test_labels_for_an_enforced_warehouse_are_born_pending(self):
        boxes = self.generate_boxes(count=2)
        self.assertEqual([box.status for box in boxes], [BoxStatus.PENDING] * 2)
        self.assertTrue(all(box.activated_at is None for box in boxes))

    def test_labels_for_a_warehouse_left_off_the_list_stay_active(self):
        boxes = self.generate_boxes(count=2, warehouse=OTHER_WAREHOUSE)
        self.assertEqual([box.status for box in boxes], [BoxStatus.ACTIVE] * 2)
        self.assertEqual(
            [box.activation_source for box in boxes],
            [ActivationSource.NOT_REQUIRED] * 2,
        )

    def test_settings_off_means_nothing_changes(self):
        settings_row = BarcodeActivationSettings.objects.get(company=self.company)
        settings_row.is_enabled = False
        settings_row.save()
        boxes = self.generate_boxes(count=2)
        self.assertEqual([box.status for box in boxes], [BoxStatus.ACTIVE] * 2)

    def test_a_company_with_no_settings_row_still_gets_the_rule(self):
        """On by default: activation is the rule, not an opt-in per company.

        The settings row is created on demand, so an unconfigured company must
        come out enforcing -- otherwise every company nobody has configured yet
        is silently exempt, which is the whole hole this closes.
        """
        BarcodeActivationSettings.objects.filter(company=self.company).delete()
        boxes = self.generate_boxes(count=2, warehouse='ANY-WH')
        self.assertEqual([box.status for box in boxes], [BoxStatus.PENDING] * 2)
        row = BarcodeActivationSettings.objects.get(company=self.company)
        self.assertTrue(row.is_enabled)
        self.assertEqual(row.enforced_warehouses, [])

    def test_empty_warehouse_list_covers_every_warehouse(self):
        settings_row = BarcodeActivationSettings.objects.get(company=self.company)
        settings_row.enforced_warehouses = []
        settings_row.save()
        boxes = self.generate_boxes(count=1, warehouse=OTHER_WAREHOUSE)
        self.assertEqual(boxes[0].status, BoxStatus.PENDING)

    def test_listing_warehouses_narrows_the_rule(self):
        """The list is a narrowing tool, so a warehouse left off it opts out."""
        settings_row = BarcodeActivationSettings.objects.get(company=self.company)
        settings_row.enforced_warehouses = [RECEIVING_WAREHOUSE]
        settings_row.save()
        self.assertEqual(
            self.generate_boxes(count=1, warehouse=OTHER_WAREHOUSE)[0].status,
            BoxStatus.ACTIVE,
        )
        self.assertEqual(self.generate_boxes(count=1)[0].status, BoxStatus.PENDING)

    def test_print_workflow_leaves_the_pallet_pending_not_empty(self):
        """The regression this feature could most easily have caused.

        `_recalculate_pallet` calls a pallet with no *active* boxes EMPTY, which
        would fire the moment the print workflow attaches its pending labels and
        leave the pallet unusable for printing.
        """
        pallet = self.make_pallet(box_count=4)
        self.assertEqual(pallet.status, PalletStatus.PENDING)
        self.assertEqual(pallet.boxes.count(), 4)
        self.assertEqual(pallet.box_count, 0)  # no stock on it yet
        self.assertEqual(pallet.total_boxes, 4)


class ReceiveScanTests(ActivationTestBase):

    def test_box_scan_into_matching_warehouse_activates_it(self):
        box = self.generate_boxes(count=1)[0]
        outcome = self.activation.scan_for_activation(
            box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        box.refresh_from_db()
        self.assertEqual(outcome.status, ACCEPTED)
        self.assertEqual(box.status, BoxStatus.ACTIVE)
        self.assertEqual(box.activation_source, ActivationSource.GATE_SCAN)
        self.assertEqual(box.activation_warehouse, RECEIVING_WAREHOUSE)
        self.assertEqual(box.activated_by, self.user)
        self.assertTrue(
            BoxMovement.objects.filter(
                box=box, movement_type=BoxMovementType.ACTIVATE
            ).exists()
        )
        self.assertTrue(
            BarcodeAuditLog.objects.filter(
                box=box, transaction_type=BarcodeAuditTransactionType.ACTIVATED
            ).exists()
        )

    def test_box_printed_for_another_warehouse_is_refused(self):
        box = self.generate_boxes(count=1)[0]
        outcome = self.activation.scan_for_activation(
            box.box_barcode, warehouse=OTHER_WAREHOUSE, user=self.user
        )
        box.refresh_from_db()
        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_WAREHOUSE_MISMATCH)
        self.assertIn(RECEIVING_WAREHOUSE, outcome.detail)
        self.assertIn(OTHER_WAREHOUSE, outcome.detail)
        self.assertEqual(box.status, BoxStatus.PENDING)

    def test_rescanning_an_active_box_is_refused_and_says_when(self):
        box = self.generate_boxes(count=1)[0]
        self.activation.scan_for_activation(
            box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        outcome = self.activation.scan_for_activation(
            box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_NOT_PENDING)
        self.assertIn('already activated', outcome.detail)

    def test_every_scan_is_audited_accepted_or_not(self):
        box = self.generate_boxes(count=1)[0]
        self.activation.scan_for_activation(
            box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        self.activation.scan_for_activation(
            'BOX-NONSENSE-0001', warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        logs = ScanLog.objects.filter(
            company=self.company, scan_type=ScanType.ACTIVATE
        )
        self.assertEqual(logs.filter(scan_result=ScanResult.SUCCESS).count(), 1)
        self.assertEqual(logs.filter(scan_result=ScanResult.REJECTED).count(), 1)

    def test_pallet_scan_asks_for_a_count_before_activating_anything(self):
        pallet = self.make_pallet(box_count=4)
        outcome = self.activation.scan_for_activation(
            pallet.pallet_id, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        self.assertEqual(outcome.status, NEEDS_COUNT)
        self.assertEqual(outcome.pending_box_count, 4)
        self.assertEqual(
            pallet.boxes.filter(status=BoxStatus.PENDING).count(), 4
        )

    def test_pallet_scan_with_matching_count_activates_the_whole_pallet(self):
        pallet = self.make_pallet(box_count=4)
        outcome = self.activation.scan_for_activation(
            pallet.pallet_id,
            warehouse=RECEIVING_WAREHOUSE,
            user=self.user,
            confirmed_box_count=4,
        )
        pallet.refresh_from_db()
        self.assertEqual(outcome.status, ACCEPTED)
        self.assertEqual(outcome.activated_count, 4)
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.ACTIVE).count(), 4)
        self.assertEqual(pallet.status, PalletStatus.ACTIVE)
        self.assertEqual(pallet.box_count, 4)
        self.assertEqual(pallet.activation_source, ActivationSource.GATE_SCAN)

    def test_short_count_activates_nothing_and_opens_a_ticket(self):
        """The whole point of the count confirm.

        A pallet's labels were created at print time, phantom ones included, so a
        short count means some label is not on a real box — and nothing here can
        say which. Activating the pallet anyway would let the phantom through.
        """
        pallet = self.make_pallet(box_count=4)
        outcome = self.activation.scan_for_activation(
            pallet.pallet_id,
            warehouse=RECEIVING_WAREHOUSE,
            user=self.user,
            confirmed_box_count=3,
        )
        pallet.refresh_from_db()
        self.assertEqual(outcome.status, NEEDS_BOX_SCAN)
        self.assertEqual(outcome.code, REJECT_COUNT_MISMATCH)
        self.assertEqual(outcome.activated_count, 0)
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.PENDING).count(), 4)
        self.assertEqual(pallet.status, PalletStatus.PENDING)
        ticket = PalletVerifyRequest.objects.get(pallet=pallet)
        self.assertEqual(ticket.source, PalletVerifyRequestSource.GATE)

    def test_box_by_box_after_a_short_count_leaves_the_phantom_pending(self):
        pallet = self.make_pallet(box_count=4)
        real_boxes = list(pallet.boxes.order_by('box_barcode'))[:3]
        for box in real_boxes:
            self.activation.scan_for_activation(
                box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
            )
        pallet.refresh_from_db()
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.ACTIVE).count(), 3)
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.PENDING).count(), 1)
        # Holds real stock now, so it is a live pallet -- counting only what was
        # actually received.
        self.assertEqual(pallet.status, PalletStatus.ACTIVE)
        self.assertEqual(pallet.box_count, 3)

    def test_more_boxes_than_labels_is_refused(self):
        pallet = self.make_pallet(box_count=2)
        outcome = self.activation.scan_for_activation(
            pallet.pallet_id,
            warehouse=RECEIVING_WAREHOUSE,
            user=self.user,
            confirmed_box_count=5,
        )
        self.assertEqual(outcome.status, REJECTED)
        self.assertEqual(outcome.code, REJECT_COUNT_MISMATCH)


class ReceiveEndpointTests(ActivationTestBase):
    """The warehouse-side API, including the manager-assignment gate."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.defaults['HTTP_COMPANY_CODE'] = self.company.code

    def _scan(self, barcode, warehouse=RECEIVING_WAREHOUSE, **extra):
        return self.client.post(
            '/api/v1/warehouse/receive/scan/',
            {'barcode': barcode, 'warehouse': warehouse, **extra},
            format='json',
        )

    def test_scan_endpoint_activates_a_box(self):
        box = self.generate_boxes(count=1)[0]
        response = self._scan(box.box_barcode)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], ACCEPTED)
        self.assertEqual(response.data['activated_count'], 1)
        box.refresh_from_db()
        self.assertEqual(box.status, BoxStatus.ACTIVE)

    def test_receiving_into_an_unmanaged_warehouse_is_forbidden(self):
        box = self.generate_boxes(count=1, warehouse=OTHER_WAREHOUSE)[0]
        response = self._scan(box.box_barcode, warehouse=OTHER_WAREHOUSE)
        self.assertEqual(response.status_code, 403)
        box.refresh_from_db()
        self.assertEqual(box.status, BoxStatus.ACTIVE)  # unenforced warehouse

    def test_unassigned_user_cannot_receive_at_all(self):
        """No assignment means no access -- the stricter reading, on purpose."""
        UserWarehouse.objects.filter(user=self.user).delete()
        box = self.generate_boxes(count=1)[0]
        response = self._scan(box.box_barcode)
        self.assertEqual(response.status_code, 403)
        box.refresh_from_db()
        self.assertEqual(box.status, BoxStatus.PENDING)

    def test_business_refusals_are_200_so_the_next_scan_still_works(self):
        box = self.generate_boxes(count=1)[0]
        self.activation.scan_for_activation(
            box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        response = self._scan(box.box_barcode)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], REJECTED)
        self.assertEqual(response.data['code'], REJECT_NOT_PENDING)

    def test_session_tally_counts_todays_receiving(self):
        boxes = self.generate_boxes(count=2)
        for box in boxes:
            self._scan(box.box_barcode)
        response = self.client.get(
            '/api/v1/warehouse/receive/session/', {'warehouse': RECEIVING_WAREHOUSE}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['boxes_activated'], 2)
        self.assertEqual(response.data['scans_accepted'], 2)


class ApprovalRouteTests(ActivationTestBase):

    def setUp(self):
        super().setUp()
        self.requester = User.objects.create_user(
            email='printer@example.com',
            password='test-pass',
            full_name='Label Printer',
            employee_code='EMP-ACT-002',
        )
        self.requester.user_permissions.add(
            Permission.objects.get(codename='can_request_barcode_activation')
        )
        role, _ = UserRole.objects.get_or_create(name='Tester')
        UserCompany.objects.create(
            user=self.requester, company=self.company, role=role, is_active=True
        )
        self.requests = BarcodeActivationRequestService(company_code=self.company.code)

    def test_approval_activates_without_any_scan(self):
        pallet = self.make_pallet(box_count=3)
        request = self.requests.create_request(
            reason='Gate scanner is down', pallet_id=pallet.id, user=self.requester
        )
        self.assertEqual(request.status, BarcodeActivationRequestStatus.OPEN)

        request, activated = self.requests.approve(
            request.id, note='Verified with the floor supervisor', user=self.user
        )
        pallet.refresh_from_db()
        self.assertEqual(request.status, BarcodeActivationRequestStatus.APPROVED)
        self.assertEqual(len(activated), 3)
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.ACTIVE).count(), 3)
        self.assertEqual(
            {box.activation_source for box in activated},
            {ActivationSource.APPROVAL},
        )
        # The audit trail is the mitigation for a route with nothing physical
        # behind it: "what was activated without a scan?" must be one filter.
        self.assertEqual(
            Box.objects.filter(
                company=self.company, activation_source=ActivationSource.APPROVAL
            ).count(),
            3,
        )

    def test_rejection_leaves_the_labels_pending(self):
        pallet = self.make_pallet(box_count=2)
        request = self.requests.create_request(
            reason='Please activate', pallet_id=pallet.id, user=self.requester
        )
        request = self.requests.reject(request.id, note='Go and scan them', user=self.user)
        pallet.refresh_from_db()
        self.assertEqual(request.status, BarcodeActivationRequestStatus.REJECTED)
        self.assertEqual(pallet.boxes.filter(status=BoxStatus.PENDING).count(), 2)

    def test_reason_is_required(self):
        pallet = self.make_pallet(box_count=1)
        with self.assertRaises(ValueError):
            self.requests.create_request(reason='   ', pallet_id=pallet.id, user=self.requester)

    def test_a_second_open_request_for_the_same_labels_is_refused(self):
        pallet = self.make_pallet(box_count=2)
        self.requests.create_request(
            reason='First', pallet_id=pallet.id, user=self.requester
        )
        with self.assertRaises(ValueError):
            self.requests.create_request(
                reason='Second', pallet_id=pallet.id, user=self.requester
            )

    def test_approving_boxes_the_gate_already_took_is_not_double_counted(self):
        pallet = self.make_pallet(box_count=2)
        request = self.requests.create_request(
            reason='Scanner down', pallet_id=pallet.id, user=self.requester
        )
        gate_box = pallet.boxes.order_by('box_barcode').first()
        self.activation.scan_for_activation(
            gate_box.box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )

        request, activated = self.requests.approve(request.id, user=self.user)
        gate_box.refresh_from_db()
        self.assertEqual(len(activated), 1)
        self.assertEqual(gate_box.activation_source, ActivationSource.GATE_SCAN)
        self.assertEqual(BarcodeActivationRequest.objects.count(), 1)


class PendingReportTests(ActivationTestBase):

    def test_report_groups_by_print_run_and_totals_the_labels(self):
        self.generate_boxes(count=3, batch='BATCH-A')
        self.generate_boxes(count=2, batch='BATCH-B')
        groups = self.activation.pending_groups()
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(group['box_count'] for group in groups), 5)

    def test_activated_labels_leave_the_report(self):
        boxes = self.generate_boxes(count=2)
        self.activation.scan_for_activation(
            boxes[0].box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        groups = self.activation.pending_groups()
        self.assertEqual(sum(group['box_count'] for group in groups), 1)

    def test_void_only_touches_pending_labels(self):
        boxes = self.generate_boxes(count=3)
        self.activation.scan_for_activation(
            boxes[0].box_barcode, warehouse=RECEIVING_WAREHOUSE, user=self.user
        )
        voided = self.activation.void_pending(
            [box.id for box in boxes], reason='Never arrived at the godown',
            user=self.user,
        )
        self.assertEqual(len(voided), 2)
        boxes[0].refresh_from_db()
        self.assertEqual(boxes[0].status, BoxStatus.ACTIVE)
        self.assertEqual(
            Box.objects.filter(company=self.company, status=BoxStatus.VOID).count(), 2
        )

    def test_void_requires_a_reason(self):
        boxes = self.generate_boxes(count=1)
        with self.assertRaises(ValueError):
            self.activation.void_pending([boxes[0].id], reason='', user=self.user)


class PendingStockIsRefusedEverywhereTests(ActivationTestBase):
    """A pending label must not behave like stock in any flow."""

    def test_docking_scan_refuses_a_pending_box_with_a_useful_reason(self):
        from gate_core.services.sales_dispatch_loading import (
            REJECT_BOX_NOT_ACTIVATED,
            box_unavailable_detail,
        )

        box = self.generate_boxes(count=1)[0]
        detail = box_unavailable_detail(box)
        self.assertIn('never received', detail)
        self.assertIn(RECEIVING_WAREHOUSE, detail)
        self.assertEqual(REJECT_BOX_NOT_ACTIVATED, 'BOX_NOT_ACTIVATED')

    def test_bst_scan_refuses_a_pending_box(self):
        from warehouse.services.bst_service import BSTError

        box = self.generate_boxes(count=1)[0]
        from warehouse.services.bst_service import BSTService

        service = BSTService(company_code=self.company.code)
        with self.assertRaises(BSTError) as caught:
            service._validate_box(box, None, {box.item_code}, {RECEIVING_WAREHOUSE})
        self.assertEqual(caught.exception.code, 'BOX_NOT_ACTIVATED')

    def test_intercompany_transfer_refuses_a_pending_box(self):
        from .services.intercompany_transfer_service import (
            IntercompanyTransferError,
            IntercompanyTransferService,
        )

        box = self.generate_boxes(count=1)[0]
        service = IntercompanyTransferService(self.user)
        with self.assertRaises(IntercompanyTransferError) as caught:
            service._validate_box(box, self.company, box.box_barcode)
        self.assertIn('never received', str(caught.exception))

    def test_pending_pallet_is_not_mirrored_onto_the_warehouse_map(self):
        from wms.models import Inventory as WmsInventory

        pallet = self.make_pallet(box_count=3)
        self.assertEqual(
            WmsInventory.objects.filter(
                company=self.company, record_id=f"bc-pallet-{pallet.id}"
            ).count(),
            0,
        )
