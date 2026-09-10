"""The Goods Receipt Note print endpoint and the shaping behind it.

The reader's SQL is exercised against HANA, not here; what these tests pin is
everything a print can get wrong without SAP noticing: which company's schema
the note is read from, which postings have a note at all, and the handful of
places where the reader reshapes what HANA returns.
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
from grpo.models import GRPOPosting, GRPOStatus
from raw_material_gatein.models import POReceipt
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from sap_client.hana.grpo_print_reader import HanaGRPOPrintReader
from vehicle_management.models import Vehicle, VehicleType

User = get_user_model()


class GRPOPrintAPITests(APITestCase):
    """GET /api/v1/grpo/<posting_id>/print/"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Oil Company", code="TC001")
        # A second company, to prove the note is read from the receipt's own
        # schema rather than whichever company the operator is looking from.
        cls.other_company = Company.objects.create(name="Beverage Company", code="TC002")

        cls.user = User.objects.create_user(
            email="printer@example.com",
            password="testpass123",
            full_name="Print Operator",
            employee_code="EMP900",
        )
        role = UserRole.objects.create(name="GRPO Print Operator")
        for company in (cls.company, cls.other_company):
            UserCompany.objects.create(
                user=cls.user,
                company=company,
                role=role,
                is_default=company == cls.company,
                is_active=True,
            )
        cls.user.user_permissions.add(
            Permission.objects.get(codename="can_view_grpo_history")
        )

        vehicle_type = VehicleType.objects.create(name="TRUCK")
        vehicle = Vehicle.objects.create(vehicle_number="HR55AB1234", vehicle_type=vehicle_type)
        driver = Driver.objects.create(
            name="Print Driver", mobile_no="9876500000", license_no="DL900900"
        )

        # The receipt belongs to the *other* company.
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-PRINT-001",
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
        cls.posted = GRPOPosting.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_receipt=cls.po_receipt,
            sap_doc_entry=10462,
            sap_doc_num=2026088346,
            sap_doc_total=Decimal("2185596.00"),
            status=GRPOStatus.POSTED,
        )
        cls.draft = GRPOPosting.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_receipt=cls.po_receipt,
            status=GRPOStatus.DRAFT,
        )

    def setUp(self):
        self.client = APIClient()

    def _get(self, posting_id, company_code="TC001"):
        return self.client.get(
            f"/api/v1/grpo/{posting_id}/print/", HTTP_COMPANY_CODE=company_code
        )

    def test_unauthenticated(self):
        self.assertEqual(self._get(self.posted.id).status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("sap_client.client.SAPClient")
    def test_reads_the_note_from_the_receipts_own_company(self, sap_client):
        """The operator is in TC001; the receipt is TC002's, and so is the read.

        A GRPO printed against the wrong schema answers "SAP has no goods
        receipt" for a document that plainly exists, which is the shape this
        guards against.
        """
        sap_client.return_value.grpo_print.return_value = {
            "doc_num": 2026088346,
            "lines": [],
        }
        self.client.force_authenticate(user=self.user)

        response = self._get(self.posted.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["doc_num"], 2026088346)
        # The posting id rides along so the sheet can be traced back.
        self.assertEqual(response.data["posting_id"], self.posted.id)
        sap_client.assert_called_once_with(company_code="TC002")
        sap_client.return_value.grpo_print.assert_called_once_with(10462)

    @patch("sap_client.client.SAPClient")
    def test_unposted_posting_has_nothing_to_print(self, sap_client):
        self.client.force_authenticate(user=self.user)

        response = self._get(self.draft.id)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("not been posted", response.data["detail"])
        sap_client.assert_not_called()

    @patch("sap_client.client.SAPClient")
    def test_document_missing_in_sap(self, sap_client):
        sap_client.return_value.grpo_print.return_value = None
        self.client.force_authenticate(user=self.user)

        response = self._get(self.posted.id)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("2026088346", response.data["detail"])

    def test_unknown_posting(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self._get(999_999).status_code, status.HTTP_404_NOT_FOUND)

    @patch("sap_client.client.SAPClient")
    def test_sap_being_down_is_not_a_server_error(self, sap_client):
        """A HANA outage reads as SAP unavailable, not as this app breaking."""
        sap_client.return_value.grpo_print.side_effect = SAPConnectionError("no route")
        self.client.force_authenticate(user=self.user)

        response = self._get(self.posted.id)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("sap_client.client.SAPClient")
    def test_a_company_sap_does_not_know_is_a_bad_request(self, sap_client):
        sap_client.side_effect = SAPValidationError("Invalid company code: TC002")
        self.client.force_authenticate(user=self.user)

        response = self._get(self.posted.id)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("TC002", response.data["detail"])

    @patch("sap_client.client.SAPClient")
    def test_needs_the_history_permission(self, sap_client):
        stranger = User.objects.create_user(
            email="stranger@example.com",
            password="testpass123",
            full_name="No Permissions",
            employee_code="EMP901",
        )
        UserCompany.objects.create(
            user=stranger,
            company=self.company,
            role=UserRole.objects.create(name="No GRPO Access"),
            is_default=True,
            is_active=True,
        )
        self.client.force_authenticate(user=stranger)

        response = self._get(self.posted.id)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap_client.assert_not_called()


class GRPOPrintReaderShapingTests(SimpleTestCase):
    """The reshaping the reader does on top of what HANA returns."""

    def reader(self):
        # No connection is opened: every test here drives ``_query`` itself.
        with patch("sap_client.hana.grpo_print_reader.HanaConnection"):
            return HanaGRPOPrintReader(MagicMock())

    def test_document_address_splits_on_carriage_returns(self):
        self.assertEqual(
            HanaGRPOPrintReader._address_lines(
                "VILLAGE KARAD MADHUBAN ROAD  SILVASSA\rALOK CITY-396240\rIN"
            ),
            ["VILLAGE KARAD MADHUBAN ROAD  SILVASSA", "ALOK CITY-396240"],
        )

    def test_only_a_bare_country_code_is_dropped(self):
        """A real two-letter last line is the country; a longer one is an address."""
        self.assertEqual(HanaGRPOPrintReader._address_lines("A ROAD\rSONIPAT"), ["A ROAD", "SONIPAT"])
        self.assertEqual(HanaGRPOPrintReader._address_lines("A ROAD\r12"), ["A ROAD", "12"])
        self.assertEqual(HanaGRPOPrintReader._address_lines(""), [])

    def test_tax_rows_are_labelled_the_way_the_sheet_labels_them(self):
        """``sys_IGST`` at 18 becomes the printed "IGST@18.00 %"."""
        reader = self.reader()
        with patch.object(
            reader,
            "_query",
            return_value=[
                (-100, Decimal("9"), Decimal("166698"), "sys_CGST"),
                (-110, Decimal("9"), Decimal("166698"), "sys_SGST"),
            ],
        ):
            self.assertEqual(
                reader._tax_rows(10462),
                [
                    {"label": "CGST@9.00 %", "amount": "166698"},
                    {"label": "SGST@9.00 %", "amount": "166698"},
                ],
            )

    def test_an_unnamed_tax_type_still_prints_a_row(self):
        reader = self.reader()
        with patch.object(reader, "_query", return_value=[(-999, Decimal("2"), Decimal("50"), "")]):
            self.assertEqual(
                reader._tax_rows(1), [{"label": "Tax -999@2.00 %", "amount": "50"}]
            )

    def test_totals_add_the_lines_and_take_the_document_total_as_printed(self):
        """The grand total is SAP's own figure, never a re-addition of the rows.

        Sub Total is the lines; Grand Total is ``DocTotal``, because that is
        what SAP printed and what the vendor's copy says.
        """
        reader = self.reader()
        header = {
            "_doc_total": Decimal("2185596"),
            "_disc_sum": Decimal("0"),
            "_round_dif": Decimal("0"),
            "_expenses": Decimal("0"),
        }
        lines = [
            {"_quantity": Decimal("630000"), "_amount": Decimal("1852200")},
            {"_quantity": Decimal("10"), "_amount": Decimal("100")},
        ]
        with patch.object(reader, "_tax_rows", return_value=[]), patch.object(
            reader, "_expense_label", return_value=""
        ):
            totals = reader._totals(10462, header, lines)

        self.assertEqual(totals["total_qty"], "630010")
        self.assertEqual(totals["sub_total"], "1852300")
        self.assertEqual(totals["grand_total"], "2185596")
        # SAP prints the charges row even with nothing in it.
        self.assertEqual(totals["expenses"], {"label": "", "amount": "0"})
        # ...but the round-off row only when it rounded something.
        self.assertIsNone(totals["round_off"])

    def test_a_rounded_document_prints_its_round_off_row(self):
        reader = self.reader()
        header = {
            "_doc_total": Decimal("100"),
            "_disc_sum": Decimal("0"),
            "_round_dif": Decimal("0.40"),
            "_expenses": Decimal("0"),
        }
        with patch.object(reader, "_tax_rows", return_value=[]), patch.object(
            reader, "_expense_label", return_value=""
        ):
            totals = reader._totals(1, header, [])

        self.assertEqual(totals["round_off"], {"label": "Short & Excess", "amount": "0.40"})
