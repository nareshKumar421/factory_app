"""The Purchase Order print endpoint and the shaping behind it.

The reader's SQL is exercised against HANA, not here; what these tests pin is
everything a print can get wrong without SAP noticing: which company's schema
the order is read from, how a receipt that predates ``sap_doc_entry`` still
finds its order, and the handful of places where the reader reshapes what HANA
returns — the two address glues, the FSSAI rule and the tax grouping.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from raw_material_gatein.models import POReceipt
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from sap_client.hana.po_print_reader import HanaPOPrintReader
from vehicle_management.models import Vehicle, VehicleType

User = get_user_model()


class POPrintAPITests(APITestCase):
    """GET /api/v1/grpo/po-receipt/<po_receipt_id>/print/"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Oil Company", code="TC001")
        # A second company, to prove the order is read from the receipt's own
        # schema rather than whichever company the operator is looking from.
        cls.other_company = Company.objects.create(name="Beverage Company", code="TC002")

        cls.user = User.objects.create_user(
            email="po-printer@example.com",
            password="testpass123",
            full_name="PO Print Operator",
            employee_code="EMP910",
        )
        role = UserRole.objects.create(name="PO Print Operator")
        for company in (cls.company, cls.other_company):
            UserCompany.objects.create(
                user=cls.user,
                company=company,
                role=role,
                is_default=company == cls.company,
                is_active=True,
            )
        # Only the pending-list permission, which is the weakest of the three
        # the endpoint accepts — the button sits on that screen too.
        cls.user.user_permissions.add(
            Permission.objects.get(codename="can_view_pending_grpo")
        )

        vehicle_type = VehicleType.objects.create(name="TRUCK")
        vehicle = Vehicle.objects.create(vehicle_number="HR55AB4321", vehicle_type=vehicle_type)
        driver = Driver.objects.create(
            name="PO Print Driver", mobile_no="9876500001", license_no="DL910910"
        )

        # The receipt belongs to the *other* company.
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-PO-PRINT-001",
            company=cls.other_company,
            vehicle=vehicle,
            driver=driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED,
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="826228032",
            supplier_code="VENDA000758",
            supplier_name="NATIONAL POLYPLAST INDIA PVT LTD",
            sap_doc_entry=4131,
        )
        # A receipt from before ``sap_doc_entry`` was captured.
        cls.legacy_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="726228019",
            supplier_code="VENDA000758",
            supplier_name="NATIONAL POLYPLAST INDIA PVT LTD",
            sap_doc_entry=None,
        )

    def setUp(self):
        self.client = APIClient()

    def _get(self, po_receipt_id, company_code="TC001"):
        return self.client.get(
            f"/api/v1/grpo/po-receipt/{po_receipt_id}/print/",
            HTTP_COMPANY_CODE=company_code,
        )

    def test_unauthenticated(self):
        self.assertEqual(
            self._get(self.po_receipt.id).status_code, status.HTTP_401_UNAUTHORIZED
        )

    @patch("sap_client.client.SAPClient")
    def test_reads_the_order_from_the_receipts_own_company(self, sap_client):
        """The operator is in TC001; the receipt is TC002's, and so is the read.

        An order printed against the wrong schema answers "SAP has no purchase
        order" for a document that plainly exists, which is the shape this
        guards against.
        """
        sap_client.return_value.po_print.return_value = {
            "doc_num": 826228032,
            "lines": [],
        }
        self.client.force_authenticate(user=self.user)

        response = self._get(self.po_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["doc_num"], 826228032)
        # The receipt id rides along so the sheet can be traced back.
        self.assertEqual(response.data["po_receipt_id"], self.po_receipt.id)
        sap_client.assert_called_once_with(company_code="TC002")
        sap_client.return_value.po_print.assert_called_once_with(4131)

    @patch("sap_client.client.SAPClient")
    def test_a_receipt_without_a_doc_entry_is_found_by_its_number(self, sap_client):
        """The older rows carry only the PO number, and still have a sheet."""
        sap_client.return_value.po_doc_entry_for_number.return_value = 3919
        sap_client.return_value.po_print.return_value = {"doc_num": 726228019}
        self.client.force_authenticate(user=self.user)

        response = self._get(self.legacy_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sap_client.return_value.po_doc_entry_for_number.assert_called_once_with(
            "726228019"
        )
        sap_client.return_value.po_print.assert_called_once_with(3919)

    @patch("sap_client.client.SAPClient")
    def test_a_number_sap_cannot_place_is_not_a_read(self, sap_client):
        """No DocEntry means no sheet — and no pointless print query either."""
        sap_client.return_value.po_doc_entry_for_number.return_value = None
        self.client.force_authenticate(user=self.user)

        response = self._get(self.legacy_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("726228019", response.data["detail"])
        sap_client.return_value.po_print.assert_not_called()

    @patch("sap_client.client.SAPClient")
    def test_document_missing_in_sap(self, sap_client):
        sap_client.return_value.po_print.return_value = None
        self.client.force_authenticate(user=self.user)

        response = self._get(self.po_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("826228032", response.data["detail"])

    def test_unknown_receipt(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self._get(999_999).status_code, status.HTTP_404_NOT_FOUND)

    @patch("sap_client.client.SAPClient")
    def test_sap_being_down_is_not_a_server_error(self, sap_client):
        """A HANA outage reads as SAP unavailable, not as this app breaking."""
        sap_client.return_value.po_print.side_effect = SAPConnectionError("no route")
        self.client.force_authenticate(user=self.user)

        response = self._get(self.po_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("sap_client.client.SAPClient")
    def test_a_company_sap_does_not_know_is_a_bad_request(self, sap_client):
        sap_client.side_effect = SAPValidationError("Invalid company code: TC002")
        self.client.force_authenticate(user=self.user)

        response = self._get(self.po_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("TC002", response.data["detail"])

    @patch("sap_client.client.SAPClient")
    def test_any_grpo_read_permission_will_do(self, sap_client):
        """The history permission alone is enough, as the pending one was."""
        sap_client.return_value.po_print.return_value = {"doc_num": 826228032}
        historian = User.objects.create_user(
            email="historian@example.com",
            password="testpass123",
            full_name="History Only",
            employee_code="EMP911",
        )
        UserCompany.objects.create(
            user=historian,
            company=self.company,
            role=UserRole.objects.create(name="GRPO History Only"),
            is_default=True,
            is_active=True,
        )
        historian.user_permissions.add(
            Permission.objects.get(codename="can_view_grpo_history")
        )
        self.client.force_authenticate(user=historian)

        self.assertEqual(self._get(self.po_receipt.id).status_code, status.HTTP_200_OK)

    @patch("sap_client.client.SAPClient")
    def test_no_grpo_permission_at_all_is_forbidden(self, sap_client):
        stranger = User.objects.create_user(
            email="po-stranger@example.com",
            password="testpass123",
            full_name="No Permissions",
            employee_code="EMP912",
        )
        UserCompany.objects.create(
            user=stranger,
            company=self.company,
            role=UserRole.objects.create(name="No GRPO Access At All"),
            is_default=True,
            is_active=True,
        )
        self.client.force_authenticate(user=stranger)

        response = self._get(self.po_receipt.id)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap_client.assert_not_called()


class POPrintReaderShapingTests(SimpleTestCase):
    """The reshaping the reader does on top of what HANA returns.

    Every expected string here was taken off the SAP-printed reference sheet
    (Beverages PO 826228032), not from reading the SQL back to itself.
    """

    def reader(self):
        # No connection is opened: every test here drives ``_query`` itself.
        with patch("sap_client.hana.po_print_reader.HanaConnection"):
            return HanaPOPrintReader(MagicMock())

    def test_the_vendor_address_keeps_the_gaps_sap_prints(self):
        """Empty parts stay empty, so the printed line has the same wide gaps."""
        self.assertEqual(
            HanaPOPrintReader._vendor_address(
                "HANUMAN TEMPLE  SURVEY NO. 16 HISSA NO.02",
                "",
                "VILLAGE KARAD MADHUBAN ROAD  SILVASSA",
                "",
                "ALOK CITY",
                "396240",
            ),
            "HANUMAN TEMPLE  SURVEY NO. 16 HISSA NO.02    "
            "VILLAGE KARAD MADHUBAN ROAD  SILVASSA    ALOK CITY - 396240",
        )

    def test_the_location_address_collapses_its_gaps(self):
        """The other address glue is single-spaced, and spells out India."""
        self.assertEqual(
            HanaPOPrintReader._location_address(
                "Khasra No 20//9/2 & 10/1/2",
                "Bhakharpur",
                "Ganaur",
                "Sonipat",
                "131101",
                "HR",
                "IN",
            ),
            "Khasra No 20//9/2 & 10/1/2 Bhakharpur Ganaur Sonipat 131101 HR India",
        )

    def test_a_location_outside_india_drops_the_country(self):
        self.assertEqual(
            HanaPOPrintReader._location_address("A ROAD", "", "", "DUBAI", "0000", "", "AE"),
            "A ROAD DUBAI 0000",
        )

    def test_the_lore_warehouse_has_its_own_fssai_licence(self):
        """Branch 2 splits on the warehouse; every other branch does not."""
        self.assertEqual(HanaPOPrintReader._fssai(2, "BH-PM"), "10015064000541")
        self.assertEqual(HanaPOPrintReader._fssai(2, "BH-LR"), "10824999000237")
        self.assertEqual(HanaPOPrintReader._fssai(1, "BH-LR"), "13322999001306")
        self.assertEqual(HanaPOPrintReader._fssai(3, ""), "12123999000082")
        # An unmapped branch falls back rather than printing nothing.
        self.assertEqual(HanaPOPrintReader._fssai(9, ""), "10014011001626")
        self.assertEqual(HanaPOPrintReader._fssai(None, ""), "10014011001626")

    def test_tax_rows_are_labelled_the_way_the_sheet_labels_them(self):
        """``sys_IGST`` at 18 becomes the printed "IGST@18.00 %"."""
        reader = self.reader()
        with patch.object(
            reader,
            "_query",
            return_value=[
                (-120, Decimal("18"), Decimal("333396"), "sys_IGST"),
            ],
        ):
            self.assertEqual(
                reader._tax_rows(4131),
                [{"label": "IGST@18.00 %", "amount": "333396"}],
            )

    def test_an_unnamed_tax_component_still_gets_a_label(self):
        reader = self.reader()
        with patch.object(
            reader, "_query", return_value=[(-999, Decimal("5"), Decimal("10"), "")]
        ):
            self.assertEqual(
                reader._tax_rows(4131),
                [{"label": "Tax -999@5.00 %", "amount": "10"}],
            )

    def test_the_hsn_strip_adds_the_components_of_one_rate(self):
        """An intra-state order's 9% CGST and 9% SGST print as one 18.00 row."""
        reader = self.reader()
        lines = [
            {"sno": 1, "hsn_code": "3921.90.96", "_taxable": Decimal("134700")},
        ]
        with patch.object(
            reader,
            "_query",
            return_value=[(0, Decimal("18"), Decimal("24246"))],
        ):
            self.assertEqual(
                reader._hsn_summary(3925, lines),
                [{
                    "hsn_code": "3921.90.96",
                    "taxable_value": "134700",
                    "tax_rate": "18.00",
                    "total_tax": "24246",
                }],
            )

    def test_one_hsn_at_two_rates_stays_two_rows(self):
        """Collapsing them would have to pick a rate and misstate the other."""
        reader = self.reader()
        lines = [
            {"sno": 1, "hsn_code": "1509.10.00", "_taxable": Decimal("100")},
            {"sno": 2, "hsn_code": "1509.10.00", "_taxable": Decimal("200")},
        ]
        with patch.object(
            reader,
            "_query",
            return_value=[
                (0, Decimal("5"), Decimal("5")),
                (1, Decimal("18"), Decimal("36")),
            ],
        ):
            self.assertEqual(
                [row["tax_rate"] for row in reader._hsn_summary(1, lines)],
                ["5.00", "18.00"],
            )

    def test_lines_at_the_same_hsn_and_rate_are_added(self):
        reader = self.reader()
        lines = [
            {"sno": 1, "hsn_code": "3923.90.90", "_taxable": Decimal("100")},
            {"sno": 2, "hsn_code": "3923.90.90", "_taxable": Decimal("50")},
        ]
        with patch.object(
            reader,
            "_query",
            return_value=[
                (0, Decimal("18"), Decimal("18")),
                (1, Decimal("18"), Decimal("9")),
            ],
        ):
            self.assertEqual(
                reader._hsn_summary(1, lines),
                [{
                    "hsn_code": "3923.90.90",
                    "taxable_value": "150",
                    "tax_rate": "18.00",
                    "total_tax": "27",
                }],
            )

    def test_an_order_with_no_lines_has_no_hsn_strip(self):
        """And asks HANA nothing — there is no line to group."""
        reader = self.reader()
        with patch.object(reader, "_query") as query:
            self.assertEqual(reader._hsn_summary(4131, []), [])
            query.assert_not_called()

    def test_only_a_numeric_po_number_is_looked_up(self):
        """``DocNum`` is an integer column; a blank or a label is not a lookup."""
        reader = self.reader()
        with patch.object(reader, "_query") as query:
            self.assertIsNone(reader.doc_entry_for_number(""))
            self.assertIsNone(reader.doc_entry_for_number("N/A"))
            query.assert_not_called()

        with patch.object(reader, "_query", return_value=[(4131,)]) as query:
            self.assertEqual(reader.doc_entry_for_number(" 826228032 "), 4131)
            self.assertEqual(query.call_args.args[1], (826228032,))

    def test_a_number_no_order_carries_resolves_to_nothing(self):
        reader = self.reader()
        with patch.object(reader, "_query", return_value=[]):
            self.assertIsNone(reader.doc_entry_for_number("826228032"))
