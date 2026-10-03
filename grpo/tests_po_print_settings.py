"""Which approver the printed Purchase Order names: SAP's, or one typed in here."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from raw_material_gatein.models import POReceipt
from vehicle_management.models import Vehicle, VehicleType

from .models import POApproverSource, POPrintSettings
from .po_print_settings import apply_to_payload

User = get_user_model()

SETTINGS_URL = "/api/v1/grpo/po-print-settings/"


def _sap_order():
    return {
        "doc_num": 826228032,
        "approval": {"is_approved": True, "approver": "BHUPINDER SINGH"},
        "lines": [],
    }


class POPrintSettingsTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Oil Company", code="TC001")
        cls.other_company = Company.objects.create(name="Beverage Company", code="TC002")

        role = UserRole.objects.create(name="PO Print Operator")

        def user(email, *codenames):
            u = User.objects.create_user(
                email=email, password="testpass123", full_name=email.split("@")[0],
                employee_code=email[:10],
            )
            for company in (cls.company, cls.other_company):
                UserCompany.objects.create(
                    user=u, company=company, role=role,
                    is_default=company == cls.company, is_active=True,
                )
            u.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
            return u

        cls.printer = user("printer@example.com", "can_view_pending_grpo")
        cls.manager = user(
            "manager@example.com", "can_view_pending_grpo", "can_manage_po_print_settings"
        )

        vehicle_type = VehicleType.objects.create(name="TRUCK")
        vehicle = Vehicle.objects.create(vehicle_number="HR55AB9999", vehicle_type=vehicle_type)
        driver = Driver.objects.create(
            name="Approver Driver", mobile_no="9876500099", license_no="DL990990"
        )
        entry = VehicleEntry.objects.create(
            entry_no="VE-PO-APPROVER-001",
            company=cls.company,
            vehicle=vehicle,
            driver=driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED,
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number="826228032",
            supplier_code="VENDA000758",
            supplier_name="NATIONAL POLYPLAST INDIA PVT LTD",
            sap_doc_entry=4131,
        )

    def setUp(self):
        self.client = APIClient()

    def _print(self, company_code="TC001"):
        return self.client.get(
            f"/api/v1/grpo/po-receipt/{self.po_receipt.id}/print/",
            HTTP_COMPANY_CODE=company_code,
        )

    def _patch(self, data, company_code="TC001"):
        return self.client.patch(
            SETTINGS_URL, data, format="json", HTTP_COMPANY_CODE=company_code
        )

    # -- the printed sheet -------------------------------------------------

    @patch("sap_client.client.SAPClient")
    def test_without_settings_the_approver_is_sap_s(self, sap_client):
        sap_client.return_value.po_print.return_value = _sap_order()
        self.client.force_authenticate(user=self.printer)

        response = self._print()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approval"]["approver"], "BHUPINDER SINGH")

    @patch("sap_client.client.SAPClient")
    def test_a_typed_approver_replaces_sap_s_and_keeps_the_stamp(self, sap_client):
        POPrintSettings.objects.create(
            company=self.company,
            approver_source=POApproverSource.MANUAL,
            approver_name="Vishal/Gagandeep Singh",
        )
        sap_client.return_value.po_print.return_value = _sap_order()
        self.client.force_authenticate(user=self.printer)

        response = self._print()

        self.assertEqual(
            response.data["approval"],
            {"is_approved": True, "approver": "Vishal/Gagandeep Singh"},
        )

    @patch("sap_client.client.SAPClient")
    def test_the_order_s_own_company_decides_not_the_viewer_s(self, sap_client):
        """Printed from TC002, a TC001 order still follows TC001's setting."""
        POPrintSettings.objects.create(
            company=self.other_company,
            approver_source=POApproverSource.MANUAL,
            approver_name="Somebody Else",
        )
        sap_client.return_value.po_print.return_value = _sap_order()
        self.client.force_authenticate(user=self.printer)

        response = self._print(company_code="TC002")

        self.assertEqual(response.data["approval"]["approver"], "BHUPINDER SINGH")

    @patch("sap_client.client.SAPClient")
    def test_a_saved_name_is_ignored_once_the_source_is_sap_again(self, sap_client):
        POPrintSettings.objects.create(
            company=self.company,
            approver_source=POApproverSource.SAP,
            approver_name="Vishal/Gagandeep Singh",
        )
        sap_client.return_value.po_print.return_value = _sap_order()
        self.client.force_authenticate(user=self.printer)

        self.assertEqual(self._print().data["approval"]["approver"], "BHUPINDER SINGH")

    def test_the_pm_board_s_sheet_follows_the_same_setting(self):
        """``apply_to_payload`` is what the PM requirement board's order uses."""
        POPrintSettings.objects.create(
            company=self.company,
            approver_source=POApproverSource.MANUAL,
            approver_name="Vishal/Gagandeep Singh",
        )
        order = apply_to_payload(_sap_order(), self.company)
        self.assertEqual(order["approval"]["approver"], "Vishal/Gagandeep Singh")

    # -- the settings endpoint ----------------------------------------------

    def test_reading_defaults_to_sap_and_writes_nothing(self):
        self.client.force_authenticate(user=self.printer)

        response = self.client.get(SETTINGS_URL, HTTP_COMPANY_CODE="TC001")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approver_source"], "SAP")
        self.assertEqual(response.data["approver_name"], "")
        self.assertFalse(POPrintSettings.objects.exists())

    def test_printing_alone_does_not_let_you_change_it(self):
        self.client.force_authenticate(user=self.printer)

        response = self._patch(
            {"approver_source": "MANUAL", "approver_name": "Vishal/Gagandeep Singh"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(POPrintSettings.objects.exists())

    def test_a_typed_source_needs_a_name(self):
        self.client.force_authenticate(user=self.manager)

        response = self._patch({"approver_source": "MANUAL", "approver_name": "   "})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(POPrintSettings.objects.exists())

    def test_saving_a_typed_approver(self):
        self.client.force_authenticate(user=self.manager)

        response = self._patch(
            {"approver_source": "MANUAL", "approver_name": "  Vishal/Gagandeep Singh "}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = POPrintSettings.objects.get(company=self.company)
        self.assertEqual(row.approver_source, POApproverSource.MANUAL)
        self.assertEqual(row.approver_name, "Vishal/Gagandeep Singh")
        self.assertEqual(row.updated_by, self.manager)
        self.assertEqual(response.data["updated_by_name"], "manager")
        # Only the active company's row.
        self.assertFalse(POPrintSettings.objects.filter(company=self.other_company).exists())

    def test_switching_back_to_sap_keeps_the_name_for_next_time(self):
        POPrintSettings.objects.create(
            company=self.company,
            approver_source=POApproverSource.MANUAL,
            approver_name="Vishal/Gagandeep Singh",
        )
        self.client.force_authenticate(user=self.manager)

        response = self._patch({"approver_source": "SAP"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approver_source"], "SAP")
        self.assertEqual(response.data["approver_name"], "Vishal/Gagandeep Singh")
