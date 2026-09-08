"""The docking scan report has to say the same thing the review screen says.

The screen and the workbook are two renderings of one figure ("2,146 / 2,199 pcs"),
and the workbook is the one that gets mailed to a customer arguing about a short
delivery. These tests pin the three ways that figure can be misread:

* a bill genuinely short, which must be called SHORT rather than rounded away;
* a packaging-material bill, which ships as the cartons themselves and so is
  never scanned -- zero scans there is correct, not a shortfall;
* a scan-optional company (Jivo Beverages), whose bills carry no scans by policy.

Plus the scope rule: a truck carrying more than one docking reports the whole
load, because that is what the screen shows.
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from barcode.models import Box, BoxStatus
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
    VehicleArrival,
)
from gate_core.services.sales_dispatch_loading import scan_box_onto_docking
from gate_core.services.sales_dispatch_scan_report import (
    STATUS_FULL,
    STATUS_NOT_SCANNED,
    STATUS_PM_DOC,
    STATUS_SCAN_OPTIONAL,
    STATUS_SHORT,
    build_scan_report_filename,
    build_scan_report_workbook,
    load_dockings,
)
from vehicle_management.models import Transporter, Vehicle

BOXED_CODE = "FG0000028"
BOXED_NAME = "POMACE OLIVE 1 LTR 16 PCS"
PIECES_PER_BOX = 16
PM_CODE = "PM0000003"
PM_NAME = "CARTON 1 LTR POMACE 16 PCS"

OPTIONAL_CODE = "JIVO_BEV_T"


def sheet_rows(ws, header_row=1):
    """Rows below a header, keyed by column label, blank trailing rows dropped."""
    headers = [cell.value for cell in ws[header_row]]
    rows = []
    for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if all(value in (None, "") for value in row):
            continue
        rows.append(dict(zip(headers, row)))
    return rows


@override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=[OPTIONAL_CODE])
class DockingScanReportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_T")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="scanreport@example.com", password="p",
            full_name="Dock Operator", employee_code="SR1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="Pick & Ship")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LAQ9317", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Shiva", mobile_no="6201041655", license_no="DL-1"
        )
        self.entry = self._docking(self.company, "DOCK-A")

    # ----- fixtures -------------------------------------------------------

    def _docking(self, company, entry_no, arrival=None, status=None):
        # (company, document_type, sap_doc_entry) is unique, so each docking in a test
        # needs its own header doc entry.
        self._doc_entry_seq = getattr(self, "_doc_entry_seq", 0) + 1
        vehicle_entry = VehicleEntry.objects.create(
            entry_no=f"VE-{entry_no}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=company, entry_no=entry_no, vehicle_entry=vehicle_entry,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            arrival=arrival, vehicle_no=self.vehicle.vehicle_number,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=self._doc_entry_seq, sap_doc_num=str(self._doc_entry_seq),
            status=status or SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )

    def _arrival(self):
        return VehicleArrival.objects.create(
            arrival_no="ARV-1", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=date(2026, 8, 8), in_time=time(18, 41),
            created_by=self.user, updated_by=self.user,
        )

    def _document(self, entry, doc_num, total_quantity, customer="RAGHAV MARKETING"):
        return SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=entry, company=entry.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=int(doc_num), sap_doc_num=doc_num,
            sap_doc_date=date(2026, 8, 6), customer_code="CUSTA000013",
            customer_name=customer, total_quantity=Decimal(total_quantity),
            created_by=self.user, updated_by=self.user,
        )

    def _item(self, entry, document, line_num, quantity, code=BOXED_CODE, name=BOXED_NAME):
        return SalesDispatchGateOutItem.objects.create(
            sales_dispatch=entry, document=document, line_num=line_num,
            item_code=code, item_name=name, quantity=Decimal(quantity), uom="PCS",
            sal_factor2=PIECES_PER_BOX, warehouse_code="BH-BT",
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, entry, document, barcode, qty=PIECES_PER_BOX, code=BOXED_CODE):
        box = Box.objects.create(
            company=entry.company, box_barcode=barcode, item_code=code,
            item_name=BOXED_NAME, batch_number="L3 000098", qty=qty,
            current_warehouse="BH-BT", mfg_date=date(2026, 7, 1),
            exp_date=date(2027, 7, 1), status=BoxStatus.ACTIVE,
        )
        return scan_box_onto_docking(entry, box, user=self.user, document_id=document.id)

    def _report(self):
        """Re-read the docking so the workbook sees committed documents and scans."""
        return build_scan_report_workbook(SalesDispatchGateOut.objects.get(pk=self.entry.pk))

    # ----- the figure -----------------------------------------------------

    def test_fully_scanned_bill_reports_no_shortfall(self):
        document = self._document(self.entry, "626080195", 32)
        self._item(self.entry, document, 0, 32)
        self._scan(self.entry, document, "BOX-1")
        self._scan(self.entry, document, "BOX-2")

        workbook = self._report()
        row = sheet_rows(workbook["Summary"], header_row=18)[0]
        self.assertEqual(row["Bill Pcs"], 32)
        self.assertEqual(row["Scanned Pcs"], 32)
        self.assertEqual(row["Boxes Scanned"], 2)
        self.assertEqual(row["Short Pcs"], 0)
        self.assertEqual(row["Scan Status"], STATUS_FULL)

    def test_short_bill_is_called_short_with_the_missing_pieces(self):
        """The 18-pcs gap that made this report worth having must survive to the sheet."""
        document = self._document(self.entry, "626080194", 48)
        self._item(self.entry, document, 0, 48)
        self._scan(self.entry, document, "BOX-1")
        self._scan(self.entry, document, "BOX-2")

        summary = sheet_rows(self._report()["Summary"], header_row=18)[0]
        self.assertEqual(summary["Short Pcs"], 16)
        self.assertEqual(summary["Scan Status"], STATUS_SHORT)
        self.assertAlmostEqual(summary["Scan %"], 32 / 48)

    def test_totals_row_sums_the_whole_load(self):
        first = self._document(self.entry, "626080195", 32)
        self._item(self.entry, first, 0, 32)
        self._scan(self.entry, first, "BOX-1")
        self._scan(self.entry, first, "BOX-2")
        second = self._document(self.entry, "626080196", 48)
        self._item(self.entry, second, 1, 48)
        self._scan(self.entry, second, "BOX-3")

        rows = sheet_rows(self._report()["Summary"], header_row=18)
        total = rows[-1]
        self.assertEqual(total["SAP Doc No"], "TOTAL")
        self.assertEqual(total["Bill Pcs"], 80)
        self.assertEqual(total["Scanned Pcs"], 48)
        self.assertEqual(total["Boxes Scanned"], 3)
        self.assertEqual(total["Short Pcs"], 32)

    # ----- the two ways zero scans are correct ----------------------------

    def test_pm_only_bill_is_exempt_not_short(self):
        document = self._document(self.entry, "626080196", 35)
        self._item(self.entry, document, 0, 35, code=PM_CODE, name=PM_NAME)

        summary = sheet_rows(self._report()["Summary"], header_row=18)[0]
        self.assertEqual(summary["Scanned Pcs"], 0)
        self.assertEqual(summary["Scan Status"], STATUS_PM_DOC)

    def test_scan_optional_company_bill_is_not_reported_as_unscanned(self):
        beverages = Company.objects.create(name="Jivo Beverages", code=OPTIONAL_CODE)
        UserCompany.objects.create(user=self.user, company=beverages, role=self.role)
        entry = self._docking(beverages, "DOCK-BEV")
        document = self._document(entry, "626098009", 27927, customer="SAFE AND SECURE")
        self._item(entry, document, 0, 27927)

        summary = sheet_rows(build_scan_report_workbook(entry)["Summary"], header_row=18)[0]
        self.assertEqual(summary["Scan Status"], STATUS_SCAN_OPTIONAL)

    def test_an_ordinary_unscanned_bill_still_says_not_scanned(self):
        document = self._document(self.entry, "626080197", 32)
        self._item(self.entry, document, 0, 32)

        summary = sheet_rows(self._report()["Summary"], header_row=18)[0]
        self.assertEqual(summary["Scan Status"], STATUS_NOT_SCANNED)

    # ----- item and scan detail -------------------------------------------

    def test_item_sheet_carries_batches_and_the_scanned_split(self):
        document = self._document(self.entry, "626080195", 48)
        self._item(self.entry, document, 0, 48)
        self._scan(self.entry, document, "BOX-1")
        self._scan(self.entry, document, "BOX-2")

        row = sheet_rows(self._report()["Item-wise Scanning"])[0]
        self.assertEqual(row["Item Code"], BOXED_CODE)
        self.assertEqual(row["Bill Qty (Pcs)"], 48)
        self.assertEqual(row["Scanned Pcs"], 32)
        self.assertEqual(row["Boxes Scanned"], 2)
        self.assertEqual(row["Short Pcs"], 16)
        self.assertEqual(row["Batch(es) Scanned"], "L3 000098")

    def test_one_item_on_two_lines_is_apportioned_not_double_counted(self):
        """Scans carry no line number, so 48 scanned pcs split 80/20 across the lines."""
        document = self._document(self.entry, "626080170", 80)
        self._item(self.entry, document, 0, 64)
        self._item(self.entry, document, 1, 16)
        for index in range(3):
            self._scan(self.entry, document, f"BOX-{index}")

        rows = sheet_rows(self._report()["Item-wise Scanning"])
        self.assertEqual([row["Scanned Pcs"] for row in rows], [38.4, 9.6])
        # Blank rather than 3 on each line, which would report six boxes for three.
        self.assertEqual([row["Boxes Scanned"] for row in rows], ["", ""])
        self.assertEqual(sum(row["Scanned Pcs"] for row in rows), 48)

    def test_box_scan_sheet_lists_every_scan_oldest_first(self):
        document = self._document(self.entry, "626080195", 48)
        self._item(self.entry, document, 0, 48)
        for index in range(3):
            self._scan(self.entry, document, f"BOX-{index}")

        rows = sheet_rows(self._report()["Box Scans"])
        self.assertEqual([row["Box Barcode"] for row in rows], ["BOX-0", "BOX-1", "BOX-2"])
        self.assertEqual([row["#"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]["Qty (Pcs)"], PIECES_PER_BOX)
        self.assertEqual(rows[0]["SAP Doc No"], "626080195")
        self.assertEqual(rows[0]["Scanned By"], "Dock Operator")

    # ----- scope ----------------------------------------------------------

    def test_a_single_docking_load_has_no_docking_column(self):
        document = self._document(self.entry, "626080195", 16)
        self._item(self.entry, document, 0, 16)

        headers = [cell.value for cell in self._report()["Item-wise Scanning"][1]]
        self.assertNotIn("Docking", headers)

    def test_a_multi_docking_truck_reports_every_docking_on_it(self):
        arrival = self._arrival()
        oil = self._docking(self.company, "DOCK-OIL", arrival=arrival)
        oil_doc = self._document(oil, "626090247", 32)
        self._item(oil, oil_doc, 0, 32)
        self._scan(oil, oil_doc, "BOX-OIL-1")

        beverages = Company.objects.create(name="Jivo Beverages", code=OPTIONAL_CODE)
        UserCompany.objects.create(user=self.user, company=beverages, role=self.role)
        bev = self._docking(beverages, "DOCK-BEV", arrival=arrival)
        bev_doc = self._document(bev, "626098009", 100, customer="SAFE AND SECURE")
        self._item(bev, bev_doc, 0, 100)

        self.assertEqual(
            {docking.entry_no for docking in load_dockings(oil)},
            {"DOCK-OIL", "DOCK-BEV"},
        )
        summary = build_scan_report_workbook(oil)["Summary"]
        rows = sheet_rows(summary, header_row=19)
        self.assertEqual([row["Docking"] for row in rows[:-1]], ["DOCK-OIL", "DOCK-BEV"])
        self.assertEqual([row["Company"] for row in rows[:-1]], ["Jivo Oil", "Jivo Beverages"])
        self.assertEqual(rows[-1]["Bill Pcs"], 132)
        self.assertEqual(rows[-1]["Scanned Pcs"], 16)

    def test_a_cancelled_docking_reports_itself_alone(self):
        """It is off the truck, so it never appears among its own siblings."""
        arrival = self._arrival()
        live = self._docking(self.company, "DOCK-LIVE", arrival=arrival)
        cancelled = self._docking(
            self.company, "DOCK-DEAD", arrival=arrival,
            status=SalesDispatchGateOutStatus.CANCELLED,
        )
        self.assertEqual(load_dockings(cancelled), [cancelled])
        self.assertEqual(load_dockings(live), [live])

    def test_filename_names_the_entry(self):
        self.assertIn("DOCK-A", build_scan_report_filename(self.entry))
        self.assertTrue(build_scan_report_filename(self.entry).endswith(".xlsx"))


@override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=[])
class DockingScanReportEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_E")
        self.role = UserRole.objects.create(name="Dock")
        self.user = get_user_model().objects.create_user(
            email="scanreport-api@example.com", password="p",
            full_name="Dock Operator", employee_code="SR2",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        transporter = Transporter.objects.create(name="Pick & Ship")
        vehicle = Vehicle.objects.create(vehicle_number="HR55X1111", transporter=transporter)
        driver = Driver.objects.create(name="Shiva", mobile_no="6201041655", license_no="DL-2")
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-API", company=self.company, vehicle=vehicle, driver=driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        self.entry = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-API", vehicle_entry=vehicle_entry,
            vehicle=vehicle, transporter=transporter, driver=driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=1,
            sap_doc_num="1", status=SalesDispatchGateOutStatus.DISPATCHED,
            created_by=self.user, updated_by=self.user,
        )
        self.url = reverse("sales_dispatch_scan_report", args=[self.entry.id])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    def _grant_view(self):
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="gate_core", codename="can_view_sales_dispatch_out"
            )
        )

    def test_download_returns_an_xlsx_attachment(self):
        self._grant_view()
        response = self.client.get(self.url, **self.company_header)

        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("DOCK-API", response["Content-Disposition"])
        # A real workbook, not an error page rendered with the wrong content type.
        self.assertTrue(response.content.startswith(b"PK"))

    def test_it_needs_the_permission_that_shows_the_screen(self):
        response = self.client.get(self.url, **self.company_header)
        self.assertEqual(response.status_code, 403)

    def test_a_docking_outside_the_users_companies_is_not_downloadable(self):
        other = Company.objects.create(name="Other Co", code="OTHER_E")
        self.entry.company = other
        self.entry.save(update_fields=["company"])

        self._grant_view()
        response = self.client.get(self.url, **self.company_header)
        self.assertEqual(response.status_code, 404)
