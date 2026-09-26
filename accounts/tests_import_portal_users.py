"""import_portal_users — SAP Portal's users into JI (accounts/portal_users.py).

    python manage.py test accounts.tests_import_portal_users --settings=config.sqlite_test_settings

SAP is never read: the SAP user ids are translated from a file (--sap-users-file).
"""

import json
import os
import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from company.models import Company, UserCompany, UserRole
from sap_client.models import SapApproverIdentity

from . import portal_users

GROUPS = [
    "SAP Finance - Ledger Viewer", "SAP Finance - Budget Editor", "SAP Documents - Viewer with attachments",
    "SAP Approvals - Approver", "Credit Note A/R Approver", "Credit Note A/P Approver",
    "Production SAP Orders", "SAP Reports", "BOM Changes - Level 1 Approver",
    "BOM Changes - Level 2 Approver", "BOM Changes - SAP Pusher", "BOM Changes - Admin",
    "BOM Changes - Requester",
]

ROWS = [
    {"ID": 1, "USERNAME": "admin", "FULL_NAME": "System Administrator", "EMAIL": "admin@company.com",
     "ROLE": "admin", "ACTIVE": 1, "MODULES": None, "SAP_USER_ID": None, "PASSWORD": "$2a$10$hash"},
    {"ID": 7, "USERNAME": "asha", "FULL_NAME": "Asha Accounts", "EMAIL": "Asha@Jivo.example.in",
     "ROLE": "manager", "ACTIVE": 1, "MODULES": '["journal-entries","credit-notes-ar","bom","grpo"]',
     "SAP_USER_ID": 12, "PASSWORD": "$2a$10$hash"},
    {"ID": 8, "USERNAME": "ravi", "FULL_NAME": "Ravi", "EMAIL": "", "ROLE": "manager", "ACTIVE": 1,
     "MODULES": "[]", "SAP_USER_ID": None},
    {"ID": 9, "USERNAME": "neha", "FULL_NAME": "Neha SAP", "EMAIL": "neha@jivo.example.in",
     "ROLE": "sap_adder", "ACTIVE": 1, "MODULES": '["bom"]', "SAP_USER_ID": 30},
]

SAP_USERS = {"JIVO_OIL": {"12": "user12", "30": "USER30"}, "JIVO_MART": {"12": "USER12M"}}


class PortalUsersTestCase(TestCase):
    def setUp(self):
        for name in GROUPS:
            Group.objects.create(name=name)
        self.oil = Company.objects.create(code="JIVO_OIL", name="Oil")
        self.mart = Company.objects.create(code="JIVO_MART", name="Mart")
        self.tmp = tempfile.mkdtemp()
        self.users_file = self._write("users.json", ROWS)
        self.sap_file = self._write("sap.json", SAP_USERS)

    def _write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def run_import(self, *extra):
        out = StringIO()
        call_command(
            "import_portal_users", "--from-file", self.users_file, "--sap-users-file", self.sap_file,
            *extra, stdout=out,
        )
        return out.getvalue()


