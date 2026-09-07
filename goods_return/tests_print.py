"""Tests for the printed Return Note.

The sheet is SAP's own layout, so the things worth testing are the ones that
would make it disagree with the copy SAP prints: the shaping of the fields it
reads, and the refusals that stop an empty or wrong sheet being printed at all.

No SAP and no HANA — `SAPClient` is mocked at the seam the service imports it.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.hana.returns_reader import HanaReturnsReader

from .models import GoodsReturn, GoodsReturnBasis, GoodsReturnStatus

User = get_user_model()

COMPANY_CODE = "TC001"
BASE = "/api/v1/goods-return/"


class ReturnPrintShapingTests(SimpleTestCase):
    """The two fields SAP does not hand over in a printable form."""

    def test_doc_time_becomes_a_clock(self):
        # ORDN.DocTime is an integer: 1104 is 11:04, not 1,104.
        self.assertEqual(HanaReturnsReader._clock(1104), "11:04")

    def test_a_morning_time_keeps_its_leading_zero(self):
        self.assertEqual(HanaReturnsReader._clock(904), "09:04")

    def test_midnight_is_not_blank(self):
        self.assertEqual(HanaReturnsReader._clock(0), "00:00")

    def test_a_missing_time_is_empty_rather_than_wrong(self):
        self.assertEqual(HanaReturnsReader._clock(None), "00:00")
        self.assertEqual(HanaReturnsReader._clock("x"), "")

    def test_the_address_block_splits_on_saps_carriage_returns(self):
        block = "VILLAGE RAHAKA  ESR SOHNA LOGISTICS PARK\rGURUGRAM-122103\rIN"
        self.assertEqual(
            HanaReturnsReader._address_lines(block),
            ["VILLAGE RAHAKA  ESR SOHNA LOGISTICS PARK", "GURUGRAM-122103", "IN"],
        )

    def test_blank_lines_in_the_address_are_dropped(self):
        self.assertEqual(
            HanaReturnsReader._address_lines("A\r\n\r\nB\r"), ["A", "B"]
        )

    def test_no_address_is_no_lines(self):
        self.assertEqual(HanaReturnsReader._address_lines(None), [])


class GoodsReturnPrintEndpointTests(APITestCase):
    """GET .../<pk>/print/ — SAP's Return Note, as data."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Print Co", code=COMPANY_CODE)
        cls.other_company = Company.objects.create(name="Other Co", code="TC002")
        role = UserRole.objects.create(name="Returns")
        cls.viewer = User.objects.create_user(
            email="gr-print@example.com",
            password="pass12345",
            full_name="GR Printer",
            employee_code="GR-PRN",
        )
        UserCompany.objects.create(
            user=cls.viewer, company=cls.company, role=role, is_active=True
        )
        # Deliberately view-only: printing a return the warehouse already posted
        # must not require the permission to post one.
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="goods_return",
                codename="can_view_goods_return",
            )
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.viewer)
        patcher = mock.patch("sap_client.client.SAPClient")
        self.addCleanup(patcher.stop)
        self.sap = patcher.start().return_value

    def _return(self, **over):
        fields = {
            "company": self.company,
            "entry_no": "GR-20260907-0001",
            "basis": GoodsReturnBasis.INVOICE,
            "status": GoodsReturnStatus.POSTED,
            "customer_code": "CUSTA000048",
            "sap_gr_doc_entry": 3738,
            "sap_gr_doc_num": "1609264514",
        }
        fields.update(over)
        return GoodsReturn.objects.create(**fields)

    def _print(self, gr):
        return self.client.get(
            f"{BASE}{gr.id}/print/", HTTP_COMPANY_CODE=COMPANY_CODE
        )

    def test_print_returns_the_sap_document(self):
        self.sap.goods_return_print.return_value = {
            "doc_num": "1609264514",
            "lines": [],
        }
        gr = self._return()

        resp = self._print(gr)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["doc_num"], "1609264514")
        # The app's own reference travels with the sheet so the two records can
        # be tied together by whoever holds the paper.
        self.assertEqual(resp.data["entry_no"], "GR-20260907-0001")
        self.assertEqual(resp.data["goods_return_id"], gr.id)
        self.sap.goods_return_print.assert_called_once_with(3738)

    def test_print_refused_before_the_return_reaches_sap(self):
        """A RECEIVED debit-note return has no document behind it to print."""
        gr = self._return(
            basis=GoodsReturnBasis.DEBIT_NOTE,
            status=GoodsReturnStatus.RECEIVED,
            sap_gr_doc_entry=None,
            sap_gr_doc_num="",
        )

        resp = self._print(gr)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("not been posted", resp.data["detail"])
        self.sap.goods_return_print.assert_not_called()

    def test_print_reports_a_document_sap_no_longer_has(self):
        self.sap.goods_return_print.return_value = None
        gr = self._return()

        resp = self._print(gr)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("1609264514", resp.data["detail"])

    def test_print_is_scoped_to_the_callers_companies(self):
        gr = self._return(company=self.other_company, entry_no="GR-20260907-0002")

        resp = self._print(gr)

        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.goods_return_print.assert_not_called()
