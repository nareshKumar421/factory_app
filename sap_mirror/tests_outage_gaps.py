"""What the 2026-10-06 HANA outage (18:45-20:38) found the copy did not cover.

* An Oil -> Mart invoice BST maps every Oil item to its Mart item, read live
  from Mart's items (``U_Oil_ItemCode``): no new BST could be created.
* The Plan page lists bills by dispatch date -- planning reaches back months --
  and one bill older than the copy's 30 days failed the whole page.

No HANA: live reads are patched at ``HanaConnection.connect`` to fail the way
an unreachable HANA does; the copies are taken against fakes.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi

from barcode.services.box_ownership import resolve_destination_item_code_map
from barcode.services.oitm_item_service import OitmItemReadError, OitmItemService
from company.models import Company
from dispatch_plans.hana_reader import HanaDispatchBillReader
from dispatch_plans.models import DispatchPlan, SelectedDispatchBill
from dispatch_plans.serializers import DispatchBillFilterSerializer
from dispatch_plans.services import DispatchPlansService
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError
from sap_client.hana.connection import HanaConnection

from . import services
from . import tests_bills as fixtures
from .models import MirroredBill

NOW = timezone.make_aware(datetime(2026, 10, 7, 10, 0))
HANA_DOWN = dbapi.OperationalError(-10709, "Connection failed (rc=111:Connection refused)")
MAPPING = [
    {"item_code": "MRT0000151", "oil_item_code": "FG0000151"},
    {"item_code": "MRT0000329", "oil_item_code": "FG0000329"},
]


def hana_down():
    return patch.object(HanaConnection, "connect", side_effect=HANA_DOWN)


class ItemMappingCopyTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.fetch = patch("sap_mirror.services._fetch_oil_item_mapping", return_value=MAPPING)
        self.fetched = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        only_mapping = patch.dict(
            services.DATASETS,
            {services.OIL_ITEM_MAPPING: services.DATASETS[services.OIL_ITEM_MAPPING]},
            clear=True,
        )
        only_mapping.start()
        self.addCleanup(only_mapping.stop)
        services.sync_due(NOW)

    def test_it_is_copied_for_mart_only(self):
        self.assertEqual([call.args[0] for call in self.fetched.call_args_list], ["JIVO_MART"])

    def test_both_lookups_answer_from_the_copy(self):
        mart = OitmItemService("JIVO_MART")
        with hana_down():
            self.assertEqual(mart.find_item_codes_by_oil_item_code("FG0000329"), ["MRT0000329"])
            self.assertEqual(mart.find_oil_item_code_by_mart_item_code("MRT0000151"), "FG0000151")

    def test_a_code_the_copy_does_not_map_is_still_sap_being_down(self):
        # Mapped in SAP since last night, perhaps: not "no mapping".
        mart = OitmItemService("JIVO_MART")
        with hana_down(), self.assertRaises(OitmItemReadError):
            mart.find_item_codes_by_oil_item_code("FG0099999")
        with hana_down(), self.assertRaises(OitmItemReadError):
            mart.find_oil_item_code_by_mart_item_code("MRT0099999")

    def test_an_oil_to_mart_bst_can_map_its_items_with_hana_down(self):
        with hana_down():
            mapping = resolve_destination_item_code_map(
                self.oil, self.mart, ["FG0000151", "FG0000329"]
            )

        self.assertEqual(mapping, {"FG0000151": "MRT0000151", "FG0000329": "MRT0000329"})


class PlannedBillsInTheCopyTests(fixtures.BillCopyTestCase):
    """Bills still in planning stay in the copy, whatever their age."""

    def setUp(self):
        super().setUp()
        # Invoiced in July: outside the 30 days, but still waiting to go.
        self.sap.add(7001, 626070001, "2026-07-02")

    def test_a_bill_selected_for_planning_is_copied(self):
        SelectedDispatchBill.objects.create(company=self.oil, sap_invoice_doc_entry=7001)

        self.take_copy()

        self.assertIn(7001, self.copied())

    def test_a_pending_plan_is_copied_and_leaves_once_dispatched_long_ago(self):
        plan = DispatchPlan.objects.create(
            company=self.oil, sap_invoice_doc_entry=7001, booking_status="PENDING",
        )
        self.take_copy()
        self.assertIn(7001, self.copied())

        DispatchPlan.objects.filter(pk=plan.pk).update(
            booking_status="DISPATCHED", dispatch_date=date(2026, 7, 10)
        )
        self.take_copy(fixtures.NOW + timedelta(minutes=15))

        self.assertNotIn(7001, self.copied())

    def test_a_credit_note_on_an_old_planned_bill_is_seen(self):
        SelectedDispatchBill.objects.create(company=self.oil, sap_invoice_doc_entry=7001)
        self.sap.credited = {7001}

        self.take_copy()

        self.assertTrue(MirroredBill.objects.get(doc_entry=7001).credited)


class PlanPageWithHanaDownTests(fixtures.BillCopyTestCase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        down = hana_down()
        down.start()
        self.addCleanup(down.stop)

    def plan(self, entry, dispatch_date):
        return DispatchPlan.objects.create(
            company=self.oil, sap_invoice_doc_entry=entry, booking_status="PENDING",
            dispatch_date=dispatch_date,
        )

    def test_the_plan_list_shows_what_the_copy_holds_rather_than_failing(self):
        self.plan(9001, date(2026, 9, 30))
        self.plan(9002, date(2026, 9, 30))
        # A bill the copy does not hold (cancelled in SAP, say): one bill, not
        # the page.
        self.plan(6001, date(2026, 9, 30))
        filters = DispatchBillFilterSerializer(data={
            "date_from": "2026-09-01", "date_to": "2026-09-30", "by_dispatch_date": True,
        })
        self.assertTrue(filters.is_valid(), filters.errors)

        result = DispatchPlansService(company_code="JIVO_OIL").get_bills(filters.validated_data)

        self.assertEqual(sorted(row["doc_entry"] for row in result["data"]), [9001, 9002])

    def test_a_list_taken_for_work_is_still_all_or_nothing(self):
        reader = HanaDispatchBillReader(CompanyContext("JIVO_OIL"))

        with self.assertRaises(SAPConnectionError):
            reader.list_bills_by_doc_entries([9001, 6001])
        self.assertEqual(
            [b["doc_entry"] for b in reader.list_bills_by_doc_entries([9001, 6001], partial_ok=True)],
            [9001],
        )


class WarehouseCopiesTests(TestCase):
    """The GRPO warehouse picker and the transfer letterhead, with HANA down."""

    def setUp(self):
        from sap_client.hana.warehouse_reader import HanaWarehouseReader

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        for target, value in (
            ("sap_mirror.services._fetch_warehouses", [
                {"code": "BH-PC", "name": "Panchkula FG"}, {"code": "BH-BT", "name": "Barotiwala FG"},
            ]),
            ("sap_mirror.services._fetch_warehouse_print_info", [
                {"code": HanaWarehouseReader.COMPANY_ROW, "company_name": "JIVO WELLNESS PVT LTD",
                 "company_email": "info@jivo.in"},
                {"code": "BH-PC", "name": "Panchkula FG", "gstin": "06AACCJ4223F1Z0",
                 "state_name": "HARYANA", "branch_name": "FACTORY"},
                {"code": "BH-BT", "name": "Barotiwala FG", "gstin": "02AACCJ4223F1Z4",
                 "state_name": "HIMACHAL PRADESH", "branch_name": "BAROTIWALA"},
            ]),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (services.WAREHOUSES, services.WAREHOUSE_PRINT_INFO):
            services.refresh(self.oil, name, NOW)
        self.reader = HanaWarehouseReader(CompanyContext("JIVO_OIL"))

    def test_the_active_warehouses_come_from_the_copy(self):
        with hana_down():
            warehouses = self.reader.get_active_warehouses()

        self.assertEqual([w.warehouse_code for w in warehouses], ["BH-BT", "BH-PC"])
        self.assertEqual(warehouses[1].warehouse_name, "Panchkula FG")

    def test_the_grpo_warehouse_picker_answers(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from company.models import UserCompany, UserRole

        user = get_user_model().objects.create(email="stores@example.com", full_name="Stores")
        UserCompany.objects.create(
            user=user, company=self.oil, role=UserRole.objects.create(name="Stores"), is_active=True,
        )
        api = APIClient()
        api.force_authenticate(user)
        with hana_down():
            response = api.get("/api/v1/po/warehouses/", HTTP_COMPANY_CODE="JIVO_OIL")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["warehouse_code"], "BH-BT")

    def test_the_letterhead_comes_from_the_copy(self):
        with hana_down():
            info = self.reader.get_warehouse_print_info(["BH-PC", "NOPE"])

        self.assertEqual(info["company_name"], "JIVO WELLNESS PVT LTD")
        self.assertEqual(list(info["warehouses"]), ["BH-PC"])
        self.assertEqual(info["warehouses"]["BH-PC"]["gstin"], "06AACCJ4223F1Z0")

    def test_the_item_group_filter_says_sap_is_down_not_a_server_fault(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from company.models import UserCompany, UserRole

        user = get_user_model().objects.create(email="viewer@example.com", full_name="Viewer")
        UserCompany.objects.create(
            user=user, company=self.oil, role=UserRole.objects.create(name="Viewer"), is_active=True,
        )
        api = APIClient()
        api.force_authenticate(user)
        with hana_down():
            response = api.get("/api/v1/warehouse/wms/item-groups/", HTTP_COMPANY_CODE="JIVO_OIL")

        self.assertEqual(response.status_code, 503, response.content)
