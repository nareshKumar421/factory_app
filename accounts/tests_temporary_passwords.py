"""Temporary passwords for users who have none (imported SAP Portal users).

    python manage.py test accounts.tests_temporary_passwords --settings=config.sqlite_test_settings
"""

import csv
import os
import stat
import tempfile
from io import StringIO

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from accounts.admin import UserAdmin
from accounts.temporary_passwords import LENGTH, issue, new_temporary_password

User = get_user_model()


def portal_user(email="imported@jivo.in"):
    """What import_portal_users creates: no usable password."""
    return User.objects.create_user(email=email, full_name="Imported Person", password=None)


class TemporaryPasswordTests(TestCase):
    def test_a_temporary_password_is_long_and_has_no_look_alikes(self):
        password = new_temporary_password()
        self.assertEqual(len(password), LENGTH)
        self.assertFalse(set(password) & set("0O1lI"))

    def test_issuing_sets_it_and_asks_for_a_change(self):
        user = portal_user()
        [(same, password)] = issue([user])
        user.refresh_from_db()
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.must_change_password)

    def test_me_and_login_say_a_change_is_due(self):
        user = portal_user()
        [(_, password)] = issue([user])
        client = APIClient()
        login = client.post("/api/v1/accounts/login/", {"email": user.email, "password": password}, format="json")
        self.assertEqual(login.status_code, 200, login.data)
        self.assertTrue(login.data["user"]["must_change_password"])
        client.force_authenticate(user)
        self.assertTrue(client.get("/api/v1/accounts/me/").data["must_change_password"])

    def test_choosing_a_password_clears_the_flag(self):
        user = portal_user()
        [(_, password)] = issue([user])
        client = APIClient()
        client.force_authenticate(user)
        response = client.post(
            "/api/v1/accounts/change-password/",
            {"old_password": password, "new_password": "a-new-password-9"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.must_change_password)
        self.assertTrue(user.check_password("a-new-password-9"))


class IssueCommandTests(TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = os.path.join(self.dir.name, "passwords.csv")

    def run_command(self, *args):
        stdout = StringIO()
        call_command("issue_temporary_passwords", *args, stdout=stdout)
        return stdout.getvalue()

    def test_a_dry_run_lists_and_changes_nothing(self):
        user = portal_user()
        output = self.run_command("--without-password", "--dry-run")
        self.assertIn(user.email, output)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        self.assertFalse(os.path.exists(self.out))

    def test_users_without_a_password_get_one_written_to_a_private_file(self):
        imported = portal_user()
        User.objects.create_user(email="has@jivo.in", full_name="Has One", password="own-password-1")
        output = self.run_command("--without-password", "--output", self.out)
        # The password is in the file, never on the terminal.
        with open(self.out, newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["email"] for row in rows], [imported.email])
        self.assertNotIn(rows[0]["temporary_password"], output)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o600)
        imported.refresh_from_db()
        self.assertTrue(imported.check_password(rows[0]["temporary_password"]))
        self.assertTrue(imported.must_change_password)

    def test_a_named_user_with_a_password_is_skipped_unless_asked(self):
        user = User.objects.create_user(email="has@jivo.in", full_name="Has One", password="own-password-1")
        output = self.run_command("--email", user.email, "--output", self.out)
        self.assertIn("skipped", output)
        user.refresh_from_db()
        self.assertTrue(user.check_password("own-password-1"))
        self.run_command("--email", user.email, "--reset-existing", "--output", os.path.join(self.dir.name, "two.csv"))
        user.refresh_from_db()
        self.assertFalse(user.check_password("own-password-1"))

    def test_it_needs_a_target_and_a_new_output_file(self):
        portal_user()
        with self.assertRaises(CommandError):
            self.run_command("--without-password")
        open(self.out, "w").close()
        with self.assertRaises(CommandError):
            self.run_command("--without-password", "--output", self.out)
        with self.assertRaises(CommandError):
            self.run_command("--output", self.out + ".new")


class AdminActionTests(TestCase):
    def test_the_admin_action_shows_the_password_once(self):
        user = portal_user()
        admin_user = User.objects.create_superuser(email="admin@jivo.in", full_name="Admin", password="x")
        request = RequestFactory().post("/admin/accounts/user/")
        request.user = admin_user
        request.session = {}
        request._messages = FallbackStorage(request)
        UserAdmin(User, AdminSite()).issue_temporary_password(request, User.objects.filter(pk=user.pk))
        [message] = [str(m) for m in request._messages]
        password = message.split("temporary password ")[1].split(" ")[0]
        user.refresh_from_db()
        self.assertTrue(user.check_password(password))
        self.assertTrue(user.must_change_password)
