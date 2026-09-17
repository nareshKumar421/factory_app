"""
The two warehouse facts SAP does not hold: rated tonnage capacity and the date
stock was last physically verified.

The behaviours worth pinning down are all about absence. An unconfigured
warehouse must answer nulls rather than 404, a capacity of zero must be refused
rather than stored (it would make the warehouse read as infinitely full), and
one company's setting must never be visible to another.
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

from .models import WarehouseBoardSettings


class WarehouseBoardSettingsAPITests(TestCase):
    def setUp(self):
        self.url = reverse("warehouse-board-settings")

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="StockViewer")

        self.user = get_user_model().objects.create_user(
            email="board@example.com",
            password="testpass123",
            full_name="Board Setter",
            employee_code="BOARD01",
        )
        for company in (self.oil, self.mart):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_active=True
            )

        # The endpoint reads under the stock-dashboard right, which the board
        # already requires to show either figure.
        from django.contrib.auth.models import Permission

        self.user.user_permissions.add(
            Permission.objects.get(codename="can_view_stock_dashboard")
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.oil.code)

    def _get(self, warehouse="BH-BT"):
        return self.client.get(self.url, {"warehouse": warehouse})

    def _put(self, payload, warehouse="BH-BT"):
        return self.client.put(f"{self.url}?warehouse={warehouse}", payload, format="json")

    # ---------------------------------------------------------------- reading

    def test_unconfigured_warehouse_answers_nulls_rather_than_404(self):
        """The board must render "not configured", not an error page."""
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["capacity_tonnes"])
        self.assertIsNone(response.data["last_audit_date"])

    def test_reading_creates_the_row_once(self):
        self._get()
        self._get()

        self.assertEqual(
            WarehouseBoardSettings.objects.filter(
                company_code="JIVO_OIL", warehouse="BH-BT"
            ).count(),
            1,
        )

    def test_warehouse_is_required(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 400)

    def test_warehouse_code_is_normalised(self):
        """`bh-bt` and `BH-BT` are the same shelf."""
        self._put({"capacity_tonnes": 5360}, warehouse="bh-bt")
        response = self._get("BH-BT")

        self.assertEqual(response.data["capacity_tonnes"], 5360)

    # ---------------------------------------------------------------- writing

    def test_capacity_and_audit_date_round_trip(self):
        audited = date.today() - timedelta(days=3)
        response = self._put(
            {"capacity_tonnes": 5360.5, "last_audit_date": audited.isoformat()}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["capacity_tonnes"], 5360.5)
        self.assertEqual(response.data["last_audit_date"], audited.isoformat())

    def test_capacity_comes_back_as_a_number_not_a_decimal_string(self):
        """The board divides by this; a string would be parsed somewhere it isn't."""
        self._put({"capacity_tonnes": 5360})
        response = self._get()

        self.assertIsInstance(response.data["capacity_tonnes"], float)

    def test_zero_capacity_is_refused(self):
        """Zero would read as infinitely full. "No rated capacity" is null."""
        response = self._put({"capacity_tonnes": 0})

        self.assertEqual(response.status_code, 400)

    def test_negative_capacity_is_refused(self):
        response = self._put({"capacity_tonnes": -5})

        self.assertEqual(response.status_code, 400)

    def test_capacity_can_be_cleared_back_to_unset(self):
        self._put({"capacity_tonnes": 5360})
        response = self._put({"capacity_tonnes": None})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["capacity_tonnes"])

    def test_a_future_audit_date_is_refused(self):
        ahead = date.today() + timedelta(days=1)
        response = self._put({"last_audit_date": ahead.isoformat()})

        self.assertEqual(response.status_code, 400)

    def test_today_is_an_acceptable_audit_date(self):
        response = self._put({"last_audit_date": date.today().isoformat()})

        self.assertEqual(response.status_code, 200)

    def test_one_field_at_a_time_leaves_the_other_alone(self):
        audited = date.today() - timedelta(days=10)
        self._put({"capacity_tonnes": 5360, "last_audit_date": audited.isoformat()})

        self._put({"capacity_tonnes": 6000})
        response = self._get()

        self.assertEqual(response.data["capacity_tonnes"], 6000)
        self.assertEqual(response.data["last_audit_date"], audited.isoformat())

    def test_the_writer_is_recorded(self):
        self._put({"capacity_tonnes": 5360})
        row = WarehouseBoardSettings.objects.get(
            company_code="JIVO_OIL", warehouse="BH-BT"
        )

        self.assertEqual(row.updated_by, self.user)

    # ------------------------------------------------------------- separation

    def test_settings_do_not_leak_between_companies(self):
        """The same warehouse code in another company is a different shelf."""
        self._put({"capacity_tonnes": 5360})

        self.client.credentials(HTTP_COMPANY_CODE=self.mart.code)
        response = self._get()

        self.assertIsNone(response.data["capacity_tonnes"])

    def test_settings_do_not_leak_between_warehouses(self):
        self._put({"capacity_tonnes": 5360}, warehouse="BH-BT")
        response = self._get("BH-PF")

        self.assertIsNone(response.data["capacity_tonnes"])
