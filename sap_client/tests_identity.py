"""Tests for the SAP-identity mapping and its admin API.

The mapping is what makes a SAP approval attributable to a person rather than
to a shared credential, so the rules it enforces are the point: one SAP account
per person per company, both ways, scoped to the company being acted in, and
never carrying a password to the browser.

The HANA read behind the SAP-user picker is mocked — nothing here reaches SAP.
"""

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.db import transaction
from django.db.utils import IntegrityError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.models import SapApproverIdentity

IDENTITIES_URL = "/api/v1/sap-identity/identities/"
SAP_USERS_URL = "/api/v1/sap-identity/sap-users/"
ME_URL = "/api/v1/sap-identity/me/"

SAP_USER_ROWS = [
    {"user_code": "USER37", "user_name": "HONEY SINGH", "locked": False,
     "authorizing_templates": 3},
    {"user_code": "USER32", "user_name": "PANKAJ", "locked": False,
     "authorizing_templates": 2},
    {"user_code": "USER99", "user_name": "NOBODY", "locked": False,
     "authorizing_templates": 0},
]


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_BEVERAGES": {"USER37": "...."}})
class SapApproverIdentityModelTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_BEVERAGES", name="Beverages")
        self.other = Company.objects.create(code="JIVO_OIL", name="Oil")
        self.role = UserRole.objects.create(name="Store")
        self.honey = self._user("honey@example.com", "Honey Singh", "E-37")
        self.pankaj = self._user("pankaj@example.com", "Pankaj", "E-32")

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def test_the_sap_code_is_upper_cased_on_save(self):
        """HANA reports upper-case codes, so matching must not depend on typing."""
        identity = SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code=" user37 "
        )
        self.assertEqual(identity.sap_user_code, "USER37")

    def test_one_sap_account_per_person_per_company(self):
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        # atomic() so the broken transaction does not poison the rest of the test.
        with self.assertRaises(IntegrityError), transaction.atomic():
            SapApproverIdentity.objects.create(
                user=self.honey, company=self.company, sap_user_code="USER32"
            )

    def test_one_person_per_sap_account_per_company(self):
        """Else two people could both act as one authorizer, unattributably."""
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SapApproverIdentity.objects.create(
                user=self.pankaj, company=self.company, sap_user_code="USER37"
            )

    def test_the_same_code_may_be_reused_in_another_company(self):
        """USER37 in Oil is a different SAP account from USER37 in Beverages."""
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        SapApproverIdentity.objects.create(
            user=self.pankaj, company=self.other, sap_user_code="USER37"
        )
        self.assertEqual(SapApproverIdentity.objects.count(), 2)

    def test_password_configured_reads_the_env_map(self):
        honey = SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        pankaj = SapApproverIdentity.objects.create(
            user=self.pankaj, company=self.company, sap_user_code="USER32"
        )
        self.assertTrue(honey.password_configured)
        self.assertFalse(pankaj.password_configured)

    def test_code_for_ignores_deactivated_and_other_companies(self):
        identity = SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        self.assertEqual(
            SapApproverIdentity.code_for(self.honey, self.company), "USER37"
        )
        self.assertIsNone(SapApproverIdentity.code_for(self.honey, self.other))
        identity.is_active = False
        identity.save()
        self.assertIsNone(SapApproverIdentity.code_for(self.honey, self.company))


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_BEVERAGES": {"USER37": "...."}})
class SapIdentityAPITests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_BEVERAGES", name="Beverages")
        self.role = UserRole.objects.create(name="Store")
        self.admin = self._user("admin@example.com", "Admin", "E-ADM")
        self.admin.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="sap_client",
                codename="can_manage_sap_identities",
            )
        )
        self.honey = self._user("honey@example.com", "Honey Singh", "E-37")

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    # ---- CRUD -------------------------------------------------------------

    def test_creating_a_mapping_reports_whether_a_password_exists(self):
        response = self._client(self.admin).post(
            IDENTITIES_URL,
            {"user": self.honey.id, "sap_user_code": "user37",
             "sap_user_name": "HONEY SINGH"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["sap_user_code"], "USER37")
        self.assertTrue(response.data["password_configured"])
        # The flag, never the secret.
        self.assertNotIn("password", response.data)

    def test_the_company_comes_from_context_not_the_body(self):
        """An admin must not be able to map into a company they are not in."""
        other = Company.objects.create(code="JIVO_OIL", name="Oil")
        response = self._client(self.admin).post(
            IDENTITIES_URL,
            {"user": self.honey.id, "sap_user_code": "USER37", "company": other.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            SapApproverIdentity.objects.get(pk=response.data["id"]).company, self.company
        )

    def test_a_duplicate_user_is_refused_by_name(self):
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        response = self._client(self.admin).post(
            IDENTITIES_URL,
            {"user": self.honey.id, "sap_user_code": "USER32"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("USER37", response.data["error"])

    def test_a_taken_sap_account_is_refused_by_name(self):
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        other = self._user("x@example.com", "Someone Else", "E-X")
        response = self._client(self.admin).post(
            IDENTITIES_URL,
            {"user": other.id, "sap_user_code": "USER37"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Honey Singh", response.data["error"])

    def test_list_is_scoped_to_the_acting_company(self):
        other = Company.objects.create(code="JIVO_OIL", name="Oil")
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        SapApproverIdentity.objects.create(
            user=self.honey, company=other, sap_user_code="USER12"
        )
        response = self._client(self.admin).get(IDENTITIES_URL)
        self.assertEqual(
            [row["sap_user_code"] for row in response.data], ["USER37"]
        )

    def test_delete_removes_the_mapping(self):
        identity = SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        response = self._client(self.admin).delete(f"{IDENTITIES_URL}{identity.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(SapApproverIdentity.objects.exists())

    # ---- permissions ------------------------------------------------------

    def test_managing_needs_the_admin_permission(self):
        response = self._client(self.honey).get(IDENTITIES_URL)
        self.assertEqual(response.status_code, 403)

    def test_anyone_may_read_their_own_identity(self):
        """A screen cannot correctly disable an action it may not ask about."""
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        response = self._client(self.honey).get(ME_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sap_user_code"], "USER37")
        self.assertTrue(response.data["password_configured"])

    def test_me_answers_cleanly_when_unmapped(self):
        response = self._client(self.honey).get(ME_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["sap_user_code"])
        self.assertFalse(response.data["password_configured"])

    # ---- the SAP-user picker ---------------------------------------------

    @patch("sap_client.views_identity.SAPClient")
    def test_sap_users_carry_mapping_and_password_status(self, sap):
        sap.return_value.list_sap_users.return_value = [dict(r) for r in SAP_USER_ROWS]
        SapApproverIdentity.objects.create(
            user=self.honey, company=self.company, sap_user_code="USER37"
        )
        response = self._client(self.admin).get(SAP_USERS_URL)
        self.assertEqual(response.status_code, 200)
        by_code = {row["user_code"]: row for row in response.data}

        self.assertTrue(by_code["USER37"]["password_configured"])
        self.assertEqual(by_code["USER37"]["mapped_to"]["user_name"], "Honey Singh")
        # An authorizer nobody is mapped to, and whose password is missing: this
        # pairing is exactly what the page's worklist is for.
        self.assertFalse(by_code["USER32"]["password_configured"])
        self.assertIsNone(by_code["USER32"]["mapped_to"])
        self.assertEqual(by_code["USER99"]["authorizing_templates"], 0)

    @patch("sap_client.views_identity.SAPClient")
    def test_sap_users_never_return_a_password(self, sap):
        sap.return_value.list_sap_users.return_value = [dict(r) for r in SAP_USER_ROWS]
        response = self._client(self.admin).get(SAP_USERS_URL)
        for row in response.data:
            self.assertNotIn("password", row)
            self.assertNotIn("....", [str(value) for value in row.values()])
