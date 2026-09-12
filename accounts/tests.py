from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .models import User


class LastLoginStampTests(TestCase):
    """Everybody signs in through the SPA, so the JWT login has to stamp last_login.

    Without SIMPLE_JWT["UPDATE_LAST_LOGIN"] the admin's "Last Login" column reads
    "Never" for every user who has never opened the Django admin itself.
    """

    LOGIN_URL = "/api/v1/accounts/login/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="pilot@example.com",
            password="sup3r-s3cret",
            full_name="Pilot",
            employee_code="LL-1",
        )

    def test_new_user_has_never_logged_in(self):
        self.assertIsNone(self.user.last_login)

    def test_successful_login_stamps_last_login(self):
        response = self.client.post(
            self.LOGIN_URL,
            {"email": "pilot@example.com", "password": "sup3r-s3cret"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_failed_login_leaves_last_login_alone(self):
        response = self.client.post(
            self.LOGIN_URL,
            {"email": "pilot@example.com", "password": "wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

        self.user.refresh_from_db()
        self.assertIsNone(self.user.last_login)

    def test_token_refresh_does_not_restamp_last_login(self):
        """A refresh is the browser topping up a token, not somebody signing in."""
        login = self.client.post(
            self.LOGIN_URL,
            {"email": "pilot@example.com", "password": "sup3r-s3cret"},
            format="json",
        )
        self.user.refresh_from_db()
        first_stamp = self.user.last_login

        response = self.client.post(
            "/api/v1/accounts/token/refresh/",
            {"refresh": login.data["refresh"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login, first_stamp)


class OptionalEmployeeCodeTests(TestCase):
    """Not every account belongs to somebody on the payroll."""

    def test_user_can_be_created_without_a_code(self):
        user = User.objects.create_user(
            email="nocode@example.com", password="x", full_name="No Code"
        )
        user.refresh_from_db()
        self.assertIsNone(user.employee_code)

    def test_blank_code_is_stored_as_null(self):
        user = User.objects.create_user(
            email="blank@example.com", password="x", full_name="Blank", employee_code=""
        )
        user.refresh_from_db()
        self.assertIsNone(user.employee_code)

    def test_several_users_may_go_without_a_code(self):
        User.objects.create_user(email="a@example.com", password="x", full_name="A")
        User.objects.create_user(
            email="b@example.com", password="x", full_name="B", employee_code=""
        )
        self.assertEqual(User.objects.filter(employee_code__isnull=True).count(), 2)

    def test_a_real_code_is_still_unique(self):
        User.objects.create_user(
            email="one@example.com", password="x", full_name="One", employee_code="E-9"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    email="two@example.com",
                    password="x",
                    full_name="Two",
                    employee_code="E-9",
                )

    def test_createsuperuser_does_not_demand_a_code(self):
        self.assertNotIn("employee_code", User.REQUIRED_FIELDS)
        root = User.objects.create_superuser(
            email="root@example.com", password="x", full_name="Root"
        )
        self.assertIsNone(root.employee_code)
        self.assertTrue(root.is_superuser)
