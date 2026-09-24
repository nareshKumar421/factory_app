"""Column funnels and sort on the docking board list endpoint.

The board is paged, so both are the server's work: ``?f_status=DOCKED`` narrows
every page, ``?sort=vehicle`` orders the whole range before it is cut into
pages, and ``/sales-dispatch/columns/?column=status`` answers what one funnel
should offer -- counted over the whole range, narrowed by the OTHER funnels but
never by its own.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from vehicle_management.models import Transporter, Vehicle

LIST_URL = "/api/v1/gate-core/sales-dispatch/"
COLUMNS_URL = "/api/v1/gate-core/sales-dispatch/columns/"
BLANK = "—"


class SalesDispatchColumnsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="cols@example.com", password="p", full_name="Cols", employee_code="C1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        UserCompany.objects.create(
            user=self.user, company=self.other_company, role=self.role, is_default=False
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self.transporter = Transporter.objects.create(name="T")
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.a = self._docking(
            1, "HR69E9959", SalesDispatchGateOutStatus.DISPATCHED,
            dispatch_date=date(2026, 1, 3), gate_out_date=date(2026, 1, 4),
            gatepass_no="GP-1", customer="ILAHI CO.",
        )
        self.b = self._docking(
            2, "DL01LY5728", SalesDispatchGateOutStatus.DOCKED,
            dispatch_date=date(2026, 1, 1), customer="GURU RAMDAS",
        )
        self.c = self._docking(
            3, "DL01MA6176", SalesDispatchGateOutStatus.GATEPASS_PRINTED,
            dispatch_date=None, gatepass_no="GP-3", customer="HIMJYOTI TRADERS",
            company=self.other_company,
        )

    def _docking(
        self, index, vehicle_no, status, *, dispatch_date, gate_out_date=None,
        gatepass_no=None, customer="", company=None,
    ):
        company = company or self.company
        doc_entry = 30000 + index
        doc_num = f"6260905{index:02d}"
        vehicle = Vehicle.objects.create(
            vehicle_number=vehicle_no, transporter=self.transporter
        )
        plan = None
        if dispatch_date is not None:
            plan = DispatchPlan.objects.create(
                company=company, sap_invoice_doc_entry=doc_entry, sap_invoice_doc_num=doc_num,
                booking_status=DispatchPlanStatus.BOOKED, vehicle=vehicle,
                dispatch_date=dispatch_date, created_by=self.user, updated_by=self.user,
            )
        entry = VehicleEntry.objects.create(
            entry_no=f"DOCKV-C{index}", company=company, vehicle=vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"DOCK-{index}", vehicle_entry=entry, dispatch_plan=plan,
            vehicle=vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=doc_entry,
            sap_doc_num=doc_num, vehicle_no=vehicle_no, customer_name=customer,
            status=status, gate_out_date=gate_out_date, gatepass_no=gatepass_no,
            created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=company, dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=doc_entry,
            sap_doc_num=doc_num, created_by=self.user, updated_by=self.user,
        )
        return docking

    def _entry_nos(self, params):
        resp = self.client.get(LIST_URL, {"all_companies": 1, **params})
        self.assertEqual(resp.status_code, 200, resp.data)
        rows = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        return [row["entry_no"] for row in rows]

    def _values(self, column, params=None):
        resp = self.client.get(COLUMNS_URL, {"all_companies": 1, "column": column, **(params or {})})
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row["value"]: row["count"] for row in resp.data["values"]}

    # -- filters ---------------------------------------------------------

    def test_status_funnel_narrows_every_page(self):
        self.assertEqual(
            self._entry_nos({"page": 1, "page_size": 25, "f_status": "DOCKED"}),
            ["DOCK-2"],
        )

    def test_two_ticked_values_are_an_or(self):
        self.assertCountEqual(
            self._entry_nos({"page": 1, "f_status": "DOCKED|DISPATCHED"}),
            ["DOCK-1", "DOCK-2"],
        )

    def test_blank_ticks_the_rows_with_nothing_in_the_column(self):
        self.assertEqual(self._entry_nos({"page": 1, "f_gatepass": BLANK}), ["DOCK-2"])

    def test_blank_dispatch_date_is_tickable(self):
        self.assertEqual(self._entry_nos({"page": 1, "f_dispatch_date": BLANK}), ["DOCK-3"])

    def test_document_funnel_reads_the_bills(self):
        self.assertEqual(self._entry_nos({"page": 1, "f_document": "626090501"}), ["DOCK-1"])

    def test_gate_out_is_blank_until_the_truck_is_out(self):
        # DOCK-3 carries no gate-out date and has not gone out; DOCK-2 neither.
        self.assertCountEqual(
            self._entry_nos({"page": 1, "f_gate_out": BLANK}), ["DOCK-2", "DOCK-3"]
        )
        self.assertEqual(
            self._entry_nos({"page": 1, "f_gate_out": "2026-01-04"}), ["DOCK-1"]
        )

    def test_funnels_combine_as_an_and(self):
        self.assertEqual(
            self._entry_nos(
                {"page": 1, "f_status": "DOCKED|DISPATCHED", "f_vehicle": "HR69E9959"}
            ),
            ["DOCK-1"],
        )

    def test_filters_apply_to_the_unpaged_array_too(self):
        # Export reads the unpaged endpoint; it has to see the same rows.
        self.assertEqual(self._entry_nos({"f_status": "DOCKED"}), ["DOCK-2"])

    # -- sort ------------------------------------------------------------

    def test_default_sort_is_newest_planned_dispatch_first(self):
        self.assertEqual(self._entry_nos({"page": 1}), ["DOCK-1", "DOCK-2", "DOCK-3"])

    def test_undated_rows_sink_whichever_way_the_column_points(self):
        self.assertEqual(
            self._entry_nos({"page": 1, "sort": "dispatch_date"}),
            ["DOCK-2", "DOCK-1", "DOCK-3"],
        )

    def test_sort_by_vehicle(self):
        self.assertEqual(
            self._entry_nos({"page": 1, "sort": "vehicle"}),
            ["DOCK-2", "DOCK-3", "DOCK-1"],
        )
        self.assertEqual(
            self._entry_nos({"page": 1, "sort": "-vehicle"}),
            ["DOCK-1", "DOCK-3", "DOCK-2"],
        )

    def test_status_sorts_down_the_pipeline_not_the_alphabet(self):
        # DOCKED -> GATEPASS_PRINTED -> DISPATCHED, which the alphabet reverses.
        self.assertEqual(
            self._entry_nos({"page": 1, "sort": "status"}),
            ["DOCK-2", "DOCK-3", "DOCK-1"],
        )

    def test_unknown_sort_falls_back_rather_than_refusing(self):
        self.assertEqual(
            self._entry_nos({"page": 1, "sort": "nonsense"}),
            ["DOCK-1", "DOCK-2", "DOCK-3"],
        )

    def test_sort_holds_across_page_boundaries(self):
        first = self._entry_nos({"page": 1, "page_size": 2, "sort": "vehicle"})
        second = self._entry_nos({"page": 2, "page_size": 2, "sort": "vehicle"})
        self.assertEqual(first + second, ["DOCK-2", "DOCK-3", "DOCK-1"])

    # -- value lists -----------------------------------------------------

    def test_values_are_counted_over_the_whole_range_not_one_page(self):
        self.assertEqual(
            self._values("status"),
            {"DOCKED": 1, "GATEPASS_PRINTED": 1, "DISPATCHED": 1},
        )

    def test_a_column_does_not_narrow_its_own_list(self):
        # Two of the three still on offer, so a third can be added without
        # first clearing what is ticked.
        self.assertEqual(
            self._values("status", {"f_status": "DOCKED"}),
            {"DOCKED": 1, "GATEPASS_PRINTED": 1, "DISPATCHED": 1},
        )

    def test_another_columns_filter_does_narrow_the_list(self):
        self.assertEqual(self._values("vehicle", {"f_status": "DOCKED"}), {"DL01LY5728": 1})

    def test_empty_cells_gather_under_one_blank_entry(self):
        self.assertEqual(self._values("gatepass"), {"GP-1": 1, "GP-3": 1, BLANK: 1})

    def test_gate_out_counts_the_undispatched_as_blank(self):
        self.assertEqual(self._values("gate_out"), {"2026-01-04": 1, BLANK: 2})

    def test_document_values_come_from_the_bills(self):
        self.assertEqual(
            self._values("document"),
            {"626090501": 1, "626090502": 1, "626090503": 1},
        )

    def test_every_column_answers(self):
        # Half of these columns are CharField and half TextField; an expression
        # that let the two mix resolved for some and blew up on the rest, so
        # every column is asked for its list here rather than a chosen few.
        for column in (
            "entry_no", "company", "vehicle", "status", "document",
            "customer", "items", "dispatch_date", "gate_out", "gatepass",
        ):
            with self.subTest(column=column):
                self.assertTrue(self._values(column))

    def test_every_column_sorts(self):
        for column in (
            "entry_no", "company", "vehicle", "status", "document",
            "customer", "items", "dispatch_date", "gate_out", "gatepass",
        ):
            with self.subTest(column=column):
                self.assertCountEqual(
                    self._entry_nos({"page": 1, "sort": column}),
                    ["DOCK-1", "DOCK-2", "DOCK-3"],
                )
                self.assertCountEqual(
                    self._entry_nos({"page": 1, "sort": f"-{column}"}),
                    ["DOCK-1", "DOCK-2", "DOCK-3"],
                )

    def test_every_column_filters(self):
        for column in (
            "entry_no", "company", "vehicle", "status", "document",
            "customer", "items", "dispatch_date", "gate_out", "gatepass",
        ):
            with self.subTest(column=column):
                offered = self._values(column)
                for value, count in offered.items():
                    kept = self._entry_nos({"page": 1, f"f_{column}": value})
                    self.assertEqual(
                        len(kept), count, f"{column}={value!r} kept {kept}"
                    )

    def test_unknown_column_is_refused_by_name(self):
        resp = self.client.get(COLUMNS_URL, {"all_companies": 1, "column": "nope"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nope", resp.data["detail"])
