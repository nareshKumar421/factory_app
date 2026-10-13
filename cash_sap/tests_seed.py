"""Migration 0002's routing and G/L map seed, run against the live app registry
(migrations are disabled in the test settings, so the function is called
directly, as ``cash_book``'s ``BranchMigrationMappingTests`` does).
"""

import importlib

from django.apps import apps as django_apps
from django.test import TestCase

from cash_book.models import CashBranch
from cash_sap.models import CashBranchSapRouting, GLItemMap
from company.models import Company

seed = importlib.import_module("cash_sap.migrations.0002_seed_and_groups")


class SeedTests(TestCase):
    def test_nothing_happens_without_jivo_oil(self):
        seed.seed_routing_and_map(django_apps, None)
        self.assertFalse(CashBranchSapRouting.objects.exists())
        self.assertFalse(GLItemMap.objects.exists())

    def test_routing_and_map_for_jivo_oil(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        for name in ("Oil", "beverage", "Water", "Common", "Mart", "Store"):
            CashBranch.objects.create(company=company, name=name)
        seed.seed_routing_and_map(django_apps, None)

        rows = {r.branch.name: r for r in CashBranchSapRouting.objects.select_related("branch")}
        self.assertEqual(set(rows), {"Oil", "beverage", "Water", "Common", "Mart"})
        oil = rows["Oil"]
        self.assertEqual(
            (oil.enabled, oil.sap_company_code, oil.mode, oil.vendor_code, oil.bpl_id, oil.warehouse_code,
             oil.ocr_code, oil.ocr_code3, oil.ocr_code5, oil.item_tax_code, oil.service_tax_code,
             oil.ap_drafts_enabled, oil.grpo_needs_attachment, oil.grpo_grouping, oil.ap_series_hint),
            (True, "JIVO_OIL", "PO_GRPO", "ORGV000465", 2, "BH-FA", "CANOLA", "Factory", "HR", "IGST@0",
             "Exampt", False, True, "NONE", "HR_B"),
        )
        # Beverages keeps the same PO + GRPO shape as Oil, in its own company,
        # on branch 2 (SAP's "Factory" budget needs it), Dim1 per branch.
        for name, dim1 in (("beverage", "DRINKS"), ("Water", "WATER")):
            bev = rows[name]
            self.assertEqual(
                (bev.enabled, bev.sap_company_code, bev.mode, bev.vendor_code, bev.bpl_id,
                 bev.warehouse_code, bev.ocr_code, bev.ocr_code3, bev.ocr_code5, bev.ap_drafts_enabled,
                 bev.ap_series_hint),
                (True, "JIVO_BEVERAGES", "PO_GRPO", "ORGV000245", 2, "BH-FA", dim1, "Factory", "HR", False,
                 "HR_B"),
            )
        self.assertFalse(rows["Mart"].enabled)

        oil_map = GLItemMap.objects.filter(sap_company_code="JIVO_OIL")
        items = dict(oil_map.filter(treatment="ITEM").values_list("gl_account_code", "item_code"))
        self.assertEqual(items["5630004"], "CG0000015")
        self.assertEqual(items["5650016"], "CG0000003")
        self.assertEqual(len(items), 10)
        reviewed = set(oil_map.filter(needs_review=True).values_list("gl_account_code", flat=True))
        self.assertEqual(reviewed, {"5630004", "5680012", "5100015", "5100006"})
        self.assertEqual(oil_map.filter(treatment="SERVICE").count(), 9)
        self.assertEqual(set(oil_map.filter(treatment="NEVER").values_list("gl_account_code", flat=True)),
                         {"1101015", "5630001"})

        # Item codes mean other things in Beverages: its own rows, never Oil's.
        bev_map = GLItemMap.objects.filter(sap_company_code="JIVO_BEVERAGES")
        bev_items = dict(bev_map.filter(treatment="ITEM").values_list("gl_account_code", "item_code"))
        self.assertEqual(bev_items["5650016"], "CG0000004")
        self.assertNotIn("5630004", bev_items)
        self.assertEqual(bev_map.get(gl_account_code="5630004").treatment, "SERVICE")
        self.assertEqual(len(bev_items), 6)
        self.assertEqual(bev_map.filter(treatment="SERVICE").count(), 13)

    def test_running_twice_changes_nothing(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        branch = CashBranch.objects.create(company=company, name="Oil")
        CashBranchSapRouting.objects.create(branch=branch, enabled=False, vendor_code="KEEP")
        seed.seed_routing_and_map(django_apps, None)
        seed.seed_routing_and_map(django_apps, None)
        self.assertEqual(CashBranchSapRouting.objects.get(branch=branch).vendor_code, "KEEP")
        self.assertEqual(GLItemMap.objects.filter(sap_company_code="JIVO_OIL").count(), 21)
        self.assertEqual(GLItemMap.objects.filter(sap_company_code="JIVO_BEVERAGES").count(), 21)
