"""Freezing a bunch: what is snapshotted, which documents are laid out, and the
vendor references they get (spec §2.3.8 ``freeze_bunch``, D4, D12).

Run: ``python manage.py test cash_sap.tests_freeze --settings=config.sqlite_test_settings``
"""

from datetime import date

from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError

from cash_book.models import CashEntry
from cash_sap import services
from cash_sap.models import (
    BunchSapState,
    CashBranchSapRouting,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    SapDocStatus,
)
from cash_sap.support_for_tests import SIMULATE_FLAGS, CashSapFixture


@override_settings(**SIMULATE_FLAGS)
class FreezeTests(CashSapFixture):
    def test_po_lines_follow_plan_order_and_groups_are_made(self):
        a = self.voucher("350.00", bill_date=date(2026, 9, 3))
        b = self.voucher("150.00", gl="5680015", gl_name="HOUSE KEEPING", bill_date=date(2026, 9, 4))
        bunch = self.bunch(a, b)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(batch.po_status, SapDocStatus.PLANNED)
        self.assertTrue(batch.simulated)
        self.assertEqual((batch.vendor_code, batch.bpl_id, batch.warehouse_code), ("ORGV000465", 2, "BH-FA"))
        self.assertEqual(batch.po_doc_date, date(2026, 9, 1))
        ivs = {iv.entry_id: iv for iv in ImprestVoucher.objects.filter(batch=batch)}
        self.assertEqual((ivs[a.id].po_line_num, ivs[b.id].po_line_num), (0, 1))
        grpos = list(ImprestGRPO.objects.filter(batch=batch).order_by("id"))
        self.assertEqual([g.num_at_card for g in grpos], ["350/03-09-26", "150/04-09-26"])
        self.assertEqual(ivs[a.id].grpo_id, grpos[0].id)
        state = BunchSapState.objects.get(bunch=bunch)
        self.assertEqual(state.status, "FROZEN")
        self.assertEqual(set(state.member_snapshot), {str(a.id), str(b.id)})

    def test_a_beverages_only_bunch_is_not_frozen(self):
        e = self.voucher(branch=self.beverage, gl="5690002", gl_name="CONVEYANCE")
        bunch = self.bunch(e)
        with self.assertRaises(ValidationError):
            services.freeze_bunch(user=self.poster, bunch=bunch)
        self.assertFalse(ImprestBatch.objects.filter(bunch=bunch).exists())
        self.assertFalse(BunchSapState.objects.filter(bunch=bunch, frozen_at__isnull=False).exists())
        self.assertEqual(services.bunch_status_summary(bunch)["state"], "NOTHING_SENT")

    def test_blocking_problems_refuse_the_freeze(self):
        e = self.voucher(bill=False)
        with self.assertRaises(ValidationError) as caught:
            services.freeze_bunch(user=self.poster, bunch=self.bunch(e))
        self.assertIn("No bill photo", str(caught.exception.detail))

    def test_groups_are_rebuilt_only_before_the_po_is_committed(self):
        a = self.voucher("350.00")
        b = self.voucher("150.00")
        bunch = self.bunch(a, b)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(grpo_grouping="GATE_NO")
        services.freeze_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(ImprestGRPO.objects.filter(batch=batch).count(), 2)  # no G.no.: one each

        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=1, po_doc_num=5)
        before = sorted(ImprestGRPO.objects.filter(batch=batch).values_list("id", flat=True))
        services.freeze_bunch(user=self.poster, bunch=bunch)
        after = sorted(ImprestGRPO.objects.filter(batch=batch).values_list("id", flat=True))
        self.assertEqual(before, after)

    def test_num_at_card_is_deterministic_across_bunches(self):
        first = self.bunch(self.voucher("500.00", bill_date=date(2026, 9, 24)))
        second = self.bunch(self.voucher("500.00", bill_date=date(2026, 9, 24)))
        services.freeze_bunch(user=self.poster, bunch=first)
        services.freeze_bunch(user=self.poster, bunch=second)
        refs = sorted(ImprestGRPO.objects.values_list("num_at_card", flat=True))
        self.assertEqual(refs, ["500/24-09-26", "500/24-09-26."])

    def test_the_unique_constraint_holds(self):
        bunch = self.bunch(self.voucher("500.00", bill_date=date(2026, 9, 24)))
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImprestGRPO.objects.create(batch=batch, group_key="other", sap_company_code="JIVO_OIL",
                                       vendor_code="ORGV000465", bill_date=date(2026, 9, 24),
                                       posting_date=date(2026, 9, 24), amount=500,
                                       num_at_card="500/24-09-26")

    def test_service_draft_laid_out_when_drafts_are_on(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=True)
        item = self.voucher("3250.00")
        service = self.voucher("800.00", gl="5690002", gl_name="CONVEYANCE", bill_date=date(2026, 9, 28))
        bunch = self.bunch(item, service)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        drafts = {d.kind: d for d in ImprestAPDraft.objects.filter(batch=batch)}
        self.assertEqual(set(drafts), {"ITEM", "SERVICE"})
        self.assertEqual(drafts["SERVICE"].num_at_card, "SEP 26/4050/800")
        self.assertEqual(drafts["SERVICE"].doc_date, date(2026, 9, 28))
        self.assertEqual(drafts["ITEM"].num_at_card, "SEP 26/4050/3250")

    def test_lock_sql_has_no_outer_join(self):
        bunch = self.bunch(self.voucher())
        with CaptureQueriesContext(connection) as ctx:
            services.freeze_bunch(user=self.poster, bunch=bunch)
        for query in ctx.captured_queries:
            sql = query["sql"].upper()
            if "FOR UPDATE" in sql:
                self.assertNotIn("LEFT OUTER JOIN", sql)

    def test_a_changed_bunch_refuses_to_freeze_again(self):
        a, b = self.voucher(), self.voucher("10.00")
        bunch = self.bunch(a, b)
        services.freeze_bunch(user=self.poster, bunch=bunch)
        CashEntry.objects.filter(pk=b.pk).update(bunch=None)
        with self.assertRaises(services.BunchChanged):
            services.freeze_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(BunchSapState.objects.get(bunch=bunch).status, "CHANGED")
