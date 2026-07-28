import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth.models import Permission
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from accounts.models import User
from company.models import Company, UserCompany, UserRole

from .models import (
    BarcodeSequence,
    BarcodeAuditLog,
    BarcodeAuditTransactionType,
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    EntityType,
    LabelPrintLog,
    LooseStockStatus,
    PalletMovement,
    PalletStatus,
    PalletVerifyRequest,
    PalletVerifyRequestStatus,
    DispatchScanLog,
    DispatchScanResult,
    DispatchScannedUnit,
    DispatchSapSyncLog,
    DispatchSapUpdateStatus,
    DispatchSessionStatus,
    DispatchSettings,
    IntercompanyTransfer,
    IntercompanyTransferStatus,
    ScanLog,
    ScanResult,
)
from .serializers import (
    BoxGenerateSerializer,
    BoxListSerializer,
    MAX_BOX_LABELS_PER_REQUEST,
    PalletCreateSerializer,
)
from .services.barcode_service import BarcodeService
from .services.label_service import LabelService
from .services.oitm_item_service import OitmItemService
from .services.production_release_service import ProductionReleaseOilService
from .services.scan_service import ScanService
from .services.verify_request_service import PalletVerifyRequestService
from .services.dispatch_service import (
    BarcodeDispatchService,
    DispatchValidationError,
    SapDispatchAdapter,
    SapUpdateResult,
)
from .services.intercompany_transfer_service import (
    IntercompanyTransferError,
    IntercompanyTransferService,
)
from .views import _list_response


class BarcodeWorkflowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name='Barcode Test Company',
            code='TESTCO',
        )
        self.user = User.objects.create_user(
            email='barcode@example.com',
            password='test-pass',
            full_name='Barcode Tester',
            employee_code='EMP-BC-001',
        )
        # Barcode endpoints now require a barcode permission (see barcode/permissions.py).
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label='barcode')
        )
        self.service = BarcodeService(company_code=self.company.code)
        self.label_service = LabelService(company_code=self.company.code)
        self.scan_service = ScanService(company_code=self.company.code)

    def _generate_boxes(self, count=3, qty='12.50', line='Line 1', batch='BATCH-001'):
        return self.service.generate_boxes(
            {
                'item_code': 'FG001',
                'item_name': 'Test Finished Good',
                'batch_number': batch,
                'qty': Decimal(qty),
                'box_count': count,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': line,
            },
            user=self.user,
        )

    def _generate_boxes_for_company(
        self,
        company_code,
        *,
        count=1,
        item_code='FG001',
        qty='12.50',
        line='Line 1',
        batch='BATCH-001',
    ):
        return BarcodeService(company_code=company_code).generate_boxes(
            {
                'item_code': item_code,
                'item_name': 'Test Finished Good',
                'batch_number': batch,
                'qty': Decimal(qty),
                'box_count': count,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': line,
            },
            user=self.user,
        )

    def test_generate_boxes_reserves_unique_sequence_and_logs_movements(self):
        boxes = self._generate_boxes(count=3)

        self.assertEqual(
            [box.box_barcode for box in boxes],
            [
                'BOX-20260507-Line_1-0001',
                'BOX-20260507-Line_1-0002',
                'BOX-20260507-Line_1-0003',
            ],
        )
        self.assertEqual(Box.objects.count(), 3)
        self.assertEqual(
            BoxMovement.objects.filter(movement_type=BoxMovementType.CREATE).count(),
            3,
        )
        self.assertEqual(
            [box.barcode_data for box in boxes],
            [{'barcode': box.box_barcode} for box in boxes],
        )

        sequence = BarcodeSequence.objects.get(
            company=self.company,
            sequence_type='BOX',
            date_str='20260507',
            line_key='Line_1',
        )
        self.assertEqual(sequence.next_value, 4)

        more_boxes = self._generate_boxes(count=2)
        self.assertEqual(
            [box.box_barcode for box in more_boxes],
            [
                'BOX-20260507-Line_1-0004',
                'BOX-20260507-Line_1-0005',
            ],
        )

    def test_list_response_returns_page_metadata_when_requested(self):
        self._generate_boxes(count=3)
        request = Request(APIRequestFactory().get('/barcode/boxes/?page=1&page_size=2'))

        response = _list_response(request, self.service.list_boxes(), BoxListSerializer)

        self.assertEqual(response.data['count'], 3)
        self.assertEqual(response.data['page'], 1)
        self.assertEqual(response.data['page_size'], 2)
        self.assertEqual(response.data['total_pages'], 2)
        self.assertTrue(response.data['next'])
        self.assertFalse(response.data['previous'])
        self.assertEqual(len(response.data['results']), 2)

    def test_list_response_keeps_legacy_array_shape_without_page_params(self):
        self._generate_boxes(count=2)
        request = Request(APIRequestFactory().get('/barcode/boxes/'))

        response = _list_response(request, self.service.list_boxes(), BoxListSerializer)

        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 2)

    def test_create_pallet_creates_empty_pallet_then_adds_boxes(self):
        boxes = self._generate_boxes(count=3)

        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )

        self.assertEqual(pallet.pallet_id, 'PLT-20260507-Line_1-001')
        self.assertEqual(pallet.box_count, 0)
        self.assertEqual(pallet.total_qty, Decimal('0'))
        self.assertEqual(PalletMovement.objects.count(), 1)

        pallet = self.service.add_boxes_to_pallet(
            pallet.id,
            [box.id for box in boxes],
            user=self.user,
        )
        self.assertEqual(pallet.box_count, 3)
        self.assertEqual(pallet.total_qty, Decimal('37.50'))
        self.assertEqual(pallet.item_code, 'FG001')
        self.assertEqual(pallet.item_name, 'Test Finished Good')
        self.assertEqual(pallet.batch_number, 'BATCH-001')
        self.assertEqual(pallet.uom, 'PCS')
        self.assertEqual(pallet.mfg_date, date(2026, 5, 7))
        self.assertEqual(pallet.exp_date, date(2027, 5, 7))
        self.assertEqual(
            Box.objects.filter(pallet=pallet, current_warehouse='FG01').count(),
            3,
        )
        self.assertEqual(pallet.barcode_data, {'barcode': pallet.pallet_id})

    def test_reconcile_pallet_matches_missing_foreign_and_recovers_labels(self):
        boxes = self._generate_boxes(count=3)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )

        # A separate unpalletized box (different batch) — a "foreign" scan.
        other = self._generate_boxes(count=1, batch='BATCH-002', line='Line 2')[0]

        # Scan two of three real boxes + the foreign box; report one unlabeled box.
        result = self.service.reconcile_pallet(
            pallet.id,
            scanned_barcodes=[boxes[0].box_barcode, boxes[1].box_barcode, other.box_barcode],
            unlabeled_count=1,
        )

        self.assertEqual(result['expected_count'], 3)
        self.assertEqual(
            {b['box_barcode'] for b in result['matched']},
            {boxes[0].box_barcode, boxes[1].box_barcode},
        )
        self.assertEqual(
            [b['box_barcode'] for b in result['missing']],
            [boxes[2].box_barcode],
        )
        self.assertEqual(len(result['foreign']), 1)
        self.assertEqual(result['foreign'][0]['barcode'], other.box_barcode)
        self.assertEqual(result['foreign'][0]['reason'], 'UNPALLETIZED')
        self.assertFalse(result['foreign'][0]['item_matches_pallet'])
        self.assertFalse(result['is_fully_reconciled'])

        # Deterministic recovery: 1 unlabeled == 1 missing (same item/batch) → eligible.
        self.assertTrue(result['recover']['eligible'])
        self.assertEqual(
            [b['box_barcode'] for b in result['recover']['boxes']],
            [boxes[2].box_barcode],
        )

    def test_reconcile_pallet_fully_matched_is_reconciled_and_not_recoverable(self):
        boxes = self._generate_boxes(count=2)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )

        result = self.service.reconcile_pallet(
            pallet.id,
            scanned_barcodes=[boxes[0].box_barcode, boxes[1].box_barcode],
            unlabeled_count=0,
        )

        self.assertTrue(result['is_fully_reconciled'])
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['foreign'], [])
        self.assertFalse(result['recover']['eligible'])

    def test_reconcile_pallet_unlabeled_mismatch_locks_recovery(self):
        boxes = self._generate_boxes(count=3)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )

        # Two boxes missing but operator reports only one unlabeled → not safe to reprint.
        result = self.service.reconcile_pallet(
            pallet.id,
            scanned_barcodes=[boxes[0].box_barcode],
            unlabeled_count=1,
        )

        self.assertEqual(len(result['missing']), 2)
        self.assertFalse(result['recover']['eligible'])
        self.assertEqual(result['recover']['boxes'], [])

    def test_reconcile_pallet_apply_pulls_foreign_and_drops_missing(self):
        boxes = self._generate_boxes(count=3)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )
        # A loose box of the same item/batch — safe to pull onto the pallet.
        extra = self._generate_boxes(count=1)[0]

        result = self.service.reconcile_pallet(
            pallet.id,
            scanned_barcodes=[boxes[0].box_barcode, boxes[1].box_barcode, extra.box_barcode],
            apply=True,
            pull_foreign=True,
            drop_missing=True,
            reason='cycle count',
            user=self.user,
        )

        self.assertIn(extra.box_barcode, result['applied']['pulled'])
        self.assertIn(boxes[2].box_barcode, result['applied']['dropped'])
        self.assertEqual(Box.objects.get(id=extra.id).pallet_id, pallet.id)
        self.assertIsNone(Box.objects.get(id=boxes[2].id).pallet_id)
        # Post-apply the pallet holds exactly the three scanned boxes.
        self.assertTrue(result['is_fully_reconciled'])

    def test_reconcile_pallet_apply_skips_unpullable_foreign(self):
        boxes = self._generate_boxes(count=2)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )
        # Different batch → not a member of this pallet's item context.
        other = self._generate_boxes(count=1, batch='BATCH-XYZ', line='Line 9')[0]

        result = self.service.reconcile_pallet(
            pallet.id,
            scanned_barcodes=[boxes[0].box_barcode, boxes[1].box_barcode, other.box_barcode],
            apply=True,
            pull_foreign=True,
            user=self.user,
        )

        self.assertEqual(result['applied']['pulled'], [])
        self.assertTrue(
            any(s['barcode'] == other.box_barcode for s in result['applied']['skipped'])
        )
        self.assertIsNone(Box.objects.get(id=other.id).pallet_id)

    def test_reconcile_pallet_apply_requires_an_action(self):
        boxes = self._generate_boxes(count=1)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )
        with self.assertRaises(ValueError):
            self.service.reconcile_pallet(
                pallet.id,
                scanned_barcodes=[boxes[0].box_barcode],
                apply=True,
                user=self.user,
            )

    def _make_pallet_with_boxes(self, count=3):
        boxes = self._generate_boxes(count=count)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )
        return pallet, boxes

    def test_create_verify_request_snapshots_findings_and_opens(self):
        pallet, boxes = self._make_pallet_with_boxes(count=3)
        svc = PalletVerifyRequestService(company_code=self.company.code)

        req = svc.create_request(
            pallet.id,
            reason='A label fell off',
            source='DISPATCH',
            source_reference='INV-42',
            scanned_barcodes=[boxes[0].box_barcode, boxes[1].box_barcode],
            unlabeled_count=1,
            user=self.user,
        )

        req.refresh_from_db()
        self.assertEqual(req.status, PalletVerifyRequestStatus.OPEN)
        self.assertEqual(req.requested_by, self.user)
        self.assertEqual(req.source, 'DISPATCH')
        self.assertEqual(req.findings['expected_count'], 3)
        self.assertEqual(len(req.findings['missing']), 1)
        self.assertEqual(req.findings['missing'][0]['box_barcode'], boxes[2].box_barcode)

    def test_resolve_verify_request_closes_and_records_resolver(self):
        pallet, boxes = self._make_pallet_with_boxes(count=2)
        svc = PalletVerifyRequestService(company_code=self.company.code)
        req = svc.create_request(pallet.id, user=self.user)

        resolved = svc.resolve_request(req.id, 'Relabelled the box', self.user)

        self.assertEqual(resolved.status, PalletVerifyRequestStatus.RESOLVED)
        self.assertEqual(resolved.resolved_by, self.user)
        self.assertIsNotNone(resolved.resolved_at)
        self.assertEqual(resolved.resolution_note, 'Relabelled the box')
        # A closed request cannot be resolved again.
        with self.assertRaises(ValueError):
            svc.resolve_request(req.id, 'again', self.user)

    def test_list_requests_scopes_to_owner_for_non_team(self):
        pallet, _ = self._make_pallet_with_boxes(count=1)
        svc = PalletVerifyRequestService(company_code=self.company.code)
        req = svc.create_request(pallet.id, user=self.user)

        other = User.objects.create_user(
            email='other@example.com', password='x', full_name='Other',
            employee_code='EMP-BC-OTHER',
        )

        owner_view = svc.list_requests(user=self.user, is_team=False)
        self.assertIn(req.id, [r.id for r in owner_view])

        other_view = svc.list_requests(user=other, is_team=False)
        self.assertEqual(list(other_view), [])

        team_view = svc.list_requests(user=other, is_team=True)
        self.assertIn(req.id, [r.id for r in team_view])

    def test_add_boxes_to_pallet_rejects_mismatched_item_context(self):
        boxes = self._generate_boxes(count=1)
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        self.service.add_boxes_to_pallet(pallet.id, [boxes[0].id], user=self.user)

        other_box = self.service.generate_boxes(
            {
                'item_code': 'FG002',
                'item_name': 'Other Finished Good',
                'batch_number': 'BATCH-002',
                'qty': Decimal('12.50'),
                'box_count': 1,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )[0]

        with self.assertRaisesMessage(
            ValueError,
            "Box item, batch, or UOM does not match the target pallet.",
        ):
            self.service.add_boxes_to_pallet(pallet.id, [other_box.id], user=self.user)

        other_box.refresh_from_db()
        pallet.refresh_from_db()
        self.assertIsNone(other_box.pallet)
        self.assertEqual(pallet.box_count, 1)

    def test_active_empty_pallet_with_stale_context_is_reused(self):
        boxes = self._generate_boxes(count=2, batch='BATCH-NEW')
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        pallet.item_code = 'OLD-FG'
        pallet.item_name = 'Old Finished Good'
        pallet.batch_number = 'OLD-BATCH'
        pallet.uom = 'PCS'
        pallet.save(update_fields=['item_code', 'item_name', 'batch_number', 'uom'])

        updated = self.service.add_boxes_to_pallet(
            pallet.id,
            [box.id for box in boxes],
            user=self.user,
        )

        self.assertEqual(updated.status, PalletStatus.ACTIVE)
        self.assertEqual(updated.box_count, 2)
        self.assertEqual(updated.total_qty, Decimal('25.00'))
        self.assertEqual(updated.item_code, 'FG001')
        self.assertEqual(updated.item_name, 'Test Finished Good')
        self.assertEqual(updated.batch_number, 'BATCH-NEW')

    def test_add_boxes_to_pallet_enforces_capacity(self):
        boxes = self._generate_boxes(count=2)
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
                'max_box_count': 1,
            },
            user=self.user,
        )

        with self.assertRaisesMessage(ValueError, "Pallet capacity exceeded."):
            self.service.add_boxes_to_pallet(
                pallet.id,
                [box.id for box in boxes],
                user=self.user,
            )

        self.assertEqual(Box.objects.filter(pallet=pallet).count(), 0)

    def test_clear_pallet_resets_context_and_allows_reuse(self):
        first_boxes = self._generate_boxes(count=2, qty='10.00', batch='BATCH-001')
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id,
            [box.id for box in first_boxes],
            user=self.user,
        )

        cleared = self.service.clear_pallet(
            pallet.id,
            notes='Ready for reuse',
            user=self.user,
        )

        self.assertEqual(cleared.status, PalletStatus.CLEARED)
        self.assertEqual(cleared.box_count, 0)
        self.assertEqual(cleared.total_qty, Decimal('0'))
        self.assertEqual(cleared.item_code, '')
        self.assertEqual(cleared.batch_number, '')
        self.assertEqual(cleared.uom, '')
        self.assertEqual(cleared.max_box_count, 0)
        self.assertEqual(Box.objects.filter(pallet=cleared).count(), 0)

        new_box = self.service.generate_boxes(
            {
                'item_code': 'FG002',
                'item_name': 'Reusable Pallet Product',
                'batch_number': 'BATCH-REUSE',
                'qty': Decimal('7.50'),
                'box_count': 1,
                'uom': 'PCS',
                'mfg_date': date(2026, 6, 1),
                'exp_date': date(2027, 6, 1),
                'warehouse': 'FG01',
                'production_line': 'Line 2',
            },
            user=self.user,
        )[0]

        reused = self.service.add_boxes_to_pallet(cleared.id, [new_box.id], user=self.user)

        self.assertEqual(reused.status, PalletStatus.ACTIVE)
        self.assertEqual(reused.pallet_id, pallet.pallet_id)
        self.assertEqual(reused.box_count, 1)
        self.assertEqual(reused.total_qty, Decimal('7.50'))
        self.assertEqual(reused.item_code, 'FG002')
        self.assertEqual(reused.item_name, 'Reusable Pallet Product')
        self.assertEqual(reused.batch_number, 'BATCH-REUSE')
        self.assertEqual(reused.mfg_date, date(2026, 6, 1))
        self.assertEqual(reused.exp_date, date(2027, 6, 1))

    def test_split_into_cleared_pallet_reactivates_and_sets_context(self):
        boxes = self._generate_boxes(count=3, qty='5.00', batch='BATCH-SPLIT')
        source = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        source = self.service.add_boxes_to_pallet(
            source.id,
            [box.id for box in boxes],
            user=self.user,
        )
        target = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG02',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        target.status = PalletStatus.CLEARED
        target.save(update_fields=['status'])

        updated_target = self.service.split_pallet(
            source.id,
            [boxes[0].id],
            target.id,
            user=self.user,
        )

        self.assertEqual(updated_target.status, PalletStatus.ACTIVE)
        self.assertEqual(updated_target.box_count, 1)
        self.assertEqual(updated_target.total_qty, Decimal('5.00'))
        self.assertEqual(updated_target.item_code, 'FG001')
        self.assertEqual(updated_target.batch_number, 'BATCH-SPLIT')
        self.assertEqual(updated_target.uom, 'PCS')
        updated_target.refresh_from_db()
        self.assertEqual(updated_target.boxes.first().current_warehouse, 'FG02')

    def test_full_pallet_dismantle_clears_context_for_reuse(self):
        boxes = self._generate_boxes(count=1, qty='10.00', batch='BATCH-DISMANTLE')
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id,
            [boxes[0].id],
            user=self.user,
        )

        dismantled = self.service.dismantle_pallet(
            pallet.id,
            box_ids=None,
            reason='REPACK',
            reason_notes='Production test',
            user=self.user,
        )

        self.assertEqual(dismantled.status, PalletStatus.CLEARED)
        self.assertEqual(dismantled.box_count, 0)
        self.assertEqual(dismantled.total_qty, Decimal('0'))
        self.assertEqual(dismantled.item_code, '')
        self.assertEqual(dismantled.batch_number, '')
        self.assertEqual(dismantled.uom, '')
        self.assertEqual(dismantled.max_box_count, 0)

    def test_box_transfer_to_pallet_rejects_mismatched_context(self):
        target_box = self._generate_boxes(count=1, batch='BATCH-TARGET')[0]
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(pallet.id, [target_box.id], user=self.user)
        other_box = self.service.generate_boxes(
            {
                'item_code': 'FG002',
                'item_name': 'Other Finished Good',
                'batch_number': 'BATCH-OTHER',
                'qty': Decimal('4.00'),
                'box_count': 1,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG02',
                'production_line': 'Line 1',
            },
            user=self.user,
        )[0]

        with self.assertRaisesMessage(
            ValueError,
            "Box item, batch, or UOM does not match the target pallet.",
        ):
            self.service.transfer_boxes(
                [other_box.id],
                to_warehouse='FG01',
                to_pallet_id=pallet.id,
                user=self.user,
            )

        other_box.refresh_from_db()
        self.assertIsNone(other_box.pallet)

    def test_box_transfer_between_pallets_may_exceed_target_capacity(self):
        source_boxes = self._generate_boxes(count=2, qty='5.00', batch='BATCH-MERGE')
        target_box = self._generate_boxes(count=1, qty='5.00', batch='BATCH-MERGE')[0]

        source = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        source = self.service.add_boxes_to_pallet(
            source.id,
            [box.id for box in source_boxes],
            user=self.user,
        )
        target = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG02',
                'production_line': 'Line 1',
                'mfg_date': date(2026, 5, 7),
                'max_box_count': 2,
            },
            user=self.user,
        )
        target = self.service.add_boxes_to_pallet(target.id, [target_box.id], user=self.user)

        transferred = self.service.transfer_boxes(
            [source_boxes[0].id],
            to_warehouse=target.current_warehouse,
            to_pallet_id=target.id,
            user=self.user,
        )

        self.assertEqual(transferred[0].pallet_id, target.id)
        self.assertEqual(transferred[0].current_warehouse, 'FG02')
        source.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(source.box_count, 1)
        self.assertEqual(target.box_count, 2)

        # Transfers may consolidate boxes onto a pallet past its nominal capacity —
        # the second box lands on the already-full (2/2) target, bringing it to 3.
        overfilled = self.service.transfer_boxes(
            [source_boxes[1].id],
            to_warehouse=target.current_warehouse,
            to_pallet_id=target.id,
            user=self.user,
        )

        self.assertEqual(overfilled[0].pallet_id, target.id)
        target.refresh_from_db()
        self.assertEqual(target.box_count, 3)

    def test_pallet_move_persists_destination_bin(self):
        """Moving a pallet into an own warehouse records the chosen location/bin."""
        boxes = self._generate_boxes(count=2)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )

        moved = self.service.move_pallet(
            pallet.id, to_warehouse='FG02', to_bin='A-01-1', notes='', user=self.user,
        )
        self.assertEqual(moved.current_warehouse, 'FG02')
        self.assertEqual(moved.current_bin, 'A-01-1')

        # Boxes on the pallet adopt the same warehouse + bin (re-read from the DB,
        # not the pallet's prefetched cache).
        for box in Box.objects.filter(pallet=moved):
            self.assertEqual(box.current_warehouse, 'FG02')
            self.assertEqual(box.current_bin, 'A-01-1')

        pm = PalletMovement.objects.filter(pallet=pallet).latest('id')
        self.assertEqual(pm.to_warehouse, 'FG02')
        self.assertEqual(pm.to_bin, 'A-01-1')
        bm = BoxMovement.objects.filter(box__in=boxes).latest('id')
        self.assertEqual(bm.to_bin, 'A-01-1')

    def test_pallet_move_to_sap_warehouse_clears_bin(self):
        """Moving to a SAP (external) warehouse keeps no internal bin."""
        boxes = self._generate_boxes(count=1)
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(pallet.id, [boxes[0].id], user=self.user)
        # First into an own-warehouse bin…
        self.service.move_pallet(pallet.id, to_warehouse='FG02', to_bin='A-01-1',
                                 notes='', user=self.user)
        # …then out to a SAP warehouse with no bin.
        moved = self.service.move_pallet(pallet.id, to_warehouse='SAPWH', to_bin='',
                                         notes='', user=self.user)
        self.assertEqual(moved.current_warehouse, 'SAPWH')
        self.assertEqual(moved.current_bin, '')

    def test_box_transfer_persists_destination_bin(self):
        """A box transfer to an own warehouse records the chosen location/bin."""
        boxes = self._generate_boxes(count=1)
        transferred = self.service.transfer_boxes(
            [boxes[0].id], to_warehouse='FG02', to_pallet_id=None,
            user=self.user, to_bin='B-02-1',
        )
        self.assertEqual(transferred[0].current_warehouse, 'FG02')
        self.assertEqual(transferred[0].current_bin, 'B-02-1')
        bm = BoxMovement.objects.filter(box=boxes[0]).latest('id')
        self.assertEqual(bm.to_warehouse, 'FG02')
        self.assertEqual(bm.to_bin, 'B-02-1')

    def test_pallet_move_into_own_wms_warehouse_mirrors_inventory(self):
        """Moving a pallet into an own Warehouse Ops bin shows it on the WMS map."""
        from wms.models import (
            Inventory as WmsInventory,
            Location as WmsLocation,
            Warehouse as WmsWarehouse,
        )

        # An own Warehouse Ops warehouse mapped to SAP code 'FG02' with bin 'A-01'.
        WmsWarehouse.objects.create(
            company=self.company, record_id='wh-own',
            data={'id': 'wh-own', 'type': 'OWN', 'sapWarehouseCode': 'FG02', 'name': 'Own WH'},
        )
        WmsLocation.objects.create(
            company=self.company, record_id='loc-a01',
            data={'id': 'loc-a01', 'warehouseId': 'wh-own', 'code': 'A-01', 'enabled': True},
        )

        boxes = self._generate_boxes(count=2, qty='5.00')
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1',
             'mfg_date': date(2026, 5, 7)},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [b.id for b in boxes], user=self.user,
        )

        self.service.move_pallet(pallet.id, to_warehouse='FG02', to_bin='A-01',
                                 notes='', user=self.user)

        mirror = WmsInventory.objects.filter(
            company=self.company, record_id=f'bc-pallet-{pallet.id}'
        ).first()
        self.assertIsNotNone(mirror)
        self.assertEqual(mirror.data['locationId'], 'loc-a01')
        self.assertEqual(mirror.data['boxCount'], 2)
        self.assertEqual(mirror.data['quantity'], 10.0)
        self.assertEqual(mirror.data['sourcePalletId'], pallet.pallet_id)

        # Moving out to a SAP warehouse (no bin) removes the WMS mirror.
        self.service.move_pallet(pallet.id, to_warehouse='SAPWH', to_bin='',
                                 notes='', user=self.user)
        self.assertFalse(
            WmsInventory.objects.filter(
                company=self.company, record_id=f'bc-pallet-{pallet.id}'
            ).exists()
        )

    def test_intercompany_transfer_changes_box_ownership_and_records_history(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        boxes = self._generate_boxes(count=2, qty='5.00', batch='BATCH-IC')

        service = IntercompanyTransferService(self.user)
        scan = service.scan_barcode(
            barcode=boxes[0].box_barcode,
            source_company_code=self.company.code,
            destination_company_code=destination.code,
            device_id='scanner-1',
        )
        self.assertEqual(scan['current_company'], self.company.code)

        transfer = service.create_transfer(
            source_company_code=self.company.code,
            destination_company_code=destination.code,
            barcodes=[box.box_barcode for box in boxes],
            notes='Move to trading company',
            device_id='scanner-1',
        )

        self.assertEqual(transfer.status, IntercompanyTransferStatus.COMPLETED)
        self.assertEqual(transfer.total_barcodes, 2)
        self.assertEqual(transfer.lines.count(), 2)
        for box in boxes:
            box.refresh_from_db()
            self.assertEqual(box.company_id, destination.id)

        history = BarcodeAuditLog.objects.filter(
            barcode=boxes[0].box_barcode,
            transaction_type=BarcodeAuditTransactionType.TRANSFER_COMPLETED,
        )
        self.assertEqual(history.count(), 1)

        reversed_transfer = service.reverse_transfer(transfer.id, reason='Wrong company')
        self.assertEqual(reversed_transfer.status, IntercompanyTransferStatus.REVERSED)
        boxes[0].refresh_from_db()
        self.assertEqual(boxes[0].company_id, self.company.id)

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_oil_to_mart_maps_destination_item_code(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='OIL-FG-001',
            batch='BATCH-MAP',
        )[0]
        mock_oitm_service.return_value.find_item_codes_by_oil_item_code.return_value = ['MART-FG-009']

        service = IntercompanyTransferService(self.user)
        transfer = service.create_transfer(
            source_company_code=source.code,
            destination_company_code=destination.code,
            barcodes=[box.box_barcode],
            notes='Move mapped item',
        )

        mock_oitm_service.assert_called_once_with(company_code=destination.code)
        mock_oitm_service.return_value.find_item_codes_by_oil_item_code.assert_called_once_with('OIL-FG-001')
        line = transfer.lines.get()
        self.assertEqual(line.item_code, 'OIL-FG-001')
        self.assertEqual(line.batch_number, 'BATCH-MAP')
        self.assertEqual(line.qty, Decimal('12.500'))
        self.assertEqual(line.uom, 'PCS')
        box.refresh_from_db()
        self.assertEqual(box.company_id, destination.id)
        self.assertEqual(box.item_code, 'MART-FG-009')
        self.assertEqual(box.batch_number, 'BATCH-MAP')
        self.assertEqual(box.qty, Decimal('12.50'))
        self.assertEqual(box.uom, 'PCS')
        self.assertEqual(box.current_warehouse, 'FG01')

        reversed_transfer = service.reverse_transfer(transfer.id, reason='Reverse mapped item')
        self.assertEqual(reversed_transfer.status, IntercompanyTransferStatus.REVERSED)
        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'OIL-FG-001')

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_mart_to_oil_maps_destination_item_code_back(self, mock_oitm_service):
        # Reverse direction: a JIVO MART box carrying the Mart code converts back to
        # the Oil code read from the SAME Mart item's OITM U_Oil_ItemCode column.
        source = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        destination = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='MART-FG-384',
            batch='BATCH-REVMAP',
        )[0]
        mock_oitm_service.return_value.find_oil_item_code_by_mart_item_code.return_value = 'OIL-FG-379'

        service = IntercompanyTransferService(self.user)
        transfer = service.create_transfer(
            source_company_code=source.code,
            destination_company_code=destination.code,
            barcodes=[box.box_barcode],
            notes='Move mapped item back',
        )

        # Reverse map reads the SOURCE (Jivo Mart) OITM, not the destination.
        mock_oitm_service.assert_called_once_with(company_code=source.code)
        mock_oitm_service.return_value.find_oil_item_code_by_mart_item_code.assert_called_once_with('MART-FG-384')
        line = transfer.lines.get()
        self.assertEqual(line.item_code, 'MART-FG-384')
        box.refresh_from_db()
        self.assertEqual(box.company_id, destination.id)
        self.assertEqual(box.item_code, 'OIL-FG-379')

        reversed_transfer = service.reverse_transfer(transfer.id, reason='Reverse it back')
        self.assertEqual(reversed_transfer.status, IntercompanyTransferStatus.REVERSED)
        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'MART-FG-384')

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_mart_to_oil_rejects_missing_item_mapping(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        destination = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='MART-FG-BLANK',
            batch='BATCH-REVNOMAP',
        )[0]
        mock_oitm_service.return_value.find_oil_item_code_by_mart_item_code.return_value = None
        transfer_count = IntercompanyTransfer.objects.count()

        with self.assertRaisesMessage(
            IntercompanyTransferError,
            'Item mapping not found in Jivo Mart for Jivo Mart ItemCode: MART-FG-BLANK. '
            'Please maintain U_Oil_ItemCode in Jivo Mart OITM table.',
        ):
            IntercompanyTransferService(self.user).create_transfer(
                source_company_code=source.code,
                destination_company_code=destination.code,
                barcodes=[box.box_barcode],
            )

        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'MART-FG-BLANK')
        self.assertEqual(IntercompanyTransfer.objects.count(), transfer_count)

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_oil_to_mart_rejects_missing_item_mapping(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='OIL-FG-MISSING',
            batch='BATCH-NOMAP',
        )[0]
        mock_oitm_service.return_value.find_item_codes_by_oil_item_code.return_value = []
        transfer_count = IntercompanyTransfer.objects.count()

        with self.assertRaisesMessage(
            IntercompanyTransferError,
            'Item mapping not found in Jivo Mart for Oil ItemCode: OIL-FG-MISSING. '
            'Please maintain U_Oil_ItemCode in Jivo Mart OITM table.',
        ):
            IntercompanyTransferService(self.user).create_transfer(
                source_company_code=source.code,
                destination_company_code=destination.code,
                barcodes=[box.box_barcode],
            )

        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'OIL-FG-MISSING')
        self.assertEqual(IntercompanyTransfer.objects.count(), transfer_count)

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_oil_to_mart_rejects_duplicate_item_mapping(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='OIL-FG-DUP',
            batch='BATCH-DUPMAP',
        )[0]
        mock_oitm_service.return_value.find_item_codes_by_oil_item_code.return_value = [
            'MART-FG-001',
            'MART-FG-002',
        ]
        transfer_count = IntercompanyTransfer.objects.count()

        with self.assertRaisesMessage(
            IntercompanyTransferError,
            'Duplicate item mapping found in Jivo Mart for Oil ItemCode: OIL-FG-DUP. '
            'Please correct duplicate U_Oil_ItemCode values in Jivo Mart OITM table.',
        ):
            IntercompanyTransferService(self.user).create_transfer(
                source_company_code=source.code,
                destination_company_code=destination.code,
                barcodes=[box.box_barcode],
            )

        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'OIL-FG-DUP')
        self.assertEqual(IntercompanyTransfer.objects.count(), transfer_count)

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_oil_to_mart_rejects_blank_or_null_mapping(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='OIL-FG-BLANK',
            batch='BATCH-BLANKMAP',
        )[0]
        mock_oitm_service.return_value.find_item_codes_by_oil_item_code.return_value = []
        transfer_count = IntercompanyTransfer.objects.count()

        with self.assertRaisesMessage(
            IntercompanyTransferError,
            'Item mapping not found in Jivo Mart for Oil ItemCode: OIL-FG-BLANK. '
            'Please maintain U_Oil_ItemCode in Jivo Mart OITM table.',
        ):
            IntercompanyTransferService(self.user).create_transfer(
                source_company_code=source.code,
                destination_company_code=destination.code,
                barcodes=[box.box_barcode],
            )

        box.refresh_from_db()
        self.assertEqual(box.company_id, source.id)
        self.assertEqual(box.item_code, 'OIL-FG-BLANK')
        self.assertEqual(IntercompanyTransfer.objects.count(), transfer_count)

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_item_mapping_is_skipped_for_other_company_pairs(self, mock_oitm_service):
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes(count=1, batch='BATCH-SKIP')[0]

        IntercompanyTransferService(self.user).create_transfer(
            source_company_code=self.company.code,
            destination_company_code=destination.code,
            barcodes=[box.box_barcode],
        )

        mock_oitm_service.assert_not_called()
        box.refresh_from_db()
        self.assertEqual(box.company_id, destination.id)
        self.assertEqual(box.item_code, 'FG001')

    @patch('barcode.services.box_ownership.OitmItemService')
    def test_intercompany_oil_to_mart_scan_does_not_resolve_destination_mapping(self, mock_oitm_service):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=source, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes_for_company(
            source.code,
            item_code='OIL-FG-SCAN',
            batch='BATCH-SCAN',
        )[0]

        scan = IntercompanyTransferService(self.user).scan_barcode(
            barcode=json.dumps({'type': 'BOX', 'box_barcode': box.box_barcode}),
            source_company_code=source.code,
            destination_company_code=destination.code,
        )

        mock_oitm_service.assert_not_called()
        self.assertEqual(scan['barcode'], box.box_barcode)
        self.assertEqual(scan['item_code'], 'OIL-FG-SCAN')
        self.assertEqual(scan['current_company'], source.code)

    def test_intercompany_scan_accepts_legacy_qr_payload_and_one_dimensional_value(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes(count=1, batch='BATCH-QR')[0]
        service = IntercompanyTransferService(self.user)

        legacy_payload = json.dumps({'type': 'BOX', 'box_barcode': box.box_barcode})
        legacy_scan = service.scan_barcode(
            barcode=legacy_payload,
            source_company_code=self.company.code,
            destination_company_code=destination.code,
        )
        self.assertEqual(legacy_scan['barcode'], box.box_barcode)

        one_dimensional_value = f"B{box.box_barcode.replace('-', '')}"
        one_dimensional_scan = service.scan_barcode(
            barcode=one_dimensional_value,
            source_company_code=self.company.code,
            destination_company_code=destination.code,
        )
        self.assertEqual(one_dimensional_scan['barcode'], box.box_barcode)

    def test_intercompany_pallet_transfer_moves_pallet_and_boxes(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        boxes = self._generate_boxes(count=2, qty='6.00', batch='BATCH-PLT')
        pallet = self.service.create_pallet(
            {'warehouse': 'FG01', 'production_line': 'Line 1'},
            user=self.user,
        )
        self.service.add_boxes_to_pallet(pallet.id, [box.id for box in boxes], user=self.user)
        pallet.refresh_from_db()
        service = IntercompanyTransferService(self.user)

        scan = service.scan_barcode(
            barcode=json.dumps({'type': 'PALLET', 'pallet_id': pallet.pallet_id}),
            source_company_code=self.company.code,
            destination_company_code=destination.code,
            transfer_type='PALLET',
        )

        self.assertEqual(scan['entity_type'], EntityType.PALLET)
        self.assertEqual(scan['barcode'], pallet.pallet_id)
        self.assertEqual(scan['box_count'], 2)

        transfer = service.create_transfer(
            source_company_code=self.company.code,
            destination_company_code=destination.code,
            transfer_type='PALLET',
            barcodes=[f"P{pallet.pallet_id.replace('-', '')}"],
            notes='Move full pallet',
        )

        self.assertEqual(transfer.entity_type, EntityType.PALLET)
        self.assertEqual(transfer.total_barcodes, 2)
        self.assertEqual(transfer.total_qty, Decimal('12.00'))
        self.assertEqual(transfer.lines.count(), 2)
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, destination.id)
        for box in boxes:
            box.refresh_from_db()
            self.assertEqual(box.company_id, destination.id)

        reversed_transfer = service.reverse_transfer(transfer.id, reason='Wrong pallet')
        self.assertEqual(reversed_transfer.status, IntercompanyTransferStatus.REVERSED)
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, self.company.id)
        for box in boxes:
            box.refresh_from_db()
            self.assertEqual(box.company_id, self.company.id)

    def test_intercompany_scan_rejects_wrong_selected_type(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes(count=1, batch='BATCH-TYPE')[0]
        service = IntercompanyTransferService(self.user)

        with self.assertRaisesMessage(IntercompanyTransferError, 'Select Box transfer type'):
            service.scan_barcode(
                barcode=box.box_barcode,
                source_company_code=self.company.code,
                destination_company_code=destination.code,
                transfer_type='PALLET',
            )

    def test_intercompany_transfer_api_end_to_end(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        role = UserRole.objects.create(name='Supervisor')
        UserCompany.objects.create(user=self.user, company=self.company, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=role, is_active=True)
        box = self._generate_boxes(count=1, qty='7.00', batch='BATCH-API')[0]
        client = APIClient()
        client.force_authenticate(user=self.user)
        headers = {'HTTP_COMPANY_CODE': self.company.code}

        scan_response = client.post(
            '/api/v1/barcode/intercompany/scan/',
            {
                'barcode': json.dumps({'type': 'BOX', 'box_barcode': box.box_barcode}),
                'source_company_code': self.company.code,
                'destination_company_code': destination.code,
            },
            format='json',
            **headers,
        )
        self.assertEqual(scan_response.status_code, 200)
        self.assertEqual(scan_response.data['barcode'], box.box_barcode)

        create_response = client.post(
            '/api/v1/barcode/intercompany/transfers/',
            {
                'source_company_code': self.company.code,
                'destination_company_code': destination.code,
                'barcodes': [box.box_barcode],
                'notes': 'API test',
            },
            format='json',
            **headers,
        )
        self.assertEqual(create_response.status_code, 201)
        transfer_id = create_response.data['id']
        self.assertEqual(create_response.data['total_barcodes'], 1)

        dashboard_response = client.get('/api/v1/barcode/intercompany/dashboard/', **headers)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertGreaterEqual(dashboard_response.data['today']['barcode_count'], 1)

        detail_response = client.get(
            f'/api/v1/barcode/intercompany/transfers/{transfer_id}/',
            **headers,
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data['lines'][0]['barcode'], box.box_barcode)

        trace_response = client.get(
            '/api/v1/barcode/intercompany/trace/',
            {'search': box.box_barcode},
            **headers,
        )
        self.assertEqual(trace_response.status_code, 200)
        self.assertTrue(trace_response.data['history'])

        reverse_response = client.post(
            f'/api/v1/barcode/intercompany/transfers/{transfer_id}/reverse/',
            {'reason': 'API test reverse'},
            format='json',
            **headers,
        )
        self.assertEqual(reverse_response.status_code, 200)
        self.assertEqual(reverse_response.data['status'], IntercompanyTransferStatus.REVERSED)

    def test_intercompany_transfer_rejects_wrong_source_company(self):
        destination = Company.objects.create(name='JIVO MART', code='JMART')
        other = Company.objects.create(name='JIVO EXPORT', code='JEXP')
        role = UserRole.objects.create(name='Supervisor')
        for company in (self.company, destination, other):
            UserCompany.objects.create(user=self.user, company=company, role=role, is_active=True)
        box = self._generate_boxes(count=1)[0]

        with self.assertRaisesMessage(
            IntercompanyTransferError,
            'does not belong',
        ):
            IntercompanyTransferService(self.user).create_transfer(
                source_company_code=other.code,
                destination_company_code=destination.code,
                barcodes=[box.box_barcode],
            )

    def test_pallet_sequence_uses_global_unique_namespace(self):
        first = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': '',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )
        other_company = Company.objects.create(
            name='Other Barcode Company',
            code='OTHERCO',
        )
        other_service = BarcodeService(company_code=other_company.code)

        second = other_service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': '',
                'mfg_date': date(2026, 5, 7),
            },
            user=self.user,
        )

        self.assertEqual(first.pallet_id, 'PLT-20260507-XX-001')
        self.assertEqual(second.pallet_id, 'PLT-20260507-XX-002')

    def test_pallet_create_serializer_allows_blank_production_line(self):
        serializer = PalletCreateSerializer(
            data={
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': '',
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['production_line'], '')

    def test_pallet_create_serializer_rejects_linked_boxes(self):
        serializer = PalletCreateSerializer(
            data={
                'box_ids': [1, 2, 3],
                'warehouse': 'FG01',
                'production_line': '',
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('box_ids', serializer.errors)

    def test_box_generate_serializer_allows_large_label_batches(self):
        serializer = BoxGenerateSerializer(
            data={
                'item_code': 'FG001',
                'item_name': 'Test Finished Good',
                'batch_number': 'BATCH-001',
                'qty': '1.00',
                'box_count': MAX_BOX_LABELS_PER_REQUEST,
                'uom': 'PCS',
                'mfg_date': '2026-05-07',
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_box_generate_serializer_rejects_extreme_label_batches(self):
        serializer = BoxGenerateSerializer(
            data={
                'item_code': 'FG001',
                'item_name': 'Test Finished Good',
                'batch_number': 'BATCH-001',
                'qty': '1.00',
                'box_count': MAX_BOX_LABELS_PER_REQUEST + 1,
                'uom': 'PCS',
                'mfg_date': '2026-05-07',
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('box_count', serializer.errors)

    def test_scan_service_handles_exact_qr_one_d_and_missing_barcodes(self):
        boxes = self._generate_boxes(count=3)
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id,
            [box.id for box in boxes],
            user=self.user,
        )

        exact_box = self.scan_service.process_scan(boxes[0].box_barcode, 'LOOKUP')
        self.assertEqual(exact_box['result'], ScanResult.SUCCESS)
        self.assertEqual(exact_box['entity_type'], EntityType.BOX)
        self.assertEqual(exact_box['entity_data']['box_barcode'], boxes[0].box_barcode)

        qr_box = self.scan_service.process_scan(json.dumps(boxes[1].barcode_data), 'LOOKUP')
        self.assertEqual(qr_box['result'], ScanResult.SUCCESS)
        self.assertEqual(qr_box['entity_data']['box_barcode'], boxes[1].box_barcode)

        one_d_box = self.scan_service.lookup_barcode(
            'B' + boxes[2].box_barcode.replace('-', '')
        )
        self.assertEqual(one_d_box['entity_type'], EntityType.BOX)
        self.assertEqual(one_d_box['entity_data']['box_barcode'], boxes[2].box_barcode)

        qr_pallet_lookup = self.scan_service.lookup_barcode(json.dumps(pallet.barcode_data))
        self.assertEqual(qr_pallet_lookup['entity_type'], EntityType.PALLET)
        self.assertEqual(qr_pallet_lookup['entity_data']['pallet_id'], pallet.pallet_id)

        one_d_pallet = self.scan_service.process_scan(
            'P' + pallet.pallet_id.replace('-', ''),
            'LOOKUP',
        )
        self.assertEqual(one_d_pallet['result'], ScanResult.SUCCESS)
        self.assertEqual(one_d_pallet['entity_data']['pallet_id'], pallet.pallet_id)

        missing = self.scan_service.process_scan('NOT-A-REAL-BARCODE', 'LOOKUP')
        self.assertEqual(missing['result'], ScanResult.NOT_FOUND)
        self.assertEqual(missing['entity_type'], EntityType.UNKNOWN)
        self.assertEqual(ScanLog.objects.count(), 4)

    def test_void_dismantle_and_repack_flow(self):
        boxes = self._generate_boxes(count=2, qty='10.00')
        pallet = self.service.create_pallet(
            {
                'box_ids': [],
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id,
            [box.id for box in boxes],
            user=self.user,
        )

        voided_box = self.service.void_box(
            boxes[0].id,
            reason='Damaged label',
            user=self.user,
        )
        pallet.refresh_from_db()
        self.assertEqual(voided_box.status, BoxStatus.VOID)
        self.assertIsNone(voided_box.pallet)
        self.assertEqual(pallet.box_count, 1)
        self.assertEqual(pallet.total_qty, Decimal('10.00'))

        loose = self.service.dismantle_box(
            boxes[1].id,
            loose_qty=Decimal('4.00'),
            reason='REPACK',
            reason_notes='Pilot repack test',
            user=self.user,
        )
        boxes[1].refresh_from_db()
        pallet.refresh_from_db()
        self.assertEqual(boxes[1].status, BoxStatus.PARTIAL)
        self.assertEqual(boxes[1].qty, Decimal('6.00'))
        self.assertEqual(loose.status, LooseStockStatus.ACTIVE)
        self.assertEqual(loose.qty, Decimal('4.00'))
        self.assertEqual(pallet.total_qty, Decimal('6.00'))

        repacked_box = self.service.repack(
            loose_ids=[loose.id],
            qty_per_loose=None,
            warehouse='FG01',
            user=self.user,
        )
        loose.refresh_from_db()
        self.assertEqual(loose.status, LooseStockStatus.REPACKED)
        self.assertEqual(loose.qty, Decimal('0.00'))
        self.assertEqual(loose.repacked_into_box, repacked_box)
        self.assertTrue(repacked_box.box_barcode.startswith('BOX-'))

    def test_void_pallet_voids_selected_boxes_and_disassociates_rest(self):
        boxes = self._generate_boxes(count=3, qty='10.00')
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1'},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [box.id for box in boxes], user=self.user,
        )

        # Void the pallet but only VOID the first two boxes; the third survives.
        pallet = self.service.void_pallet(
            pallet.id,
            reason='QC reject',
            user=self.user,
            box_ids=[boxes[0].id, boxes[1].id],
        )

        self.assertEqual(pallet.status, PalletStatus.VOID)
        for box in boxes:
            box.refresh_from_db()
        self.assertEqual(boxes[0].status, BoxStatus.VOID)
        self.assertEqual(boxes[1].status, BoxStatus.VOID)
        self.assertEqual(boxes[2].status, BoxStatus.ACTIVE)
        # All boxes are disassociated from the voided pallet.
        self.assertTrue(all(box.pallet is None for box in boxes))
        # The void reason is recorded on the voided boxes' VOID movements (traceability).
        void_movement = boxes[0].movements.filter(movement_type=BoxMovementType.VOID).first()
        self.assertIsNotNone(void_movement)
        self.assertEqual(void_movement.notes, 'QC reject')
        self.assertEqual(void_movement.performed_by, self.user)

    def test_void_pallet_without_box_ids_keeps_boxes_active(self):
        boxes = self._generate_boxes(count=2, qty='10.00')
        pallet = self.service.create_pallet(
            {'box_ids': [], 'warehouse': 'FG01', 'production_line': 'Line 1'},
            user=self.user,
        )
        pallet = self.service.add_boxes_to_pallet(
            pallet.id, [box.id for box in boxes], user=self.user,
        )

        pallet = self.service.void_pallet(pallet.id, reason='', user=self.user)

        self.assertEqual(pallet.status, PalletStatus.VOID)
        for box in boxes:
            box.refresh_from_db()
            self.assertEqual(box.status, BoxStatus.ACTIVE)
            self.assertIsNone(box.pallet)

    def test_line_key_is_sanitized_to_fit_barcode_field(self):
        boxes = self._generate_boxes(
            count=1,
            line='Filler Line / East Wing / Extremely Long Name 1234567890',
        )

        self.assertLessEqual(len(boxes[0].box_barcode), 50)
        self.assertNotIn('/', boxes[0].box_barcode)

    def test_label_print_log_stores_tsc_printer_name(self):
        box = self._generate_boxes(count=1)[0]
        label_data = self.label_service.get_box_label_data(box.id)

        self.label_service.log_print(
            label_type='BOX',
            reference_id=box.id,
            reference_code=label_data['barcode'],
            print_type='ORIGINAL',
            user=self.user,
            printer_name='TSC DA310 - 100 x 40 mm',
        )

        log = LabelPrintLog.objects.get(reference_code=box.box_barcode)
        self.assertEqual(log.printer_name, 'TSC DA310 - 100 x 40 mm')

    def test_production_release_oil_row_normalizes_for_label_dropdown(self):
        row = ProductionReleaseOilService._normalize_row({
            'DocEntry': 9465,
            'DocNum': 226926733,
            'PostDate': date(2026, 2, 20),
            'ItemCode': 'FG0000003',
            'ItemName': 'COLD PRESS 5 LTR + EXTRA LIGHT OLIVE 1 LTR 4 PCS',
            'Liter Countable': 'Y',
            'ManBtchNum': 'Y',
            'PlannedQty': Decimal('336.00'),
            'Box': Decimal('84.00'),
            'Liter': Decimal('1680.00'),
            'Box Size': Decimal('4.00'),
            'Volume Per Pc': Decimal('5.00'),
            'Volume Per Box': Decimal('20.00'),
            'U_BATCH_NO': 'BATCH-001',
            'MFG Date': date(2026, 2, 20),
            'Expiry Date': date(2027, 2, 20),
            'Status': 'R',
        })

        self.assertEqual(row['doc_entry'], 9465)
        self.assertEqual(row['doc_num'], 226926733)
        self.assertEqual(row['post_date'], '2026-02-20')
        self.assertEqual(row['item_code'], 'FG0000003')
        self.assertEqual(row['batch_number'], 'BATCH-001')
        self.assertEqual(row['box_count'], '84')
        self.assertEqual(row['box_size'], '4')
        self.assertEqual(row['mfg_date'], '2026-02-20')
        self.assertEqual(row['exp_date'], '2027-02-20')

    @patch('barcode.services.oitm_item_service.SAPClient')
    def test_oitm_item_service_looks_up_jivo_mart_item_by_oil_item_code(self, mock_sap_client):
        mock_sap_client.return_value.context.config = {
            'hana': {'schema': 'JIVO_MART_HANADB'},
        }
        service = OitmItemService(company_code='JIVO_MART')

        with patch.object(service, '_execute') as execute:
            self.assertEqual(service.find_item_codes_by_oil_item_code('   '), [])
            execute.assert_not_called()

        with patch.object(
            service,
            '_execute',
            return_value=[
                {'ItemCode': 'MART-FG-009'},
                {'ItemCode': ''},
                {'ItemCode': None},
            ],
        ) as execute:
            self.assertEqual(
                service.find_item_codes_by_oil_item_code('OIL-FG-001'),
                ['MART-FG-009'],
            )

        sql, params = execute.call_args.args
        self.assertIn('FROM "JIVO_MART_HANADB"."OITM"', sql)
        self.assertIn('WHERE "U_Oil_ItemCode" = ?', sql)
        self.assertEqual(params, ('OIL-FG-001',))

    @patch('barcode.services.oitm_item_service.SAPClient')
    def test_oitm_item_service_looks_up_oil_item_code_by_jivo_mart_item(self, mock_sap_client):
        mock_sap_client.return_value.context.config = {
            'hana': {'schema': 'JIVO_MART_HANADB'},
        }
        service = OitmItemService(company_code='JIVO_MART')

        with patch.object(service, '_execute') as execute:
            self.assertIsNone(service.find_oil_item_code_by_mart_item_code('   '))
            execute.assert_not_called()

        # Blank / missing U_Oil_ItemCode → no mapping.
        with patch.object(service, '_execute', return_value=[{'U_Oil_ItemCode': '  '}]):
            self.assertIsNone(service.find_oil_item_code_by_mart_item_code('FG0000384'))
        with patch.object(service, '_execute', return_value=[]):
            self.assertIsNone(service.find_oil_item_code_by_mart_item_code('FG0000384'))

        with patch.object(
            service,
            '_execute',
            return_value=[{'U_Oil_ItemCode': 'FG0000379'}],
        ) as execute:
            self.assertEqual(
                service.find_oil_item_code_by_mart_item_code('FG0000384'),
                'FG0000379',
            )

        sql, params = execute.call_args.args
        self.assertIn('FROM "JIVO_MART_HANADB"."OITM"', sql)
        self.assertIn('WHERE "ItemCode" = ?', sql)
        self.assertEqual(params, ('FG0000384',))


class FakeDispatchSapAdapter:
    def __init__(self, bill, update_status=DispatchSapUpdateStatus.NOT_CONFIGURED):
        self.bill = bill
        self.update_status = update_status
        self.update_calls = 0

    def lookup_bill(self, bill_number):
        if bill_number != self.bill['bill_number']:
            return None
        return deepcopy(self.bill)

    def update_dispatch_status(self, session):
        self.update_calls += 1
        return SapUpdateResult(
            status=self.update_status,
            message='SAP update failed in test adapter.' if self.update_status == DispatchSapUpdateStatus.FAILED else 'SAP update disabled in test adapter.',
            request_payload={'bill_number': session.bill_number},
            response_payload={},
        )


class BarcodeDispatchWorkflowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name='Dispatch Barcode Company',
            code='DISPATCHCO',
        )
        self.user = User.objects.create_user(
            email='dispatch-barcode@example.com',
            password='test-pass',
            full_name='Dispatch Barcode Tester',
            employee_code='EMP-BCD-001',
        )
        # Barcode endpoints now require a barcode permission (see barcode/permissions.py).
        self.user.user_permissions.set(
            Permission.objects.filter(content_type__app_label='barcode')
        )
        self.role = UserRole.objects.create(name='Dispatch Admin')
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.defaults['HTTP_COMPANY_CODE'] = self.company.code
        self.barcode_service = BarcodeService(company_code=self.company.code)
        self.adapter = FakeDispatchSapAdapter(self._bill())
        self.dispatch_service = BarcodeDispatchService(
            company_code=self.company.code,
            sap_adapter=self.adapter,
        )

    def _bill(self, *, bill_number='900001', item1_qty='10.000', item2_qty='5.000'):
        return {
            'source_system': 'BUSINESS_ONE',
            'sap_object_type': 'AR_INVOICE',
            'bill_number': bill_number,
            'bill_internal_id': '101',
            'bill_date': '2026-05-23',
            'already_dispatched': False,
            'sap_dispatch_status': 'OPEN',
            'reference_delivery_number': '800001',
            'customer': {
                'code': 'CUST001',
                'name': 'ABC Traders',
                'ship_to_code': 'SHIP001',
                'ship_to_name': 'ABC Traders Warehouse',
            },
            'lines': [
                {
                    'sequence_no': 1,
                    'sap_line_no': '10',
                    'material_code': 'FG001',
                    'material_description': 'First item',
                    'quantity': item1_qty,
                    'total_boxes': '2.000',
                    'uom': 'PCS',
                    'batch_number': 'BATCH-001',
                    'warehouse_code': 'FG01',
                    'serial_required': False,
                },
                {
                    'sequence_no': 2,
                    'sap_line_no': '20',
                    'material_code': 'FG002',
                    'material_description': 'Second item',
                    'quantity': item2_qty,
                    'total_boxes': '1.000',
                    'uom': 'PCS',
                    'batch_number': 'BATCH-002',
                    'warehouse_code': 'FG01',
                    'serial_required': False,
                },
            ],
            'raw': {},
        }

    def _box(self, item_code='FG001', batch='BATCH-001', qty='10.00'):
        return self._boxes(item_code=item_code, batch=batch, qty=qty, count=1)[0]

    def _boxes(self, item_code='FG001', batch='BATCH-001', qty='10.00', count=1):
        return self.barcode_service.generate_boxes(
            {
                'item_code': item_code,
                'item_name': f'{item_code} item',
                'batch_number': batch,
                'qty': Decimal(qty),
                'box_count': count,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )

    def _pallet_with_boxes(self, boxes):
        pallet = self.barcode_service.create_pallet(
            {'warehouse': 'FG01', 'production_line': 'Line 1'},
            user=self.user,
        )
        self.barcode_service.add_boxes_to_pallet(pallet.id, [box.id for box in boxes], self.user)
        pallet.refresh_from_db()
        return pallet

    def test_create_session_from_sap_bill_snapshot(self):
        session = self.dispatch_service.create_session('900001', self.user)

        self.assertEqual(session.bill_number, '900001')
        self.assertEqual(session.customer_code, 'CUST001')
        self.assertEqual(session.reference_delivery_number, '800001')
        self.assertEqual(session.status, DispatchSessionStatus.DRAFT)
        self.assertEqual(session.delivery_number, '800001')
        self.assertEqual(session.total_expected_qty, Decimal('15.000'))
        self.assertEqual(session.lines.count(), 2)
        self.assertEqual(
            list(session.lines.values_list('sequence_no', 'material_code', 'bill_qty', 'bill_boxes')),
            [
                (1, 'FG001', Decimal('10.000'), Decimal('2.000')),
                (2, 'FG002', Decimal('5.000'), Decimal('1.000')),
            ],
        )

    def test_create_session_allows_bill_marked_dispatched_in_sap(self):
        self.adapter.bill = {
            **self._bill(),
            'already_dispatched': True,
            'sap_dispatch_status': 'DISPATCHED',
            'raw': {'sap_dispatch_date': '2026-05-25'},
        }

        session = self.dispatch_service.create_session('900001', self.user)

        self.assertEqual(session.bill_number, '900001')
        self.assertEqual(session.sap_dispatch_status, 'DISPATCHED')
        self.assertEqual(session.status, DispatchSessionStatus.DRAFT)

    def test_scan_rejects_later_item_until_current_line_is_complete(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')

        scan = self.dispatch_service.submit_scan(
            session.id,
            item2_box.box_barcode,
            user=self.user,
        )

        self.assertEqual(scan.result, DispatchScanResult.REJECTED)
        self.assertEqual(scan.reject_code, 'LINE_SEQUENCE_VIOLATION')
        self.assertEqual(DispatchScanLog.objects.count(), 1)

    def test_non_sequential_scan_uses_selected_line(self):
        DispatchSettings.objects.update_or_create(
            company=self.company,
            defaults={'require_sequential_item_scanning': False},
        )
        self.dispatch_service._settings = None
        session = self.dispatch_service.create_session('900001', self.user)
        item2_line = session.lines.get(sequence_no=2)
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')

        scan = self.dispatch_service.submit_scan(
            session.id,
            item2_box.box_barcode,
            user=self.user,
            line_id=item2_line.id,
        )

        self.assertEqual(scan.result, DispatchScanResult.ACCEPTED)
        session.refresh_from_db()
        self.assertEqual(session.lines.get(sequence_no=1).scanned_qty, Decimal('0.000'))
        self.assertEqual(session.lines.get(sequence_no=2).scanned_qty, Decimal('5.000'))
        self.assertEqual(DispatchScannedUnit.objects.get().line_id, item2_line.id)

    def test_non_sequential_selected_line_still_rejects_wrong_material(self):
        DispatchSettings.objects.update_or_create(
            company=self.company,
            defaults={'require_sequential_item_scanning': False},
        )
        self.dispatch_service._settings = None
        session = self.dispatch_service.create_session('900001', self.user)
        item2_line = session.lines.get(sequence_no=2)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='5.00')

        scan = self.dispatch_service.submit_scan(
            session.id,
            item1_box.box_barcode,
            user=self.user,
            line_id=item2_line.id,
        )

        self.assertEqual(scan.result, DispatchScanResult.REJECTED)
        self.assertEqual(scan.reject_code, 'WRONG_MATERIAL')

    def test_scan_rejects_wrong_material_and_auto_allocates_pending_quantity(self):
        session = self.dispatch_service.create_session('900001', self.user)
        wrong_box = self._box(item_code='FG009', batch='BATCH-009', qty='1.00')
        over_box = self._box(item_code='FG001', batch='BATCH-001', qty='11.00')

        wrong_scan = self.dispatch_service.submit_scan(
            session.id,
            wrong_box.box_barcode,
            user=self.user,
        )
        over_scan = self.dispatch_service.submit_scan(
            session.id,
            over_box.box_barcode,
            user=self.user,
        )

        self.assertEqual(wrong_scan.result, DispatchScanResult.REJECTED)
        self.assertEqual(wrong_scan.reject_code, 'WRONG_MATERIAL')
        self.assertEqual(over_scan.result, DispatchScanResult.ACCEPTED)
        unit = DispatchScannedUnit.objects.get(box=over_box)
        self.assertEqual(unit.total_box_qty, Decimal('11.000'))
        self.assertEqual(unit.dispatch_qty, Decimal('10.000'))
        self.assertEqual(unit.remaining_qty, Decimal('1.000'))
        self.assertEqual(DispatchScanLog.objects.filter(result='REJECTED').count(), 1)

    def test_scan_accepts_correct_sequence_and_blocks_duplicate_barcode(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')

        accepted = self.dispatch_service.submit_scan(
            session.id,
            item1_box.box_barcode,
            user=self.user,
        )
        duplicate = self.dispatch_service.submit_scan(
            session.id,
            item1_box.box_barcode,
            user=self.user,
        )

        session.refresh_from_db()
        line1 = session.lines.get(sequence_no=1)
        self.assertEqual(accepted.result, DispatchScanResult.ACCEPTED)
        self.assertEqual(line1.scanned_qty, Decimal('10.000'))
        self.assertEqual(line1.status, 'COMPLETE')
        self.assertEqual(duplicate.result, DispatchScanResult.REJECTED)
        self.assertEqual(duplicate.reject_code, 'BOX_ALREADY_SCANNED')
        self.assertEqual(DispatchScannedUnit.objects.count(), 1)
        item1_box.refresh_from_db()
        self.assertEqual(item1_box.status, BoxStatus.ACTIVE)
        self.assertIsNone(item1_box.dispatch_session_id)

    def test_scanned_box_quantity_can_be_reduced_and_remaining_is_calculated(self):
        self.adapter = FakeDispatchSapAdapter(self._bill(item1_qty='20.000', item2_qty='5.000'))
        self.dispatch_service = BarcodeDispatchService(
            company_code=self.company.code,
            sap_adapter=self.adapter,
        )
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='20.00')

        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        unit = DispatchScannedUnit.objects.get(box=item1_box)
        self.assertEqual(unit.total_box_qty, Decimal('20.000'))
        self.assertEqual(unit.dispatch_qty, Decimal('20.000'))

        updated = self.dispatch_service.update_scanned_box_qty(session.id, unit.id, Decimal('12'), self.user)
        unit.refresh_from_db()
        line = updated.lines.get(sequence_no=1)
        self.assertEqual(unit.dispatch_qty, Decimal('12.000'))
        self.assertEqual(unit.remaining_qty, Decimal('8.000'))
        self.assertEqual(line.scanned_qty, Decimal('12.000'))

        dispatched = self.dispatch_service.mark_dispatched(session.id, self.user)
        item1_box.refresh_from_db()
        self.assertEqual(dispatched.total_scanned_qty, Decimal('12.000'))
        self.assertEqual(item1_box.status, BoxStatus.PARTIAL)
        self.assertEqual(item1_box.qty, Decimal('8.000'))

    def test_box_scan_auto_dispatches_only_pending_quantity(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='20.00')

        scan = self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.ACCEPTED)
        unit = DispatchScannedUnit.objects.get(box=item1_box)
        self.assertEqual(unit.total_box_qty, Decimal('20.000'))
        self.assertEqual(unit.dispatch_qty, Decimal('10.000'))
        self.assertEqual(unit.remaining_qty, Decimal('10.000'))
        self.assertEqual(session.lines.get(sequence_no=1).scanned_qty, Decimal('10.000'))

        self.dispatch_service.mark_dispatched(session.id, self.user)
        item1_box.refresh_from_db()
        self.assertEqual(item1_box.status, BoxStatus.PARTIAL)
        self.assertEqual(item1_box.qty, Decimal('10.000'))

    def test_scanned_box_quantity_validation_rejects_zero_negative_and_over_box_qty(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        unit = DispatchScannedUnit.objects.get(box=item1_box)

        with self.assertRaises(DispatchValidationError) as zero_ctx:
            self.dispatch_service.update_scanned_box_qty(session.id, unit.id, Decimal('0'), self.user)
        self.assertEqual(zero_ctx.exception.code, 'INVALID_DISPATCH_QTY')

        with self.assertRaises(DispatchValidationError) as negative_ctx:
            self.dispatch_service.update_scanned_box_qty(session.id, unit.id, Decimal('-1'), self.user)
        self.assertEqual(negative_ctx.exception.code, 'INVALID_DISPATCH_QTY')

        with self.assertRaises(DispatchValidationError) as over_ctx:
            self.dispatch_service.update_scanned_box_qty(session.id, unit.id, Decimal('11'), self.user)
        self.assertEqual(over_ctx.exception.code, 'DISPATCH_QTY_GT_BOX_QTY')

    def test_remove_scanned_box_excludes_it_from_dispatch_totals(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        unit = DispatchScannedUnit.objects.get(box=item1_box)

        updated = self.dispatch_service.remove_scanned_box(session.id, unit.id, self.user)
        unit.refresh_from_db()
        line = updated.lines.get(sequence_no=1)
        self.assertEqual(unit.scan_status, 'REMOVED')
        self.assertEqual(unit.dispatch_qty, Decimal('0.000'))
        self.assertEqual(line.scanned_qty, Decimal('0.000'))
        self.assertEqual(updated.total_scanned_qty, Decimal('0.000'))

        item1_box.refresh_from_db()
        self.assertEqual(item1_box.status, BoxStatus.ACTIVE)
        self.assertIsNone(item1_box.dispatch_session_id)

    def test_mark_dispatched_requires_all_lines_and_logs_sap_sync(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')

        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(session.id, item2_box.box_barcode, user=self.user)

        dispatched = self.dispatch_service.mark_dispatched(session.id, self.user)

        self.assertEqual(dispatched.status, DispatchSessionStatus.COMPLETED)
        self.assertEqual(dispatched.sap_update_status, DispatchSapUpdateStatus.NOT_CONFIGURED)
        self.assertEqual(dispatched.dispatched_by, self.user)
        self.assertEqual(DispatchSapSyncLog.objects.count(), 1)
        self.assertEqual(self.adapter.update_calls, 1)

    def test_dispatch_accepts_boxes_after_intercompany_transfer_to_destination(self):
        source = Company.objects.create(name='JIVO OIL', code='JIVO_OIL')
        destination = Company.objects.create(name='JIVO MART', code='JIVO_MART')
        UserCompany.objects.create(user=self.user, company=source, role=self.role, is_active=True)
        UserCompany.objects.create(user=self.user, company=destination, role=self.role, is_active=True)
        source_service = BarcodeService(company_code=source.code)
        destination_dispatch_service = BarcodeDispatchService(
            company_code=destination.code,
            sap_adapter=self.adapter,
        )
        item1_box = source_service.generate_boxes(
            {
                'item_code': 'FG001',
                'item_name': 'FG001 item',
                'batch_number': 'BATCH-001',
                'qty': Decimal('10.00'),
                'box_count': 1,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )[0]
        item2_box = source_service.generate_boxes(
            {
                'item_code': 'FG002',
                'item_name': 'FG002 item',
                'batch_number': 'BATCH-002',
                'qty': Decimal('5.00'),
                'box_count': 1,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )[0]

        transfer = IntercompanyTransferService(self.user).create_transfer(
            source_company_code=source.code,
            destination_company_code=destination.code,
            barcodes=[item1_box.box_barcode, item2_box.box_barcode],
            notes='Move Oil stock to Mart for dispatch',
        )
        self.assertEqual(transfer.status, IntercompanyTransferStatus.COMPLETED)
        item1_box.refresh_from_db()
        item2_box.refresh_from_db()
        self.assertEqual(item1_box.company_id, destination.id)
        self.assertEqual(item2_box.company_id, destination.id)

        session = destination_dispatch_service.create_session('900001', self.user)
        item1_scan = destination_dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        item2_scan = destination_dispatch_service.submit_scan(session.id, item2_box.box_barcode, user=self.user)
        dispatched = destination_dispatch_service.mark_dispatched(session.id, self.user)

        self.assertEqual(item1_scan.result, DispatchScanResult.ACCEPTED)
        self.assertEqual(item2_scan.result, DispatchScanResult.ACCEPTED)
        self.assertEqual(dispatched.status, DispatchSessionStatus.COMPLETED)
        item1_box.refresh_from_db()
        item2_box.refresh_from_db()
        self.assertEqual(item1_box.company_id, destination.id)
        self.assertEqual(item2_box.company_id, destination.id)
        self.assertEqual(item1_box.status, BoxStatus.DISPATCHED)
        self.assertEqual(item2_box.status, BoxStatus.DISPATCHED)

    def test_active_completed_and_closed_session_lists(self):
        active = self.dispatch_service.create_session('900001', self.user)
        self.assertIn(active, list(self.dispatch_service.list_sessions(status_group='active')))

        self.dispatch_service.cancel_session(active.id, 'Wrong bill selected', self.user)
        self.assertIn(active.id, [session.id for session in self.dispatch_service.list_sessions(status_group='closed')])

        self.adapter.bill = self._bill(bill_number='900002')
        completed = self.dispatch_service.create_session('900002', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')
        self.dispatch_service.submit_scan(completed.id, item1_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(completed.id, item2_box.box_barcode, user=self.user)
        completed = self.dispatch_service.mark_dispatched(completed.id, self.user)

        self.assertIn(completed.id, [session.id for session in self.dispatch_service.list_sessions(status_group='completed')])

    def test_pallet_scan_marks_all_boxes_dispatched(self):
        session = self.dispatch_service.create_session('900001', self.user)
        boxes = self._boxes(item_code='FG001', batch='BATCH-001', qty='5.00', count=2)
        pallet = self._pallet_with_boxes(boxes)

        scan = self.dispatch_service.submit_scan(session.id, pallet.pallet_id, user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.ACCEPTED)
        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.ACTIVE)
        self.assertIsNone(pallet.dispatch_session_id)
        self.assertEqual(DispatchScannedUnit.objects.filter(pallet=pallet, entity_type='BOX').count(), 2)

        self.dispatch_service.mark_dispatched(session.id, self.user)
        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.DISPATCHED)
        self.assertEqual(pallet.dispatch_session_id, session.id)
        self.assertEqual(pallet.dispatched_boxes, 2)
        self.assertEqual(Box.objects.filter(id__in=[box.id for box in boxes], status=BoxStatus.DISPATCHED).count(), 2)

    def test_box_scan_from_pallet_removes_box_and_keeps_pallet_partial(self):
        session = self.dispatch_service.create_session('900001', self.user)
        boxes = self._boxes(item_code='FG001', batch='BATCH-001', qty='5.00', count=2)
        pallet = self._pallet_with_boxes(boxes)

        scan = self.dispatch_service.submit_scan(session.id, boxes[0].box_barcode, user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.ACCEPTED)
        boxes[0].refresh_from_db()
        pallet.refresh_from_db()
        self.assertEqual(boxes[0].pallet_id, pallet.id)
        self.assertEqual(boxes[0].status, BoxStatus.ACTIVE)
        self.assertIsNone(boxes[0].removed_from_pallet_at)

        self.dispatch_service.mark_dispatched(session.id, self.user)
        boxes[0].refresh_from_db()
        pallet.refresh_from_db()
        self.assertIsNone(boxes[0].pallet_id)
        self.assertEqual(boxes[0].status, BoxStatus.DISPATCHED)
        self.assertIsNotNone(boxes[0].removed_from_pallet_at)
        self.assertEqual(pallet.status, PalletStatus.PARTIAL)
        self.assertEqual(pallet.available_boxes, 1)
        self.assertEqual(pallet.box_history.filter(action='BOX_DISPATCHED_SEPARATELY').count(), 1)

    def test_remaining_pallet_dispatch_after_one_box_removed_obeys_partial_rule(self):
        session = self.dispatch_service.create_session('900001', self.user)
        boxes = self._boxes(item_code='FG001', batch='BATCH-001', qty='5.00', count=2)
        pallet = self._pallet_with_boxes(boxes)
        self.dispatch_service.submit_scan(session.id, boxes[0].box_barcode, user=self.user)

        scan = self.dispatch_service.submit_scan(session.id, pallet.pallet_id, user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.ACCEPTED)
        pallet.refresh_from_db()
        self.assertIn('already dispatched or removed', scan.parsed_barcode.get('warning', ''))
        self.assertEqual(session.lines.get(sequence_no=1).scanned_qty, Decimal('10.000'))

        self.dispatch_service.mark_dispatched(session.id, self.user)
        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.DISPATCHED)

    def test_partial_pallet_dispatch_can_be_rejected_by_setting(self):
        DispatchSettings.objects.update_or_create(
            company=self.company,
            defaults={'allow_partial_pallet_dispatch': False},
        )
        self.dispatch_service._settings = None
        session = self.dispatch_service.create_session('900001', self.user)
        boxes = self._boxes(item_code='FG001', batch='BATCH-001', qty='5.00', count=2)
        pallet = self._pallet_with_boxes(boxes)
        self.dispatch_service.submit_scan(session.id, boxes[0].box_barcode, user=self.user)

        scan = self.dispatch_service.submit_scan(session.id, pallet.pallet_id, user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.REJECTED)
        self.assertEqual(scan.reject_code, 'PALLET_HAS_DISPATCHED_BOXES')

    def test_completed_and_closed_dispatches_do_not_allow_scanning(self):
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')
        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(session.id, item2_box.box_barcode, user=self.user)
        completed = self.dispatch_service.mark_dispatched(session.id, self.user)

        extra = self._box(item_code='FG001', batch='BATCH-001', qty='1.00')
        rejected = self.dispatch_service.submit_scan(completed.id, extra.box_barcode, user=self.user)
        self.assertEqual(rejected.result, DispatchScanResult.REJECTED)
        self.assertEqual(rejected.reject_code, 'SESSION_CLOSED')

        self.adapter.bill = self._bill(bill_number='900002')
        closed = self.dispatch_service.create_session('900002', self.user)
        self.dispatch_service.close_session(closed.id, 'Manual close test', self.user)
        rejected_closed = self.dispatch_service.submit_scan(closed.id, extra.box_barcode, user=self.user)
        self.assertEqual(rejected_closed.result, DispatchScanResult.REJECTED)
        self.assertEqual(rejected_closed.reject_code, 'SESSION_CLOSED')

    def test_sap_sync_failure_is_stored_as_failed_status(self):
        self.adapter = FakeDispatchSapAdapter(
            self._bill(),
            update_status=DispatchSapUpdateStatus.FAILED,
        )
        self.dispatch_service = BarcodeDispatchService(
            company_code=self.company.code,
            sap_adapter=self.adapter,
        )
        session = self.dispatch_service.create_session('900001', self.user)
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')
        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(session.id, item2_box.box_barcode, user=self.user)

        failed = self.dispatch_service.mark_dispatched(session.id, self.user)

        self.assertEqual(failed.status, DispatchSessionStatus.SAP_SYNC_FAILED)
        self.assertEqual(failed.sap_update_status, DispatchSapUpdateStatus.FAILED)
        self.assertTrue(failed.sap_update_error)

    def test_report_queries_return_dispatch_pallet_box_and_rejections(self):
        session = self.dispatch_service.create_session('900001', self.user)
        wrong_box = self._box(item_code='FG009', batch='BATCH-009', qty='1.00')
        item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
        item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')
        self.dispatch_service.submit_scan(session.id, wrong_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(session.id, item1_box.box_barcode, user=self.user)
        self.dispatch_service.submit_scan(session.id, item2_box.box_barcode, user=self.user)
        self.dispatch_service.mark_dispatched(session.id, self.user)

        self.assertEqual(len(self.dispatch_service.dispatch_report({'bill_number': '900001'})), 1)
        self.assertEqual(self.dispatch_service.dispatch_detail_report(session.id)['session']['bill_number'], '900001')
        self.assertGreaterEqual(len(self.dispatch_service.box_report({'bill_number': '900001'})), 2)
        self.assertEqual(len(self.dispatch_service.rejected_scan_report({'bill_number': '900001'})), 1)

    def test_dispatch_api_create_list_scan_complete_close_and_reports(self):
        with patch.object(SapDispatchAdapter, 'lookup_bill', autospec=True) as lookup_bill:
            lookup_bill.side_effect = lambda _adapter, bill_number: deepcopy(
                self._bill(bill_number=bill_number)
            )
            response = self.client.post(
                '/api/v1/barcode/dispatch/sessions/from-bill/',
                {'bill_number': '900001'},
                format='json',
            )
            self.assertEqual(response.status_code, 201)
            session_id = response.data['id']
            active_response = self.client.get('/api/v1/barcode/dispatch/sessions/active/')
            self.assertEqual(active_response.status_code, 200)
            self.assertEqual(active_response.data[0]['id'], session_id)

            item1_box = self._box(item_code='FG001', batch='BATCH-001', qty='10.00')
            scan_response = self.client.post(
                f'/api/v1/barcode/dispatch/sessions/{session_id}/scan/',
                {'barcode': item1_box.box_barcode},
                format='json',
            )
            self.assertEqual(scan_response.status_code, 200)

            item2_box = self._box(item_code='FG002', batch='BATCH-002', qty='5.00')
            scan_response = self.client.post(
                f'/api/v1/barcode/dispatch/sessions/{session_id}/scan/',
                {'barcode': item2_box.box_barcode},
                format='json',
            )
            self.assertEqual(scan_response.status_code, 200)

            complete_response = self.client.post(f'/api/v1/barcode/dispatch/sessions/{session_id}/complete/')
            self.assertEqual(complete_response.status_code, 200)
            self.assertEqual(complete_response.data['status'], DispatchSessionStatus.COMPLETED)

            report_response = self.client.get('/api/v1/barcode/dispatch/reports/', {'bill_number': '900001'})
            self.assertEqual(report_response.status_code, 200)
            self.assertEqual(report_response.data[0]['bill_number'], '900001')
            box_report_response = self.client.get('/api/v1/barcode/dispatch/reports/boxes/')
            self.assertEqual(box_report_response.status_code, 200)

            close_response = self.client.post(
                '/api/v1/barcode/dispatch/sessions/from-bill/',
                {'bill_number': '900002'},
                format='json',
            )
            close_session_id = close_response.data['id']
            close_response = self.client.post(
                f'/api/v1/barcode/dispatch/sessions/{close_session_id}/close/',
                {'reason': 'API close test'},
                format='json',
            )
            self.assertEqual(close_response.status_code, 200)
            self.assertEqual(close_response.data['status'], DispatchSessionStatus.CLOSED)

    def test_api_settings_history_rejected_report_and_csv_export(self):
        with patch.object(SapDispatchAdapter, 'lookup_bill', autospec=True) as lookup_bill:
            lookup_bill.side_effect = lambda _adapter, bill_number: deepcopy(
                self._bill(bill_number=bill_number)
            )

            settings_response = self.client.get('/api/v1/barcode/dispatch/settings/')
            self.assertEqual(settings_response.status_code, 200)
            self.assertTrue(settings_response.data['allow_box_dispatch_from_pallet'])

            settings_response = self.client.patch(
                '/api/v1/barcode/dispatch/settings/',
                {'allow_partial_pallet_dispatch': False},
                format='json',
            )
            self.assertEqual(settings_response.status_code, 200)
            self.assertFalse(settings_response.data['allow_partial_pallet_dispatch'])

            session_response = self.client.post(
                '/api/v1/barcode/dispatch/sessions/from-bill/',
                {'bill_number': '900003'},
                format='json',
            )
            self.assertEqual(session_response.status_code, 201)
            session_id = session_response.data['id']

            boxes = self._boxes(item_code='FG001', batch='BATCH-001', qty='5.00', count=2)
            pallet = self._pallet_with_boxes(boxes)
            wrong_box = self._box(item_code='FG009', batch='BATCH-009', qty='1.00')

            rejected_response = self.client.post(
                f'/api/v1/barcode/dispatch/sessions/{session_id}/scan/',
                {'barcode': wrong_box.box_barcode},
                format='json',
            )
            self.assertEqual(rejected_response.status_code, 400)
            self.assertEqual(rejected_response.data['scan']['result'], DispatchScanResult.REJECTED)
            self.assertEqual(rejected_response.data['scan']['reject_code'], 'WRONG_MATERIAL')

            accepted_response = self.client.post(
                f'/api/v1/barcode/dispatch/sessions/{session_id}/scan/',
                {'barcode': boxes[0].box_barcode},
                format='json',
            )
            self.assertEqual(accepted_response.status_code, 200)
            boxes[0].refresh_from_db()
            pallet.refresh_from_db()
            self.assertIsNone(boxes[0].pallet_id)
            self.assertEqual(pallet.status, PalletStatus.PARTIAL)

            pallet_history_response = self.client.get(f'/api/v1/barcode/pallets/{pallet.id}/history/')
            self.assertEqual(pallet_history_response.status_code, 200)
            self.assertEqual(pallet_history_response.data[0]['action'], 'BOX_DISPATCHED_SEPARATELY')

            box_history_response = self.client.get(f'/api/v1/barcode/boxes/{boxes[0].id}/history/')
            self.assertEqual(box_history_response.status_code, 200)
            self.assertEqual(box_history_response.data[0]['box'], boxes[0].id)

            rejected_report_response = self.client.get(
                '/api/v1/barcode/dispatch/reports/rejected-scans/',
                {'bill_number': '900003'},
            )
            self.assertEqual(rejected_report_response.status_code, 200)
            self.assertEqual(rejected_report_response.data[0]['rejection_code'], 'WRONG_MATERIAL')

            pallet_report_response = self.client.get(
                '/api/v1/barcode/dispatch/reports/pallets/',
                {'pallet_barcode': pallet.pallet_id},
            )
            self.assertEqual(pallet_report_response.status_code, 200)
            self.assertEqual(pallet_report_response.data[0]['pallet_barcode'], pallet.pallet_id)

            csv_response = self.client.get(
                '/api/v1/barcode/dispatch/reports/',
                {'bill_number': '900003', 'export': 'csv'},
            )
            self.assertEqual(csv_response.status_code, 200)
            self.assertIn('attachment;', csv_response['Content-Disposition'])
            self.assertIn(b'bill_number', csv_response.content)

    def test_unknown_barcode_scan_is_rejected_and_audited(self):
        session = self.dispatch_service.create_session('900001', self.user)

        scan = self.dispatch_service.submit_scan(session.id, 'UNKNOWN-BARCODE', user=self.user)

        self.assertEqual(scan.result, DispatchScanResult.REJECTED)
        self.assertEqual(scan.reject_code, 'BARCODE_NOT_FOUND')
        self.assertEqual(
            DispatchScanLog.objects.filter(result=DispatchScanResult.REJECTED).count(),
            1,
        )


class DispatchSettlementTests(TestCase):
    """settle_dispatched_boxes: mark docking-scanned stock dispatched + free WMS.

    Exercises the shared settlement used by the gate_core docking flow, which
    records box scans without touching the underlying Box/Pallet.
    """

    def setUp(self):
        self.company = Company.objects.create(name='Settle Co', code='SETTLECO')
        self.user = User.objects.create_user(
            email='settle@example.com', password='test-pass',
            full_name='Settle Tester', employee_code='EMP-STL-001',
        )
        self.barcode_service = BarcodeService(company_code=self.company.code)

    def _boxes(self, count=1, item_code='FG001', batch='BATCH-001', qty='10.00'):
        return self.barcode_service.generate_boxes(
            {
                'item_code': item_code,
                'item_name': f'{item_code} item',
                'batch_number': batch,
                'qty': Decimal(qty),
                'box_count': count,
                'uom': 'PCS',
                'mfg_date': date(2026, 5, 7),
                'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01',
                'production_line': 'Line 1',
            },
            user=self.user,
        )

    def _pallet_with_boxes(self, boxes):
        pallet = self.barcode_service.create_pallet(
            {'warehouse': 'FG01', 'production_line': 'Line 1'}, user=self.user,
        )
        self.barcode_service.add_boxes_to_pallet(pallet.id, [b.id for b in boxes], self.user)
        pallet.refresh_from_db()
        return pallet

    def _fresh_boxes(self, boxes):
        """Re-read boxes with their pallet loaded, as the real dispatch/backfill
        paths do (select_related('box__pallet')) — the settlement resolves the
        pallet from box.pallet."""
        return list(
            Box.objects.filter(id__in=[b.id for b in boxes]).select_related('pallet')
        )

    def _wms_pallet_at_bin(self, pallet, location_id='LOC-AD-14'):
        """Mirror a barcode pallet onto the WMS map at a bin, as putaway does."""
        from wms.models import Inventory as WmsInventory, Pallet as WmsPallet
        wms_pallet = WmsPallet.objects.create(
            company=self.company,
            record_id=f'wms-{pallet.pallet_id}',
            data={
                'id': f'wms-{pallet.pallet_id}',
                'status': 'ACTIVE',
                'licensePlate': pallet.pallet_id,
                'currentLocationId': location_id,
                'boxCount': pallet.box_count,
                'totalUnits': float(pallet.total_qty or 0),
            },
        )
        WmsInventory.objects.create(
            company=self.company,
            record_id=f'inv-{pallet.pallet_id}',
            data={'id': f'inv-{pallet.pallet_id}', 'palletId': wms_pallet.record_id,
                  'locationId': location_id, 'boxCount': pallet.box_count},
        )
        return wms_pallet

    def test_settle_dispatches_boxes_and_frees_wms_bin(self):
        from wms.models import Inventory as WmsInventory, Pallet as WmsPallet
        from .services.dispatch_settlement import settle_dispatched_boxes

        boxes = self._boxes(count=3)
        pallet = self._pallet_with_boxes(boxes)
        self._wms_pallet_at_bin(pallet)

        result = settle_dispatched_boxes(self.company, self._fresh_boxes(boxes), self.user)

        self.assertEqual(result['boxes_dispatched'], 3)
        self.assertEqual(result['pallets_reconciled'], 1)
        for box in boxes:
            box.refresh_from_db()
            self.assertEqual(box.status, BoxStatus.DISPATCHED)
            self.assertIsNotNone(box.dispatched_at)
        self.assertEqual(
            BoxMovement.objects.filter(
                box__in=boxes, movement_type=BoxMovementType.DISPATCH
            ).count(),
            3,
        )
        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.DISPATCHED)
        self.assertEqual(pallet.box_count, 0)
        # The emptied pallet is removed from the WMS map, freeing its bin.
        self.assertFalse(
            WmsPallet.objects.filter(company=self.company, data__licensePlate=pallet.pallet_id).exists()
        )
        self.assertFalse(
            WmsInventory.objects.filter(company=self.company, record_id=f'inv-{pallet.pallet_id}').exists()
        )

    def test_settle_is_idempotent(self):
        from .services.dispatch_settlement import settle_dispatched_boxes
        boxes = self._boxes(count=2)
        self._pallet_with_boxes(boxes)

        first = settle_dispatched_boxes(self.company, self._fresh_boxes(boxes), self.user)
        self.assertEqual(first['boxes_dispatched'], 2)
        # Second run settles nothing and creates no extra movements.
        second = settle_dispatched_boxes(self.company, self._fresh_boxes(boxes), self.user)
        self.assertEqual(second['boxes_dispatched'], 0)
        self.assertEqual(
            BoxMovement.objects.filter(box__in=boxes, movement_type=BoxMovementType.DISPATCH).count(),
            2,
        )

    def test_settle_partial_pallet_keeps_bin_with_fewer_boxes(self):
        from wms.models import Inventory as WmsInventory, Pallet as WmsPallet
        from .services.dispatch_settlement import settle_dispatched_boxes

        boxes = self._boxes(count=3)
        pallet = self._pallet_with_boxes(boxes)
        self._wms_pallet_at_bin(pallet)

        # Only one of the three boxes leaves on the docking.
        settle_dispatched_boxes(self.company, [self._fresh_boxes(boxes)[0]], self.user)

        pallet.refresh_from_db()
        self.assertEqual(pallet.status, PalletStatus.PARTIAL)
        self.assertEqual(pallet.box_count, 2)
        # Pallet still occupies its bin, but the WMS box count is decremented.
        wms_pallet = WmsPallet.objects.get(company=self.company, data__licensePlate=pallet.pallet_id)
        self.assertEqual(wms_pallet.data['boxCount'], 2)


class ScanMissDiagnosticTests(TestCase):
    """ScanService.explain_scan_miss / log_scan_miss_if_unknown — turn a bare
    company-scoped 'not found' into an actionable reason, and make BST-style
    misses visible in ScanLog."""

    def setUp(self):
        self.oil = Company.objects.create(name='Jivo Oil', code='JIVO_OIL')
        self.mart = Company.objects.create(name='Jivo Mart', code='JIVO_MART')
        self.user = User.objects.create_user(
            email='miss@example.com', password='x',
            full_name='Miss Tester', employee_code='EMP-MISS-1',
        )
        # A box owned by MART.
        self.mart_box = BarcodeService(company_code=self.mart.code).generate_boxes(
            {
                'item_code': 'FG001', 'item_name': 'Test FG', 'batch_number': 'B1',
                'qty': Decimal('4.00'), 'box_count': 1, 'uom': 'PCS',
                'mfg_date': date(2026, 5, 7), 'exp_date': date(2027, 5, 7),
                'warehouse': 'FG01', 'production_line': 'L1',
            },
            user=self.user,
        )[0].box_barcode

    def test_other_company_box_gets_actionable_message(self):
        result = ScanService(self.oil.code).explain_scan_miss(self.mart_box)
        self.assertEqual(result['code'], 'OTHER_COMPANY')
        self.assertIn('Jivo Mart', result['message'])
        self.assertIn('Jivo Oil', result['message'])

    def test_unknown_barcode_says_does_not_exist(self):
        result = ScanService(self.oil.code).explain_scan_miss('BOX-19990101-XX-9999')
        self.assertEqual(result['code'], 'UNKNOWN_BARCODE')
        self.assertIn('does not exist', result['message'])

    def test_empty_barcode(self):
        result = ScanService(self.oil.code).explain_scan_miss('   ')
        self.assertEqual(result['code'], 'EMPTY')

    def test_log_scan_miss_records_not_found_for_foreign_company_box(self):
        before = ScanLog.objects.count()
        log = ScanService(self.oil.code).log_scan_miss_if_unknown(
            self.mart_box, context_ref_type='BST_TRANSFER', context_ref_id=1,
            user=self.user,
        )
        self.assertIsNotNone(log)
        self.assertEqual(log.scan_result, ScanResult.NOT_FOUND)
        self.assertEqual(log.company_id, self.oil.id)
        self.assertEqual(ScanLog.objects.count(), before + 1)

    def test_log_scan_miss_noops_when_barcode_resolves_for_company(self):
        # Scanned by the OWNING company -> it resolves -> no NOT_FOUND log.
        before = ScanLog.objects.count()
        log = ScanService(self.mart.code).log_scan_miss_if_unknown(
            self.mart_box, context_ref_type='BST_TRANSFER', context_ref_id=1,
            user=self.user,
        )
        self.assertIsNone(log)
        self.assertEqual(ScanLog.objects.count(), before)
