"""The Dispatch Sheet register.

What only this endpoint can get wrong: which day-window it defaults to, which
of the two sheets a row lands on, that the plan's own figures beat SAP's, and
that SAP being down leaves a readable register rather than an error page.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from driver_management.models import Driver, VehicleEntry
from gate_core.models import SalesDispatchGateOut
from grpo.models import GRPOStatus, ServiceGRPOLinePosting, ServiceGRPOPosting
from weighment.models import Weighment
from vehicle_management.models import Transporter, Vehicle, VehicleType

from .models import DispatchPlan, DispatchPlanStatus
from .services import DispatchPlansService

User = get_user_model()


class DispatchSheetAPITests(TestCase):
    URL = "/api/v1/dispatch-plans/sheet/"

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Dispatch desk")
        self.user = User.objects.create_user(
            email="sheet@example.com",
            password="testpass123",
            full_name="Sheet Keeper",
            employee_code="SHEET01",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="can_view_dispatch_sheet")
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        vehicle_type = VehicleType.objects.create(name="Truck")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR69F9627", vehicle_type=vehicle_type
        )
        self.transporter = Transporter.objects.create(
            name="Delhi Punjab", mobile_no="9872987038"
        )

    def _plan(self, doc_entry, **kwargs):
        defaults = dict(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(626030000 + doc_entry),
            dispatch_date=date(2026, 4, 1),
            booking_status=DispatchPlanStatus.DISPATCHED,
            customer_name="RK World",
            vehicle=self.vehicle,
            transporter=self.transporter,
        )
        defaults.update(kwargs)
        return DispatchPlan.objects.create(**defaults)

    def _get(self, **params):
        return self.client.get(
            self.URL, params, HTTP_COMPANY_CODE=self.company.code
        )

    def _no_sap(self):
        """SAP unreachable, so only the plan's own half of each row is filled."""
        mock = MagicMock()
        mock.side_effect = SAPConnectionError("HANA is asleep")
        return patch("dispatch_plans.views_sheet.DispatchPlansService", mock)

    def _sap(self, enrichment):
        service = MagicMock()
        service.get_sheet_enrichment.return_value = enrichment
        return patch(
            "dispatch_plans.views_sheet.DispatchPlansService",
            MagicMock(return_value=service),
        )

    # -- the window -----------------------------------------------------------

    def test_window_is_on_the_dispatch_date_not_the_invoice_date(self):
        self._plan(1, dispatch_date=date(2026, 4, 1))
        self._plan(2, dispatch_date=date(2026, 4, 9))

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-05")

        self.assertEqual(response.status_code, 200)
        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [1])

    def test_cancelled_plans_are_not_lines_of_the_register(self):
        self._plan(1)
        self._plan(2, booking_status=DispatchPlanStatus.CANCELLED)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [1])

    def test_a_cancelled_plan_can_still_be_asked_for_by_name(self):
        self._plan(2, booking_status=DispatchPlanStatus.CANCELLED)

        with self._no_sap():
            response = self._get(
                date_from="2026-04-01", date_to="2026-04-30", booking_status="CANCELLED"
            )

        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [2])

    def test_a_backwards_window_is_refused(self):
        with self._no_sap():
            response = self._get(date_from="2026-04-30", date_to="2026-04-01")
        self.assertEqual(response.status_code, 400)

    # -- the row --------------------------------------------------------------

    def test_row_carries_every_column_the_workbook_has(self):
        self._plan(
            1,
            bilty_no="12054",
            priority="High",
            kanta_weight=Decimal("11996.000"),
            freight=Decimal("2.50"),
            total_freight=Decimal("29990.00"),
            remarks="Lucky- 8130168713",
            location="EMPORIUM INDUSTRIAL PARKS",
            place_of_supply="Haryana",
        )

        with self._sap(
            {
                1: {
                    "invoice_date": "2026-03-31",
                    "card_name": "SAP says somebody else",
                    "ship_to_address": "SAP says somewhere else",
                    "state": "SAP says somewhere else",
                    "total_litres": 11996.0,
                    "total_boxes": 620.0,
                    "item_summary": "JIVO CANOLA OIL 1 LTR",
                }
            }
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["dispatch_date"], "2026-04-01")
        self.assertEqual(row["invoice_date"], "2026-03-31")
        # The plan's own figures win: the desk edits those, and a correction
        # typed there is the point of it being editable.
        self.assertEqual(row["party"], "RK World")
        self.assertEqual(row["location"], "EMPORIUM INDUSTRIAL PARKS")
        self.assertEqual(row["state"], "Haryana")
        self.assertEqual(row["invoice_no"], "626030001")
        self.assertEqual(row["bilty_no"], "12054")
        self.assertEqual(row["vehicle_no"], "HR69F9627")
        self.assertEqual(row["transport_name"], "Delhi Punjab")
        self.assertEqual(row["mobile_no"], "9872987038")
        self.assertEqual(row["litres"], 11996.0)
        self.assertEqual(row["total_boxes"], 620.0)
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["kanta_weight"], 11996.0)
        self.assertEqual(row["freight"], 2.5)
        self.assertEqual(row["total_freight"], 29990.0)
        self.assertEqual(row["remarks"], "Lucky- 8130168713")

    def test_sap_fills_only_the_cells_the_plan_left_empty(self):
        self._plan(1, customer_name="", location="", place_of_supply="")

        with self._sap(
            {
                1: {
                    "invoice_date": "2026-03-31",
                    "card_name": "CHIRAG ENTERPRISES MUMBAI",
                    "ship_to_address": "ANJUR MANKOLI ROAD, BHIWANDI",
                    "state": "MH",
                    "total_litres": 10913.0,
                    "total_boxes": 500.0,
                    "item_summary": "JIVO OLIVE OIL 5 LTR",
                }
            }
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["party"], "CHIRAG ENTERPRISES MUMBAI")
        self.assertEqual(row["location"], "ANJUR MANKOLI ROAD, BHIWANDI")
        self.assertEqual(row["state"], "MH")

    def test_register_still_reads_when_sap_is_down(self):
        self._plan(1, bilty_no="1756")

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["meta"]["sap_available"])
        row = body["data"][0]
        self.assertEqual(row["bilty_no"], "1756")
        self.assertIsNone(row["invoice_date"])
        self.assertIsNone(row["total_boxes"])

    # -- where the truck has got to -------------------------------------------

    def test_row_says_where_the_truck_has_got_to(self):
        """A plan with no gate-in and no docking has not entered yet."""
        self._plan(1)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["vehicle_stage"], "BOOKED")
        self.assertEqual(row["vehicle_stage_label"], "Booked")

    def test_a_truck_at_the_gate_reads_as_empty_in(self):
        driver = Driver.objects.create(name="Balbir", mobile_no="9812840633")
        entry = VehicleEntry.objects.create(
            company=self.company,
            vehicle=self.vehicle,
            driver=driver,
            entry_no="GATE-1",
            status="IN_PROGRESS",
        )
        self._plan(1, linked_vehicle_entry=entry)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["vehicle_stage"], "EMPTY_IN")
        self.assertEqual(row["vehicle_stage_label"], "Empty Vehicle In")

    # -- the figures the desk used to type by hand ----------------------------

    def _docked(self, plan, *, gross=None, tare=None, entry_no="DOCKV-1"):
        """A truck docked against this plan, weighed on its way out.

        Two gate entries, as a real visit has: the plan already links to the
        EMPTY one it arrived on, and this is the docking it leaves loaded on.
        The loaded weighing belongs to the second, which is the whole point.
        """
        driver = Driver.objects.create(
            name=f"Driver {entry_no}", mobile_no="9812840633"
        )
        entry = VehicleEntry.objects.create(
            company=self.company,
            vehicle=self.vehicle,
            driver=driver,
            entry_no=entry_no,
            status="COMPLETED",
            entry_type="SALES_DISPATCH",
        )
        if gross is not None or tare is not None:
            Weighment.objects.create(
                vehicle_entry=entry, gross_weight=gross, tare_weight=tare
            )
        return SalesDispatchGateOut.objects.create(
            company=self.company,
            dispatch_plan=plan,
            vehicle_entry=entry,
            vehicle=self.vehicle,
            driver=driver,
            entry_no=f"SDGO-{entry_no}",
            status="DISPATCHED",
            sap_doc_entry=plan.sap_invoice_doc_entry,
        )

    def test_kanta_weight_comes_off_the_docking_weighbridge(self):
        """NOT off the entry the plan links to.

        A truck is weighed twice: empty on the way in, loaded on the way out,
        on two different gate entries. The plan links to the first, whose
        weighment holds a tare and no gross -- on the live books that was all
        760 of them, and the column read empty for every one.
        """
        plan = self._plan(1)
        self._docked(plan, gross=Decimal("16000.000"), tare=Decimal("4004.000"))

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        # The load: 16,000 off the bridge less the 4,004 tare.
        self.assertEqual(response.json()["data"][0]["kanta_weight"], 11996.0)

    def test_the_empty_in_weighing_is_not_a_kanta_weight(self):
        """The entry the plan links to carries a tare and nothing else. Reading
        it gave a net of zero, which is how this column came to be empty."""
        driver = Driver.objects.create(name="Balbir", mobile_no="9812840633")
        empty_in = VehicleEntry.objects.create(
            company=self.company,
            vehicle=self.vehicle,
            driver=driver,
            entry_no="EVGI-1",
            status="COMPLETED",
            entry_type="EMPTY_VEHICLE",
        )
        Weighment.objects.create(vehicle_entry=empty_in, tare_weight=Decimal("4004.000"))
        self._plan(1, linked_vehicle_entry=empty_in)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertIsNone(response.json()["data"][0]["kanta_weight"])

    def test_a_weighbridge_reading_never_overrides_what_the_desk_typed(self):
        plan = self._plan(1, kanta_weight=Decimal("12000.000"))
        self._docked(plan, gross=Decimal("16000.000"), tare=Decimal("4004.000"))

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.json()["data"][0]["kanta_weight"], 12000.0)

    def test_half_a_weighing_is_not_a_kanta_weight(self):
        """One weighing done and the net is still zero, which is not a weight
        of anything -- the cell stays empty rather than reading nil."""
        plan = self._plan(1)
        self._docked(plan, tare=Decimal("4004.000"))

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertIsNone(response.json()["data"][0]["kanta_weight"])

    def test_freight_comes_from_the_grpo_posted_against_the_bill(self):
        plan = self._plan(1)
        posting = ServiceGRPOPosting.objects.create(
            dispatch_plan=plan,
            vendor_code="V001",
            status=GRPOStatus.POSTED,
        )
        ServiceGRPOLinePosting.objects.create(
            service_grpo_posting=posting,
            dispatch_plan=plan,
            service_description="Freight",
            amount=Decimal("29990.00"),
            unit_price=Decimal("2.50"),
        )

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["total_freight"], 29990.0)
        self.assertEqual(row["freight"], 2.5)

    def test_freight_that_never_reached_sap_is_not_a_cost(self):
        """A failed posting is an attempt, not carriage paid. Reporting it
        would put money on the sheet that nobody owes."""
        plan = self._plan(1)
        posting = ServiceGRPOPosting.objects.create(
            dispatch_plan=plan,
            vendor_code="V001",
            status=GRPOStatus.FAILED,
        )
        ServiceGRPOLinePosting.objects.create(
            service_grpo_posting=posting,
            dispatch_plan=plan,
            service_description="Freight",
            amount=Decimal("29990.00"),
        )

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertIsNone(response.json()["data"][0]["total_freight"])

    def _sap_freight(self, by_bilty):
        """SAP answering with what it holds for each bilty."""
        service = MagicMock()
        service.schema = "JIVO_OIL_TEST"
        service.freight_by_bilty.side_effect = lambda bilties: {
            b: by_bilty[b] for b in bilties if b in by_bilty
        }
        return patch(
            "dispatch_plans.views_sheet.FreightRateService",
            MagicMock(return_value=service),
        ), service

    def test_freight_falls_back_to_what_sap_holds_for_the_bilty(self):
        """Most carriage is entered straight into SAP, never through this app.
        The bill is still paid for, and the column still has to say so."""
        cache.clear()
        self._plan(1, bilty_no="13430")

        sap, service = self._sap_freight({"13430": 13780.0})
        with self._no_sap(), sap:
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["total_freight"], 13780.0)
        self.assertTrue(row["freight_from_sap"])

    def test_one_truck_freight_is_split_across_the_bills_it_carried(self):
        """The money is the lorry's, not the bill's. Repeating it on every
        line would total a single load five times over."""
        cache.clear()
        self._plan(1, bilty_no="13430", total_litres=Decimal("7500.000"))
        self._plan(2, bilty_no="13430", total_litres=Decimal("2500.000"))

        sap, _ = self._sap_freight({"13430": 1000.0})
        with self._no_sap(), sap:
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        shares = sorted(row["total_freight"] for row in response.json()["data"])
        self.assertEqual(shares, [250.0, 750.0])
        # The parts add back to exactly what was posted.
        self.assertEqual(sum(shares), 1000.0)

    def test_what_the_app_posted_is_not_overruled_by_sap(self):
        cache.clear()
        plan = self._plan(1, bilty_no="13430")
        posting = ServiceGRPOPosting.objects.create(
            dispatch_plan=plan, vendor_code="V001", status=GRPOStatus.POSTED
        )
        ServiceGRPOLinePosting.objects.create(
            service_grpo_posting=posting,
            dispatch_plan=plan,
            service_description="Freight",
            amount=Decimal("500.00"),
        )

        sap, service = self._sap_freight({"13430": 13780.0})
        with self._no_sap(), sap:
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["total_freight"], 500.0)
        self.assertFalse(row["freight_from_sap"])
        # Nothing was even asked of SAP: the line was already answered.
        service.freight_by_bilty.assert_not_called()

    def test_a_bilty_sap_has_never_heard_of_leaves_the_cell_empty(self):
        cache.clear()
        self._plan(1, bilty_no="NOSUCH")

        sap, _ = self._sap_freight({})
        with self._no_sap(), sap:
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertIsNone(response.json()["data"][0]["total_freight"])

    def test_sap_is_asked_about_a_bilty_once_even_when_it_has_no_freight(self):
        """Most bilties have no freight posted yet, and re-asking about every
        one of them on every refresh is the whole cost of this lookup."""
        cache.clear()
        self._plan(1, bilty_no="NOSUCH")

        sap, service = self._sap_freight({})
        with self._no_sap(), sap:
            self._get(date_from="2026-04-01", date_to="2026-04-30")
            self._get(date_from="2026-04-01", date_to="2026-04-30")

        service.freight_by_bilty.assert_called_once()

    def test_the_register_reads_when_sap_cannot_be_asked_for_freight(self):
        cache.clear()
        self._plan(1, bilty_no="13430")

        broken = MagicMock(side_effect=SAPConnectionError("HANA is asleep"))
        with self._no_sap(), patch(
            "dispatch_plans.views_sheet.FreightRateService", broken
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"][0]["total_freight"])

    # -- which company's sheet ------------------------------------------------

    def test_every_row_says_which_company_it_came_from(self):
        self._plan(1)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["company_code"], "JIVO_OIL")
        self.assertEqual(row["company_name"], "Jivo Oil")

    def test_the_register_counts_each_company_before_a_sheet_is_opened(self):
        """The page labels its three tabs off this, so it must be there even
        for a company whose rows nobody has looked at yet."""
        self._plan(1)
        self._plan(2)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.json()["meta"]["counts_by_company"], {"JIVO_OIL": 2})
        self.assertEqual(response.json()["meta"]["companies"], ["JIVO_OIL"])

    def test_all_companies_reads_every_company_the_user_belongs_to(self):
        beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        UserCompany.objects.create(
            user=self.user,
            company=beverages,
            role=UserRole.objects.create(name="Bev desk"),
            is_active=True,
        )
        self._plan(1)
        self._plan(2, company=beverages)

        with self._no_sap():
            response = self._get(
                date_from="2026-04-01", date_to="2026-04-30", all_companies="1"
            )

        body = response.json()
        self.assertEqual(
            body["meta"]["counts_by_company"], {"JIVO_OIL": 1, "JIVO_BEVERAGES": 1}
        )

    def test_without_all_companies_only_the_header_company_is_read(self):
        beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        UserCompany.objects.create(
            user=self.user,
            company=beverages,
            role=UserRole.objects.create(name="Bev desk"),
            is_active=True,
        )
        self._plan(1)
        self._plan(2, company=beverages)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.json()["meta"]["counts_by_company"], {"JIVO_OIL": 1})

    # -- who may read it ------------------------------------------------------

    def test_the_register_is_closed_to_a_user_with_no_dispatch_right(self):
        stranger = User.objects.create_user(
            email="stranger@example.com",
            password="testpass123",
            full_name="Stranger",
            employee_code="STR01",
        )
        UserCompany.objects.create(
            user=stranger,
            company=self.company,
            role=UserRole.objects.create(name="Nobody"),
            is_active=True,
        )
        self.client.force_authenticate(stranger)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.status_code, 403)