class ImportTests(PortalUsersTestCase):
    def test_a_dry_run_writes_nothing_and_a_write_needs_yes(self):
        before = get_user_model().objects.count()
        output = self.run_import("--dry-run")
        self.assertIn("Dry run", output)
        self.assertEqual(get_user_model().objects.count(), before)
        with self.assertRaises(CommandError):
            self.run_import()

    def test_seed_accounts_and_people_without_an_email_are_skipped(self):
        output = self.run_import("--yes")
        self.assertIn("SKIP [1] admin", output)
        self.assertIn("SKIP [8] ravi", output)
        self.assertFalse(get_user_model().objects.filter(email="admin@company.com").exists())

    def test_a_new_user_gets_groups_companies_and_sap_identities_but_no_password(self):
        self.run_import("--yes")
        asha = get_user_model().objects.get(email__iexact="asha@jivo.example.in")
        self.assertFalse(asha.has_usable_password())
        self.assertEqual(
            set(asha.groups.values_list("name", flat=True)),
            {"SAP Finance - Ledger Viewer", "Credit Note A/R Approver", "BOM Changes - Level 1 Approver"},
        )
        self.assertEqual(
            set(UserCompany.objects.filter(user=asha).values_list("company__code", flat=True)),
            {"JIVO_OIL", "JIVO_MART"},
        )
        self.assertEqual(UserCompany.objects.filter(user=asha, is_default=True).count(), 1)
        identities = dict(SapApproverIdentity.objects.filter(user=asha).values_list("company__code", "sap_user_code"))
        self.assertEqual(identities, {"JIVO_OIL": "USER12", "JIVO_MART": "USER12M"})

    def test_an_existing_user_is_matched_by_email_and_only_added_to(self):
        existing = get_user_model().objects.create_user(email="asha@jivo.example.in", full_name="Asha K", password="kept")
        other = Group.objects.create(name="Something Else")
        existing.groups.add(other)
        self.run_import("--yes")
        existing.refresh_from_db()
        self.assertEqual(existing.full_name, "Asha K")
        self.assertTrue(existing.check_password("kept"))
        self.assertIn("Something Else", existing.groups.values_list("name", flat=True))
        self.assertIn("SAP Finance - Ledger Viewer", existing.groups.values_list("name", flat=True))

    def test_rerunning_changes_nothing(self):
        self.run_import("--yes")
        counts = (get_user_model().objects.count(), UserCompany.objects.count(), SapApproverIdentity.objects.count())
        self.run_import("--yes")
        self.assertEqual(
            counts,
            (get_user_model().objects.count(), UserCompany.objects.count(), SapApproverIdentity.objects.count()),
        )

    def test_an_unrestricted_portal_user_gets_only_listed_modules_unless_asked(self):
        self.run_import("--yes")
        neha = get_user_model().objects.get(email="neha@jivo.example.in")
        self.assertEqual(set(neha.groups.values_list("name", flat=True)), {"BOM Changes - SAP Pusher"})
        self.run_import("--yes", "--grant-unrestricted")
        neha.refresh_from_db()
        held = set(neha.groups.values_list("name", flat=True))
        self.assertIn("SAP Finance - Ledger Viewer", held)
        self.assertIn("Production SAP Orders", held)

    def test_a_sap_account_already_taken_is_not_mapped_again(self):
        someone = get_user_model().objects.create_user(email="someone@jivo.example.in", full_name="S", password="x")
        SapApproverIdentity.objects.create(user=someone, company=self.oil, sap_user_code="USER12")
        output = self.run_import("--yes")
        self.assertIn("USER12 already belongs to another user", output)
        asha = get_user_model().objects.get(email__iexact="asha@jivo.example.in")
        self.assertFalse(SapApproverIdentity.objects.filter(user=asha, company=self.oil).exists())

    def test_a_group_not_set_up_yet_is_reported_not_created(self):
        Group.objects.filter(name="SAP Finance - Ledger Viewer").delete()
        output = self.run_import("--yes")
        self.assertIn("groups not set up yet: SAP Finance - Ledger Viewer", output)
        self.assertFalse(Group.objects.filter(name="SAP Finance - Ledger Viewer").exists())

    def test_companies_can_be_narrowed_and_use_the_named_role(self):
        self.run_import("--yes", "--companies", "JIVO_OIL", "--role-name", "Accounts")
        asha = get_user_model().objects.get(email__iexact="asha@jivo.example.in")
        rows = UserCompany.objects.filter(user=asha)
        self.assertEqual([r.company.code for r in rows], ["JIVO_OIL"])
        self.assertEqual(rows[0].role, UserRole.objects.get(name="Accounts"))


class MappingTests(TestCase):
    def test_modules_are_read_from_json_a_list_or_commas(self):
        self.assertIsNone(portal_users.parse_modules(None))
        self.assertEqual(portal_users.parse_modules('["bom","budget"]'), ["bom", "budget"])
        self.assertEqual(portal_users.parse_modules(["bom"]), ["bom"])
        self.assertEqual(portal_users.parse_modules("bom, budget"), ["bom", "budget"])

    def test_the_bom_group_follows_the_portal_role(self):
        for role, group in (("manager", "BOM Changes - Level 1 Approver"), ("sr_manager", "BOM Changes - Level 2 Approver"),
                            ("sap_adder", "BOM Changes - SAP Pusher"), ("admin", "BOM Changes - Admin"),
                            ("unknown", "BOM Changes - Requester")):
            self.assertEqual(portal_users.group_names_for(role, ["bom"])[0], [group])

    def test_unmapped_modules_are_explained(self):
        groups, notes = portal_users.group_names_for("manager", ["grpo", "mystery"])
        self.assertEqual(groups, [])
        self.assertTrue(any("D4" in note for note in notes))
        self.assertTrue(any("mystery" in note for note in notes))
