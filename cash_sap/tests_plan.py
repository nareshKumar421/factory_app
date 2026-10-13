"""What would post: the plan rules (spec §2.3.6), read from JI only.

Every voucher must resolve to ITEM, SERVICE or SKIP before anything posts,
and every reason it cannot is a problem on the voucher, its company group or
the bunch. These tests walk the rules one at a time.

Run: ``python manage.py test cash_sap.tests_plan --settings=config.sqlite_test_settings``
"""

from datetime import date

from django.test import override_settings

from cash_book.models import CashDirection
from cash_sap.models import (
    CashBranchSapRouting,
    GLItemMap,
    ImprestBatch,
    ImprestVoucher,
    SapDocStatus,
)
from cash_sap.plan import build_plan, group_key_for
from cash_sap.support_for_tests import (
    REAL_FLAGS,
    SIMULATE_FLAGS,
    TODAY,
    CashSapFixture,
    patch_guard,
)


def problem_codes(obj):
    return [p.code for p in obj.problems]


def by_entry(plan, entry):
    for v in plan.skipped + plan.unrouted + [v for g in plan.groups for v in g.vouchers]:
        if v.entry_id == entry.id:
            return v
    raise AssertionError(f"voucher {entry.id} is not in the plan")


@override_settings(**SIMULATE_FLAGS)
class TreatmentTests(CashSapFixture):
    def test_mapped_goods_head_is_item_with_dims(self):
        e = self.voucher(bill_date=date(2026, 9, 2), gate_entry_no="773")
        plan = build_plan(self.bunch(e), today=TODAY)
        v = by_entry(plan, e)
        self.assertEqual((v.treatment, v.treatment_source), ("ITEM", "MAP"))
        self.assertEqual(v.item_code, "CG0000015")
        self.assertEqual(v.account_code, "5630004")  # home company: same code
        self.assertEqual((v.ocr_code, v.ocr_code2, v.ocr_code3, v.ocr_code5), ("CANOLA", "09-2026", "Factory", "HR"))
        self.assertEqual(v.tax_code, "IGST@0")
        self.assertEqual(v.gate_entry_no, "773")
        self.assertEqual(v.bill_date, date(2026, 9, 2))

    def test_mapped_service_head(self):
        e = self.voucher(gl="5690002", gl_name="CONVEYANCE")
        v = by_entry(build_plan(self.bunch(e), today=TODAY), e)
        self.assertEqual(v.treatment, "SERVICE")
        self.assertEqual(v.tax_code, "Exampt")

    def test_unmapped_head_defaults_to_service_with_a_warning(self):
        e = self.voucher(gl="5699999", gl_name="SOMETHING")
        v = by_entry(build_plan(self.bunch(e), today=TODAY), e)
        self.assertEqual((v.treatment, v.treatment_source), ("SERVICE", "DEFAULT"))
        self.assertIn("UNMAPPED_HEAD", problem_codes(v))
        self.assertFalse(v.blocking)

    def test_skips(self):
        cash_in = self.voucher(direction=CashDirection.IN, bill=False)
        salary = self.voucher(gl="1101015", gl_name="SUNDRY DEBTORS STAFF")
        never = self.voucher(gl="5660099", gl_name="DONATION")
        plan = build_plan(self.bunch(cash_in, salary, never), today=TODAY)
        self.assertEqual(by_entry(plan, cash_in).skip_reason, "Cash received — never posted")
        self.assertEqual(by_entry(plan, salary).skip_reason, "Salary advance — never posted")
        self.assertEqual(by_entry(plan, never).skip_reason, "Head 5660099 is never posted")

    def test_override_skip_and_cutover(self):
        skipped = self.voucher()
        old = self.voucher(entry_date=date(2026, 8, 1))
        bunch = self.bunch(skipped, old)
        ImprestVoucher.objects.create(entry=skipped, bunch=bunch, treatment_override="SKIP",
                                      override_note="Keyed by hand")
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(cutover_date=date(2026, 9, 1))
        plan = build_plan(bunch, today=TODAY)
        self.assertEqual(by_entry(plan, skipped).skip_reason, "Keyed by hand")
        self.assertIn("cut-over", by_entry(plan, old).skip_reason)

    def test_not_approved_blocks_the_bunch(self):
        e = self.voucher(approved=False)
        plan = build_plan(self.bunch(e), today=TODAY)
        self.assertIn("NOT_APPROVED", problem_codes(by_entry(plan, e)))
        self.assertTrue(plan.blocking)
        self.assertFalse(plan.can_post)

    def test_unrouted_is_blocking(self):
        e = self.voucher(branch=self.mart)
        plan = build_plan(self.bunch(e), today=TODAY)
        self.assertEqual(by_entry(plan, e).treatment, "UNROUTED")
        self.assertIn("UNROUTED", problem_codes(plan))
        self.assertFalse(plan.can_post)

    def test_no_routing_for_an_override_company(self):
        e = self.voucher()
        bunch = self.bunch(e)
        ImprestVoucher.objects.create(entry=e, bunch=bunch, sap_company_code_override="JIVO_MART")
        plan = build_plan(bunch, today=TODAY)
        self.assertIn("NO_ROUTING_FOR_COMPANY", problem_codes(by_entry(plan, e)))

    def test_a_mart_override_uses_mart_routing(self):
        CashBranchSapRouting.objects.filter(pk=self.mart_routing.pk).update(
            enabled=True, sap_company_code="JIVO_MART", mode="PO_GRPO", vendor_code="MARTV1",
            bpl_id=5, warehouse_code="MT-FA", ocr_code="MART", item_tax_code="IGST@0")
        GLItemMap.objects.create(sap_company_code="JIVO_MART", gl_account_code="5630004",
                                 treatment="ITEM", item_code="CG0000015", target_account_code="M5630004")
        e = self.voucher()
        bunch = self.bunch(e)
        ImprestVoucher.objects.create(entry=e, bunch=bunch, sap_company_code_override="JIVO_MART")
        plan = build_plan(bunch, today=TODAY)
        v = by_entry(plan, e)
        self.assertEqual(v.routing_id, self.mart_routing.pk)
        self.assertEqual((v.ocr_code, v.warehouse_code, v.account_code), ("MART", "MT-FA", "M5630004"))
        self.assertEqual(plan.group("JIVO_MART").vendor_code, "MARTV1")

    def test_no_item_and_no_bill(self):
        GLItemMap.objects.filter(gl_account_code="5680015").update(item_code="")
        no_item = self.voucher(gl="5680015", gl_name="HOUSE KEEPING")
        no_bill = self.voucher(bill=False)
        plan = build_plan(self.bunch(no_item, no_bill), today=TODAY)
        self.assertIn("NO_ITEM", problem_codes(by_entry(plan, no_item)))
        self.assertIn("NO_BILL", problem_codes(by_entry(plan, no_bill)))

    def test_approval_proof_counts_as_a_bill(self):
        e = self.voucher(bill=False, proof=True)
        self.assertNotIn("NO_BILL", problem_codes(by_entry(build_plan(self.bunch(e), today=TODAY), e)))

    def test_multi_item_head_warns(self):
        e = self.voucher(gl="5680012", gl_name="PRINTING AND STATIONERY")
        self.assertIn("ITEM_REVIEW", problem_codes(by_entry(build_plan(self.bunch(e), today=TODAY), e)))

    def test_ap_draft_only_company_turns_goods_into_service(self):
        GLItemMap.objects.create(sap_company_code="JIVO_BEVERAGES", gl_account_code="5630004",
                                 treatment="ITEM", item_code="CG0000015", target_account_code="B5630004")
        e = self.voucher(branch=self.beverage)
        v = by_entry(build_plan(self.bunch(e), today=TODAY), e)
        self.assertEqual(v.treatment, "SERVICE")
        self.assertIn("AP_ONLY_COMPANY", problem_codes(v))

    def test_gl_not_in_target_only_when_it_would_be_sent(self):
        e = self.voucher(branch=self.beverage, gl="5690002", gl_name="CONVEYANCE")
        bunch = self.bunch(e)
        # Drafts off: nothing goes to Beverages, so no account is needed.
        self.assertNotIn("GL_NOT_IN_TARGET", problem_codes(by_entry(build_plan(bunch, today=TODAY), e)))
        CashBranchSapRouting.objects.filter(pk=self.beverage_routing.pk).update(ap_drafts_enabled=True)
        self.assertIn("GL_NOT_IN_TARGET", problem_codes(by_entry(build_plan(bunch, today=TODAY), e)))

    def test_future_bill_date_blocks_and_no_age_warning(self):
        old = self.voucher(entry_date=date(2026, 6, 20))
        future = self.voucher(entry_date=date(2026, 10, 8), bill_date=date(2026, 10, 20))
        plan = build_plan(self.bunch(old, future), today=TODAY)
        self.assertIn("FUTURE_DATE", problem_codes(by_entry(plan, future)))
        self.assertNotIn("DATE_OLD", [w["code"] for w in by_entry(plan, old).warnings])

    def test_override_from_another_bunch_is_ignored(self):
        e = self.voucher()
        first = self.bunch()
        ImprestVoucher.objects.create(entry=e, bunch=first, treatment_override="SKIP")
        second = self.bunch(e)
        v = by_entry(build_plan(second, today=TODAY), e)
        self.assertEqual(v.treatment, "ITEM")
        self.assertFalse(v.overridden)