class DispatchSheetCostTests(DispatchSheetAPITests):
    """What a window costs, which is the page's load time.

    Two things decide it: how many database queries the plans take, and how
    much SAP is asked. Both must be flat in the number of lines, or a busy
    month is a slow page.
    """

    @staticmethod
    def _real_enrichment_on_a_mock() -> MagicMock:
        """A stand-in service that runs the REAL caching, over a fake reader.

        The chunk size has to be set by hand: a MagicMock hands back a mock for
        any attribute, and one used as a slice length silently reads as 1, so
        the fake would chunk every invoice into a query of its own and the test
        would be measuring the mock rather than the code.
        """
        service = MagicMock()
        service.SHEET_ENRICHMENT_CHUNK = DispatchPlansService.SHEET_ENRICHMENT_CHUNK
        service.reader.connection.schema = "JIVO_OIL_TEST"
        service.get_sheet_enrichment.side_effect = (
            lambda entries: DispatchPlansService.get_sheet_enrichment(service, entries)
        )
        return service

    def _queries_for(self, lines: int) -> int:
        DispatchPlan.objects.all().delete()
        for n in range(1, lines + 1):
            self._plan(n)
        with self._no_sap():
            with CaptureQueriesContext(connection) as captured:
                self._get(date_from="2026-04-01", date_to="2026-04-30")
        return len(captured)

    def test_the_database_cost_does_not_grow_with_the_lines(self):
        # One read first: a user's permissions are cached on the instance after
        # their first check, and counting that warm-up as a per-row cost would
        # make the numbers disagree for a reason that has nothing to do with
        # the rows.
        self._queries_for(1)

        few = self._queries_for(3)
        many = self._queries_for(40)
        self.assertEqual(
            few,
            many,
            f"{few} queries for 3 lines but {many} for 40 -- something is asking per row",
        )

    def test_sap_is_asked_about_an_invoice_once_not_once_per_load(self):
        """The register re-reads the same month all day, from every desk. A
        posted invoice's own figures do not change, so the second load must
        cost SAP nothing."""
        cache.clear()
        self._plan(1)

        service = self._real_enrichment_on_a_mock()
        service.reader.list_bills_by_doc_entries.return_value = [
            {
                "doc_entry": 1,
                "doc_date": "2026-03-31",
                "card_name": "CHIRAG ENTERPRISES MUMBAI",
                "total_litres": 10913.0,
                "total_boxes": 620.0,
            }
        ]

        with patch(
            "dispatch_plans.views_sheet.DispatchPlansService",
            MagicMock(return_value=service),
        ):
            first = self._get(date_from="2026-04-01", date_to="2026-04-30")
            second = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(first.json()["data"][0]["invoice_date"], "2026-03-31")
        self.assertEqual(second.json()["data"][0]["invoice_date"], "2026-03-31")
        # Asked once, on the first load. The second read it from the cache.
        service.reader.list_bills_by_doc_entries.assert_called_once()

    def test_a_window_asks_sap_only_about_the_invoices_it_has_not_seen(self):
        cache.clear()
        self._plan(1)
        self._plan(2)

        service = self._real_enrichment_on_a_mock()
        service.reader.list_bills_by_doc_entries.side_effect = lambda entries: [
            {"doc_entry": entry, "doc_date": "2026-03-31"} for entry in entries
        ]

        with patch(
            "dispatch_plans.views_sheet.DispatchPlansService",
            MagicMock(return_value=service),
        ):
            self._get(date_from="2026-04-01", date_to="2026-04-30")
            # A third invoice is billed and dispatched after that first load.
            self._plan(3)
            self._get(date_from="2026-04-01", date_to="2026-04-30")

        asked = [
            call.args[0] for call in service.reader.list_bills_by_doc_entries.call_args_list
        ]
        self.assertEqual(asked, [[1, 2], [3]])
