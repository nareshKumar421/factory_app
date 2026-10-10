"""The HTTP surface: permissions, company scoping, errors, and a full simulated post.

Run: ``python manage.py test cash_sap.tests_api --settings=config.sqlite_test_settings``
"""

from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cash_book.models import CashBranch
from cash_sap.models import ImprestBatch, ImprestGRPO, SapDocStatus
from cash_sap.support_for_tests import SIMULATE_FLAGS, CashSapFixture

BASE = "/api/v1/cash-sap"


@override_settings(**SIMULATE_FLAGS)
class CashSapAPITests(CashSapFixture):
    def setUp(self):
        super().setUp()
        self.api = APIClient()

    def as_user(self, user):
        self.api.force_authenticate(user)
        self.api.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def test_the_plan_is_readable_with_only_the_view_right(self):
        bunch = self.bunch(self.voucher())
        self.as_user(self.reader)
        response = self.api.get(f"{BASE}/bunches/{bunch.id}/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.data["can_post"])

    def test_writes_need_the_post_right(self):
        e = self.voucher()
        bunch = self.bunch(e)
        self.as_user(self.reader)
        for method, url, body in (
            ("post", f"{BASE}/bunches/{bunch.id}/post/", {}),
            ("post", f"{BASE}/bunches/{bunch.id}/send-next/", {}),
            ("post", f"{BASE}/bunches/{bunch.id}/stop/", {"reason": "x"}),
            ("post", f"{BASE}/bunches/{bunch.id}/vouchers/{e.id}/leave-out/", {"reason": "x"}),
            ("post", f"{BASE}/vouchers/{e.id}/bills/", {}),
        ):
            with self.subTest(url=url):
                response = getattr(self.api, method)(url, body, format="json")
                self.assertEqual(response.status_code, 403)

    def test_disabled_is_a_400_with_its_code(self):
        bunch = self.bunch(self.voucher())
        self.as_user(self.poster)
        with override_settings(CASH_SAP_ENABLED=False):
            response = self.api.post(f"{BASE}/bunches/{bunch.id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "CASH_SAP_DISABLED")

    def test_blocking_problems_are_a_400(self):
        bunch = self.bunch(self.voucher(bill=False))
        self.as_user(self.poster)
        response = self.api.post(f"{BASE}/bunches/{bunch.id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No bill photo", response.data["detail"])

    def test_a_full_simulated_post(self):
        bunch = self.bunch(self.voucher("350.00"), self.voucher("150.00"))
        self.as_user(self.poster)
        response = self.api.post(f"{BASE}/bunches/{bunch.id}/post/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        for _ in range(5):
            if not response.data["remaining"]:
                break
            response = self.api.post(f"{BASE}/bunches/{bunch.id}/send-next/", {}, format="json")
            self.assertEqual(response.status_code, 200, response.content)
        batch = ImprestBatch.objects.get(bunch=bunch)
        self.assertEqual(batch.po_status, SapDocStatus.POSTED)
        self.assertEqual(set(ImprestGRPO.objects.filter(batch=batch).values_list("status", flat=True)),
                         {SapDocStatus.POSTED})
        statuses = self.api.get(f"{BASE}/bunches/?ids={bunch.id}")
        self.assertEqual(statuses.data["results"][0]["state"], "SIMULATED")

    def test_the_status_list_never_builds_a_plan_per_bunch(self):
        """47 never-posted bunches against a remote database timed the browser
        out (10 Oct 2026): the list must stay a few queries for the whole page."""
        bunches = [self.bunch(self.voucher()) for _ in range(5)]
        posted = self.bunch(self.voucher("350.00"))
        self.as_user(self.poster)
        response = self.api.post(f"{BASE}/bunches/{posted.id}/post/", {}, format="json")
        for _ in range(5):
            if not response.data["remaining"]:
                break
            response = self.api.post(f"{BASE}/bunches/{posted.id}/send-next/", {}, format="json")
        no_plans = (patch("cash_sap.plan.build_plan", side_effect=AssertionError("a plan was built")),
                    patch("cash_sap.services.build_plan", side_effect=AssertionError("a plan was built")))

        def get(ids):
            with no_plans[0], no_plans[1], CaptureQueriesContext(connection) as queries:
                response = self.api.get(f"{BASE}/bunches/?ids={','.join(str(i) for i in ids)}")
            self.assertEqual(response.status_code, 200, response.content)
            return response, len(queries)

        _, one = get([bunches[0].id])
        statuses, five = get([b.id for b in bunches])
        self.assertEqual(one, five)  # the count does not grow with the bunches
        self.assertEqual({row["state"] for row in statuses.data["results"]}, {"NOT_STARTED"})
        statuses, _ = get([b.id for b in bunches] + [posted.id])
        states = {row["bunch_id"]: row["state"] for row in statuses.data["results"]}
        self.assertEqual(states[posted.id], "SIMULATED")

    def test_another_companys_bunch_is_a_404(self):
        other = self.bunch(company=self.other_company)
        self.as_user(self.poster)
        self.assertEqual(self.api.get(f"{BASE}/bunches/{other.id}/").status_code, 404)

    def test_routing_needs_the_settings_right_and_a_known_company(self):
        self.as_user(self.poster)
        url = f"{BASE}/routing/{self.oil.id}/"
        self.assertEqual(self.api.patch(url, {"enabled": False}, format="json").status_code, 403)
        self.as_user(self.settings_user)
        response = self.api.patch(url, {"sap_company_code": "NOPE"}, format="json")
        self.assertEqual(response.status_code, 400)
        response = self.api.patch(url, {"ocr_code": "SOYABEAN"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["ocr_code"], "SOYABEAN")

    def test_routing_list_includes_retired_branches(self):
        CashBranch.objects.filter(pk=self.mart.pk).update(is_active=False)
        self.as_user(self.reader)
        response = self.api.get(f"{BASE}/routing/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.mart.id, [row["branch_id"] for row in response.data])

    def test_a_goods_head_without_an_item_is_a_400(self):
        self.as_user(self.settings_user)
        response = self.api.post(f"{BASE}/gl-map/", {"sap_company_code": "JIVO_OIL", "gl_account_code": "5600001",
                                                     "treatment": "ITEM"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("item_code", response.data)

    def test_a_service_head_saves_with_a_warning_when_sap_is_down(self):
        from sap_client.exceptions import SAPConnectionError

        reader = MagicMock()
        reader.return_value.resolve.side_effect = SAPConnectionError("down")
        self.as_user(self.settings_user)
        with patch("cash_book.hana_reader.GLAccountReader", reader):
            response = self.api.post(f"{BASE}/gl-map/", {
                "sap_company_code": "JIVO_BEVERAGES", "gl_account_code": "5690002",
                "treatment": "SERVICE", "target_account_code": "B5690002"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.data["warnings"])

    def test_item_search_with_an_unknown_company_is_a_400(self):
        self.as_user(self.settings_user)
        response = self.api.get(f"{BASE}/items/?sap_company_code=NOPE&search=CG")
        self.assertEqual(response.status_code, 400)

    def test_voucher_info_is_written_by_the_custodian_with_warnings(self):
        e = self.voucher()
        self.as_user(self.custodian)
        response = self.api.patch(f"{BASE}/vouchers/{e.id}/info/",
                                  {"bill_date": "2026-09-01", "gate_entry_no": "773"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["gate_entry_no"], "773")
        self.assertIn("warnings", response.data)

    def test_options(self):
        self.as_user(self.reader)
        response = self.api.get(f"{BASE}/options/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("JIVO_OIL", response.data["sap_companies"])
        self.assertEqual(response.data["home_company"], "JIVO_OIL")
        self.assertFalse(response.data["can_post"])