@override_settings(**SIMULATE_FLAGS)
class GroupTests(CashSapFixture):
    def test_dates_and_months(self):
        a = self.voucher(entry_date=date(2026, 9, 3))
        b = self.voucher(entry_date=date(2026, 10, 2))
        plan = build_plan(self.bunch(a, b), today=TODAY)
        g = plan.group("JIVO_OIL")
        self.assertEqual(g.expense_month, date(2026, 9, 1))
        self.assertEqual(g.po_doc_date, date(2026, 9, 1))
        self.assertEqual(g.po_due_date, date(2026, 10, 2))
        self.assertIn("SPANS_MONTHS", problem_codes(g))
        self.assertEqual([v.ocr_code2 for v in g.vouchers], ["09-2026", "10-2026"])

    def test_routing_conflict_compares_headers_not_rows(self):
        a = self.voucher(branch=self.oil)
        b = self.voucher(branch=self.common)
        bunch = self.bunch(a, b)
        self.assertNotIn("ROUTING_CONFLICT", problem_codes(build_plan(bunch, today=TODAY).group("JIVO_OIL")))
        CashBranchSapRouting.objects.filter(pk=self.common_routing.pk).update(warehouse_code="DL-FA")
        self.assertIn("ROUTING_CONFLICT", problem_codes(build_plan(bunch, today=TODAY).group("JIVO_OIL")))

    def test_routing_incomplete(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(warehouse_code="")
        e = self.voucher()
        self.assertIn("ROUTING_INCOMPLETE", problem_codes(build_plan(self.bunch(e), today=TODAY).group("JIVO_OIL")))

    def test_beverages_with_drafts_off_sends_nothing(self):
        e = self.voucher(branch=self.beverage, gl="5690002", gl_name="CONVEYANCE")
        plan = build_plan(self.bunch(e), today=TODAY)
        g = plan.group("JIVO_BEVERAGES")
        self.assertFalse(g.sendable)
        self.assertIn("NOTHING_TO_POST", problem_codes(plan))
        self.assertEqual(plan.summary["state"], "NOTHING_SENT")

    def test_grouping_keys(self):
        e = self.voucher(bill_date=date(2026, 9, 24), gate_entry_no=" 773 ", supplier_bill_no="b-12")
        v = by_entry(build_plan(self.bunch(e), today=TODAY), e)
        self.assertEqual(group_key_for(v, "NONE"), f"V{e.id}")
        self.assertEqual(group_key_for(v, "GATE_NO"), "N:20260924:773")
        self.assertEqual(group_key_for(v, "BILL_NO"), "B:20260924:B-12")

    def test_preview_groups_by_gate_number(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(grpo_grouping="GATE_NO")
        a = self.voucher(bill_date=date(2026, 9, 24), gate_entry_no="773")
        b = self.voucher(bill_date=date(2026, 9, 24), gate_entry_no="773", amount="150.00")
        c = self.voucher(bill_date=date(2026, 9, 24), gate_entry_no="774")
        g = build_plan(self.bunch(a, b, c), today=TODAY).group("JIVO_OIL")
        self.assertEqual([gr["voucher_ids"] for gr in g.grpos], [[a.id, b.id], [c.id]])

    def test_simulate_can_post_and_live_blocks_without_simulate(self):
        e = self.voucher()
        bunch = self.bunch(e)
        self.assertTrue(build_plan(bunch, today=TODAY).can_post)
        patch_guard(self, test=False)
        with override_settings(**REAL_FLAGS):
            plan = build_plan(bunch, today=TODAY)
        self.assertIn("LIVE_SAP_BLOCKED", problem_codes(plan.group("JIVO_OIL")))
        self.assertFalse(plan.can_post)

    def test_test_target_is_not_blocked(self):
        patch_guard(self, test=True)
        e = self.voucher()
        with override_settings(**REAL_FLAGS):
            plan = build_plan(self.bunch(e), today=TODAY)
        self.assertNotIn("LIVE_SAP_BLOCKED", problem_codes(plan.group("JIVO_OIL")))

    def test_simulated_only_warning(self):
        e = self.voucher()
        bunch = self.bunch(e)
        ImprestBatch.objects.create(bunch=bunch, sap_company_code="JIVO_OIL", simulated=True,
                                    po_status=SapDocStatus.POSTED)
        patch_guard(self, test=True)
        with override_settings(**REAL_FLAGS):
            g = build_plan(bunch, today=TODAY).group("JIVO_OIL")
        self.assertIn("SIMULATED_ONLY", problem_codes(g))

    def test_frozen_per_treatment(self):
        item = self.voucher()
        service = self.voucher(gl="5690002", gl_name="CONVEYANCE")
        bunch = self.bunch(item, service)
        batch = ImprestBatch.objects.create(bunch=bunch, sap_company_code="JIVO_OIL",
                                            po_status=SapDocStatus.POSTED, po_doc_entry=1, po_doc_num=9)
        ImprestVoucher.objects.create(entry=item, bunch=bunch, batch=batch, treatment="ITEM",
                                      item_code="SNAP", amount=item.amount)
        ImprestVoucher.objects.create(entry=service, bunch=bunch, batch=batch, treatment="SERVICE",
                                      amount=service.amount)
        plan = build_plan(bunch, today=TODAY)
        self.assertTrue(by_entry(plan, item).frozen)
        self.assertEqual(by_entry(plan, item).item_code, "SNAP")
        self.assertFalse(by_entry(plan, service).frozen)

    def test_not_needed_po_never_freezes(self):
        e = self.voucher(gl="5690002", gl_name="CONVEYANCE")
        bunch = self.bunch(e)
        batch = ImprestBatch.objects.create(bunch=bunch, sap_company_code="JIVO_OIL",
                                            po_status=SapDocStatus.NOT_NEEDED)
        ImprestVoucher.objects.create(entry=e, bunch=bunch, batch=batch, treatment="ITEM")
        self.assertFalse(by_entry(build_plan(bunch, today=TODAY), e).frozen)

    def test_plan_is_json_ready(self):
        import json

        e = self.voucher()
        json.dumps(build_plan(self.bunch(e), today=TODAY).as_dict())


@override_settings(**SIMULATE_FLAGS)
class DriftInPlanTests(CashSapFixture):
    def test_a_voucher_taken_out_after_freezing_blocks(self):
        from cash_sap.models import BunchSapState
        from cash_sap import drift

        a, b = self.voucher(), self.voucher()
        bunch = self.bunch(a, b)
        BunchSapState.objects.create(bunch=bunch, status="FROZEN", frozen_at=TODAY_DT(),
                                     member_snapshot=drift.current_members(bunch))
        type(a).objects.filter(pk=b.pk).update(bunch=None)
        plan = build_plan(bunch, today=TODAY)
        self.assertIn("BUNCH_CHANGED", problem_codes(plan))
        self.assertFalse(plan.can_post)


def TODAY_DT():
    from django.utils import timezone

    return timezone.now()
