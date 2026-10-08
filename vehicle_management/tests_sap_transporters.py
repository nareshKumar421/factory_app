"""A vehicle's transporter picked from SAP, or typed by hand.

    python manage.py test vehicle_management.tests_sap_transporters --settings=config.sqlite_test_settings
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.utils import timezone
from openpyxl import Workbook
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from vehicle_management import sap_transporters
from vehicle_management.models import Transporter, TransporterSAPLink, Vehicle

LIST_URL = "/api/v1/vehicle-management/transporters/sap/"
RESOLVE_URL = "/api/v1/vehicle-management/transporters/resolve/"


def vendor(code, name, group="TRANSPORTER", gstin="", frozen=False):
    return {
        "card_code": code,
        "card_name": name,
        "group": group,
        "is_transporter": group == "TRANSPORTER",
        "gstin": gstin,
        "frozen": frozen,
    }


def fake_read(vendors):
    """Stands in for the HANA read: ``vendors`` is {company code: [vendor, ...]}."""

    def _read(company_code, where="", params=()):
        rows = vendors.get(company_code, [])
        if params:
            rows = [v for v in rows if v["card_code"] == params[0]]
        elif "frozenFor" in where:
            rows = [v for v in rows if not v["frozen"]]
        return [dict(v) for v in rows]

    return _read


ABHIMAN_GSTIN = "07ACLFA8846M1ZO"
VENDORS = {
    "JIVO_OIL": [
        vendor("VENDA000515", "ECHO PLAST INDIA", group="PURCHASE"),
        vendor("VENDA001676", "ABHIMAN EXPRESS", gstin=ABHIMAN_GSTIN),
        vendor("VENDA000203", "SHAMBHU ROADWAYS VEND", group="PURCHASE"),
        vendor("VENDA000999", "OLD CARRIER", frozen=True),
        vendor("VENDA000016", "AMOD KUMAR TRANSPORT"),
    ],
    "JIVO_MART": [vendor("VENDA001019", "ABHIMAN EXPRESS", gstin=ABHIMAN_GSTIN)],
}


class SapTransporterTestCase(APITestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create_user(
            email="gate@example.com", password="password", full_name="Gate", employee_code="G001"
        )
        role = UserRole.objects.create(name="Gate User")
        for company in (self.oil, self.mart):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_default=company is self.oil, is_active=True
            )
        self.client.force_authenticate(self.user)
        self.as_company(self.oil)
        patcher = mock.patch.object(sap_transporters, "_read", side_effect=fake_read(VENDORS))
        self.read = patcher.start()
        self.addCleanup(patcher.stop)

    def as_company(self, company):
        self.client.credentials(HTTP_COMPANY_CODE=company.code)


class SapTransporterListTests(SapTransporterTestCase):
    def test_transporters_come_first_and_frozen_vendors_are_left_out(self):
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [v["card_name"] for v in response.data["results"]]
        self.assertEqual(
            names,
            ["ABHIMAN EXPRESS", "AMOD KUMAR TRANSPORT", "ECHO PLAST INDIA", "SHAMBHU ROADWAYS VEND"],
        )
        self.assertIsNone(response.data["sap_copy_as_of"])

    def test_the_company_header_decides_whose_vendors(self):
        self.as_company(self.mart)

        response = self.client.get(LIST_URL)

        self.assertEqual([v["card_code"] for v in response.data["results"]], ["VENDA001019"])

    def test_with_hana_down_the_copy_answers_and_linked_codes_count_as_transporters(self):
        transporter = Transporter.objects.create(name="Shambhu roadways")
        TransporterSAPLink.objects.create(transporter=transporter, company=self.oil, card_code="VENDA000203")
        self.read.side_effect = SAPConnectionError("down")
        taken = timezone.now()
        copy = (
            [
                {"vendor_code": "VENDA000515", "vendor_name": "ECHO PLAST INDIA"},
                {"vendor_code": "VENDA000203", "vendor_name": "SHAMBHU ROADWAYS VEND"},
            ],
            taken,
        )
        with mock.patch("sap_mirror.services.copied_rows", return_value=copy):
            response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"]
        self.assertEqual([v["card_code"] for v in rows], ["VENDA000203", "VENDA000515"])
        self.assertTrue(rows[0]["is_transporter"])
        self.assertFalse(rows[1]["is_transporter"])
        self.assertEqual(response.data["sap_copy_as_of"], taken)

    def test_with_hana_down_and_no_copy_it_says_sap_is_unavailable(self):
        self.read.side_effect = SAPConnectionError("down")
        with mock.patch("sap_mirror.services.copied_rows", return_value=None):
            response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class ResolveFromSapTests(SapTransporterTestCase):
    def resolve(self, card_code):
        return self.client.post(RESOLVE_URL, {"card_code": card_code}, format="json")

    def test_a_new_vendor_becomes_a_linked_transporter(self):
        response = self.resolve("VENDA001676")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transporter = Transporter.objects.get(pk=response.data["id"])
        self.assertEqual(transporter.name, "ABHIMAN EXPRESS")
        self.assertEqual(transporter.gstin, ABHIMAN_GSTIN)
        self.assertEqual(
            response.data["sap_links"],
            [{"company_code": "JIVO_OIL", "card_code": "VENDA001676", "card_name": "ABHIMAN EXPRESS"}],
        )

    def test_picking_it_again_gives_the_same_transporter(self):
        first = self.resolve("VENDA001676").data["id"]
        second = self.resolve("VENDA001676").data["id"]

        self.assertEqual(first, second)
        self.assertEqual(Transporter.objects.count(), 1)
        self.assertEqual(TransporterSAPLink.objects.count(), 1)

    def test_the_same_transporter_in_another_company_is_found_by_gstin(self):
        oil = self.resolve("VENDA001676").data["id"]
        self.as_company(self.mart)

        mart = self.resolve("VENDA001019").data["id"]

        self.assertEqual(oil, mart)
        self.assertEqual(
            sorted(TransporterSAPLink.objects.values_list("company__code", "card_code")),
            [("JIVO_MART", "VENDA001019"), ("JIVO_OIL", "VENDA001676")],
        )

    def test_a_transporter_already_typed_under_the_same_name_is_linked_not_duplicated(self):
        typed = Transporter.objects.create(name="Abhiman Express")

        response = self.resolve("VENDA001676")

        self.assertEqual(response.data["id"], typed.id)
        typed.refresh_from_db()
        self.assertEqual(typed.gstin, ABHIMAN_GSTIN)
        self.assertEqual(Transporter.objects.count(), 1)

    def test_a_typed_gstin_is_trusted_only_when_one_transporter_has_it(self):
        only = Transporter.objects.create(name="Abhiman (old)", gstin=ABHIMAN_GSTIN)
        self.assertEqual(self.resolve("VENDA001676").data["id"], only.id)

        TransporterSAPLink.objects.all().delete()
        Transporter.objects.create(name="Someone else", gstin=ABHIMAN_GSTIN)
        response = self.resolve("VENDA001676")

        self.assertNotIn(response.data["id"], {only.id})
        self.assertEqual(response.data["name"], "ABHIMAN EXPRESS")

    def test_of_duplicates_linked_to_one_vendor_the_busiest_is_answered(self):
        quiet = Transporter.objects.create(name="Echo plast")
        busy = Transporter.objects.create(name="ECHO PLAST INDIA ")
        for transporter in (quiet, busy):
            TransporterSAPLink.objects.create(transporter=transporter, company=self.oil, card_code="VENDA000515")
        Vehicle.objects.create(vehicle_number="HR55AB0001", transporter=busy)

        self.assertEqual(self.resolve("VENDA000515").data["id"], busy.id)

    def test_a_frozen_or_unknown_code_is_refused(self):
        for code in ("VENDA000999", "VENDA404404"):
            response = self.resolve(code)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, code)
            self.assertIn("card_code", response.data)
        self.assertFalse(Transporter.objects.exists())


class ResolveByNameTests(SapTransporterTestCase):
    def test_a_typed_name_finds_the_existing_transporter(self):
        existing = Transporter.objects.create(name="Kunal cargo movers")

        response = self.client.post(RESOLVE_URL, {"name": "  KUNAL  cargo movers "}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], existing.id)
        self.assertEqual(response.data["sap_links"], [])

    def test_a_new_name_makes_an_unlinked_transporter(self):
        response = self.client.post(RESOLVE_URL, {"name": "Priyanshi  roadways"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Transporter.objects.get().name, "Priyanshi roadways")
        self.assertFalse(TransporterSAPLink.objects.exists())

    def test_it_takes_a_code_or_a_name_but_not_both_or_neither(self):
        for body in ({}, {"name": " "}, {"card_code": "VENDA001676", "name": "Abhiman"}):
            response = self.client.post(RESOLVE_URL, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)


class LinkCommandTests(SapTransporterTestCase):
    HEADER = ["App ID", "App transporter", "Match", "Oil code", "Mart code", "Beverages code", "Your call"]

    def sheet(self, rows):
        directory = Path(tempfile.mkdtemp(prefix="transporter-map-"))
        workbook = Workbook()
        mapping = workbook.active
        mapping.title = "Mapping"
        mapping.append(self.HEADER)
        for row in rows:
            mapping.append(row)
        path = directory / "map.xlsx"
        workbook.save(path)
        return str(path)

    def run_command(self, path, *args):
        out = StringIO()
        with mock.patch.object(
            sap_transporters,
            "vendors_by_code",
            side_effect=lambda code: {v["card_code"]: v for v in VENDORS.get(code, [])},
        ):
            call_command("link_transporters_to_sap", path, *args, stdout=out)
        return out.getvalue()

    def test_sure_matches_are_linked_and_the_rest_wait_for_an_ok(self):
        Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        abhiman = Transporter.objects.create(name="Abhiman Express")
        echo = Transporter.objects.create(name="Echo plast")
        amod = Transporter.objects.create(name="Amod Kumar Tpt.", gstin="07DQFPK7671M3Z4")
        shambhu = Transporter.objects.create(name="Shambhu roadways")
        anshika = Transporter.objects.create(name="ANSHIKA LOGISTICS")
        path = self.sheet(
            [
                [abhiman.id, abhiman.name, "Same name", "VENDA001676", "VENDA001019", "", ""],
                [echo.id, echo.name, "Same name", "VENDA000515 (frozen)", "", "", "manual"],
                [amod.id, amod.name, "Possible - check", "VENDA000016", "", "", "OK"],
                [shambhu.id, shambhu.name, "Close name - check", "VENDA000203", "", "", ""],
                [anshika.id, anshika.name, "Not in SAP", "VENDA000777", "", "", "ok"],
            ]
        )

        dry = self.run_command(path)
        self.assertFalse(TransporterSAPLink.objects.exists())
        self.assertIn("VENDA000777 is not a vendor in JIVO_OIL", dry)

        self.run_command(path, "--apply")

        self.assertEqual(
            sorted(TransporterSAPLink.objects.values_list("transporter__name", "company__code", "card_code")),
            [
                ("Abhiman Express", "JIVO_MART", "VENDA001019"),
                ("Abhiman Express", "JIVO_OIL", "VENDA001676"),
                ("Amod Kumar Tpt.", "JIVO_OIL", "VENDA000016"),
            ],
        )
        abhiman.refresh_from_db()
        self.assertEqual(abhiman.gstin, ABHIMAN_GSTIN)

        again = self.run_command(path, "--apply")
        self.assertIn("already linked: 3", again)
        self.assertEqual(TransporterSAPLink.objects.count(), 3)
