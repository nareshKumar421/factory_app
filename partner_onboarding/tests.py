"""
Partner Onboarding's permission surface, its group command, and the right each
endpoint checks.

    DEBUG=False python manage.py test partner_onboarding --settings=config.sqlite_test_settings

The other test modules: ``tests_public`` (the no-login forms), ``tests_workflow``
(verify / reject / edit / list / documents), ``tests_approve`` (creating the
partner in SAP) and ``tests_import`` (the SAP Portal importers).
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework import status

from .constants import RegistrationStatus
from .families import ALL_PERMISSIONS, CUSTOMER_FAMILY, VENDOR_FAMILY
from .management.commands.setup_partner_onboarding_groups import PARTNER_ONBOARDING_GROUPS
from .testing import FakeSAP, InternalTestCase, make_customer, make_vendor

EXPECTED_RIGHTS = {
    "can_view_customer_registrations",
    "can_verify_customer_registrations",
    "can_approve_customer_registrations",
    "can_view_vendor_registrations",
    "can_verify_vendor_registrations",
    "can_approve_vendor_registrations",
}


class PermissionSurfaceTests(TestCase):
    def test_exactly_the_six_custom_rights_exist(self):
        """Every model sets ``default_permissions = ()``: no add/change/delete/view
        rows sitting beside the real rights in the group editor."""
        actual = set(
            Permission.objects.filter(content_type__app_label="partner_onboarding").values_list("codename", flat=True)
        )
        self.assertEqual(actual, EXPECTED_RIGHTS)

    def test_every_right_the_api_checks_is_declared(self):
        self.assertEqual({p.split(".", 1)[1] for p in ALL_PERMISSIONS}, EXPECTED_RIGHTS)
        for family in (CUSTOMER_FAMILY, VENDOR_FAMILY):
            for permission in family.permissions:
                self.assertTrue(permission.startswith("partner_onboarding."))


class GroupCommandTests(TestCase):
    """Every group right exists, and every right the API checks is handed out."""

    def setUp(self):
        call_command("setup_partner_onboarding_groups", stdout=StringIO())

    def test_every_group_is_created_with_its_rights(self):
        for name, codes in PARTNER_ONBOARDING_GROUPS.items():
            with self.subTest(group=name):
                held = {
                    f"partner_onboarding.{codename}"
                    for codename in Group.objects.get(name=name).permissions.values_list("codename", flat=True)
                }
                self.assertEqual(held, set(codes))

    def test_every_right_the_api_checks_is_granted_by_some_group(self):
        granted = {code for codes in PARTNER_ONBOARDING_GROUPS.values() for code in codes}
        self.assertEqual(set(ALL_PERMISSIONS) - granted, set())

    def test_a_rerun_changes_nothing_and_puts_nobody_in_a_group(self):
        before = {g.name: set(g.permissions.values_list("id", flat=True)) for g in Group.objects.all()}
        call_command("setup_partner_onboarding_groups", stdout=StringIO())
        after = {g.name: set(g.permissions.values_list("id", flat=True)) for g in Group.objects.all()}
        self.assertEqual(before, after)
        for name in PARTNER_ONBOARDING_GROUPS:
            self.assertFalse(Group.objects.get(name=name).user_set.exists())

    def test_a_missing_right_stops_it_before_any_group_changes(self):
        Group.objects.filter(name__in=PARTNER_ONBOARDING_GROUPS).delete()
        Permission.objects.filter(codename="can_approve_vendor_registrations").delete()
        with self.assertRaises(CommandError):
            call_command("setup_partner_onboarding_groups", stdout=StringIO())
        self.assertFalse(Group.objects.filter(name__in=PARTNER_ONBOARDING_GROUPS).exists())

    def test_list_prints_the_table(self):
        out = StringIO()
        call_command("setup_partner_onboarding_groups", "--list", stdout=out)
        self.assertIn("Vendor Onboarding - SAP Approver", out.getvalue())


class CompanyContextTests(InternalTestCase):
    def test_the_company_header_is_required(self):
        registration = make_customer(self.company)
        for path in ("customers/", f"customers/{registration.pk}/", "vendors/"):
            with self.subTest(path=path):
                response = self.client.get(f"/api/v1/partner-onboarding/{path}")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_company_the_user_is_not_in_is_refused(self):
        from company.models import Company

        Company.objects.create(code="JIVO_BEVERAGES", name="Jivo Beverages")
        response = self.client.get("/api/v1/partner-onboarding/customers/", HTTP_COMPANY_CODE="JIVO_BEVERAGES")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_anonymous_caller_gets_nothing_internal(self):
        self.client.logout()
        self.client.force_authenticate(None)
        response = self.client.get("/api/v1/partner-onboarding/customers/", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class RightsPerEndpointTests(InternalTestCase):
    """Each endpoint against each single right: 403 unless the right it checks
    (or, for reading, any right of that kind) is held."""

    def setUp(self):
        super().setUp()
        patcher = patch("partner_onboarding.services.workflow.SAPClient", return_value=FakeSAP())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cases(self, family, plural, make):
        pending = make(self.company)
        extra = {"bank_code": "TST"} if family == "vendor" else {}
        verified = make(self.company, status=RegistrationStatus.VERIFIED, **extra)
        document = pending.attachments.first()
        view, verify, approve = (
            f"can_view_{family}_registrations",
            f"can_verify_{family}_registrations",
            f"can_approve_{family}_registrations",
        )
        return [
            ("list", lambda: self.get(f"{plural}/"), {view, verify, approve}),
            ("detail", lambda: self.get(f"{plural}/{pending.pk}/"), {view, verify, approve}),
            (
                "document",
                lambda: self.get(f"{plural}/{pending.pk}/attachments/{document.pk}/"),
                {view, verify, approve},
            ),
            ("edit", lambda: self.patch(f"{plural}/{pending.pk}/", {"industry": "RETAIL"}), {verify}),
            ("verify", lambda: self.post(f"{plural}/{pending.pk}/verify/"), {verify}),
            ("reject", lambda: self.post(f"{plural}/{verified.pk}/reject/", {"reason": "x"}), {verify, approve}),
            ("approve", lambda: self.post(f"{plural}/{verified.pk}/approve/"), {approve}),
        ]

    def _check(self, family, plural, make):
        rights = [f"can_{action}_{family}_registrations" for action in ("view", "verify", "approve")]
        other = "vendor" if family == "customer" else "customer"
        for right in rights + [f"can_view_{other}_registrations"]:
            for name, call, allowed in self._cases(family, plural, make):
                with self.subTest(right=right, endpoint=name):
                    self.only(right)
                    code = call().status_code
                    if right in allowed:
                        self.assertNotEqual(code, status.HTTP_403_FORBIDDEN)
                    else:
                        self.assertEqual(code, status.HTTP_403_FORBIDDEN)

    def test_customer_endpoints(self):
        self._check("customer", "customers", make_customer)

    def test_vendor_endpoints(self):
        self._check("vendor", "vendors", make_vendor)
