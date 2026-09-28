"""An FG receipt SAP did not take stays FAILED, with the reason, after the rollback.

`post_fg_receipt_to_sap` marks the receipt FAILED as it raises, inside its own
atomic block, so the mark used to roll back and the receipt came back RECEIVED
as if nobody had tried. And a Service Layer that did not answer at all escaped
as a raw ``requests`` exception, which the view does not catch: a 500.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from company.models import Company
from production_execution.models import ProductionLine, ProductionRun, RunStatus
from sap_client.exceptions import SAPOutcomeUnknown, SAPUnavailable
from warehouse.models import FGReceiptStatus, FinishedGoodsReceipt
from warehouse.services.warehouse_service import WarehouseService


class FGReceiptFailureKeptTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code='JIVO_OIL', name='Oil')
        line = ProductionLine.objects.create(company=self.company, name='Line-1')
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date(2026, 9, 18), line=line,
            product='FG0000372', item_code='FG0000372',
            total_production=Decimal('100'), status=RunStatus.COMPLETED,
        )
        self.receipt = FinishedGoodsReceipt.objects.create(
            company=self.company, production_run=self.run, sap_doc_entry=42,
            item_code='FG0000372', produced_qty=Decimal('100'),
            good_qty=Decimal('100'), warehouse='BH-FG',
            posting_date=date(2026, 9, 18), status=FGReceiptStatus.RECEIVED,
        )
        self.service = WarehouseService('JIVO_OIL')

    def post(self, session):
        client = MagicMock()
        client.context.service_layer = {
            'base_url': 'https://sap', 'company_db': 'DB', 'username': 'u', 'password': 'p',
        }
        with patch('sap_client.client.SAPClient', return_value=client), \
             patch('requests.Session', return_value=session), \
             patch.object(WarehouseService, '_get_branch_for_order', return_value=None):
            self.service.post_fg_receipt_to_sap(self.receipt.id)

    def test_a_refusal_leaves_the_receipt_failed_with_sap_s_reason(self):
        session = MagicMock()
        refused = MagicMock(ok=False, status_code=400)
        refused.json.return_value = {'error': {'message': {'value': 'Quantity exceeds planned'}}}
        session.post.side_effect = [MagicMock(ok=True), refused]

        with self.assertRaisesMessage(ValueError, 'Quantity exceeds planned'):
            self.post(session)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, FGReceiptStatus.FAILED)
        self.assertEqual(self.receipt.sap_error, 'Quantity exceeds planned')

    def test_a_login_that_never_answers_is_sap_unavailable_not_a_crash(self):
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ReadTimeout('login hung')

        with self.assertRaises(SAPUnavailable):
            self.post(session)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, FGReceiptStatus.FAILED)
        self.assertIn('did not answer the login', self.receipt.sap_error)

    def test_a_post_that_times_out_says_it_may_have_landed(self):
        session = MagicMock()
        session.post.side_effect = [
            MagicMock(ok=True), requests.exceptions.ReadTimeout('no answer'),
        ]

        with self.assertRaises(SAPOutcomeUnknown):
            self.post(session)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, FGReceiptStatus.FAILED)
        self.assertIn('check SAP before posting again', self.receipt.sap_error)

    def test_a_failed_receipt_can_still_be_posted_again(self):
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ConnectTimeout('no route')
        with self.assertRaises(SAPUnavailable):
            self.post(session)

        retry = MagicMock()
        retry.post.return_value = MagicMock(ok=True, json=lambda: {'DocEntry': 7})
        self.post(retry)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, FGReceiptStatus.SAP_POSTED)
        self.assertEqual(self.receipt.sap_error, '')
