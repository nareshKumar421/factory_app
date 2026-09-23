"""
Tests for ``link_employee_logins``.

    python manage.py test employee_hierarchy.tests_link_logins \
        --settings=config.sqlite_test_settings

The behaviour worth pinning is not "it links people" -- it is everything it
refuses to do. A wrong link is invisible: the person applies for leave as
somebody else, and later reads somebody else's salary. So the cases below are
mostly about ambiguity being reported rather than guessed, and about an
existing link never being moved by a re-run.
"""

import csv
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase

from company.models import Company

from .constants import EmploymentStatus
from .models import Department, Designation, Employee

User = get_user_model()


class LinkEmployeeLoginsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL", is_active=True)
        cls.department = Department.objects.create(
            company=cls.company, code="PROD", name="Production"
        )
        cls.designation = Designation.objects.create(
            company=cls.company, code="OP", name="Operator", level=1
        )

    def _employee(self, code, **kwargs):
        return Employee.objects.create(
            company=self.company,
            employee_code=code,
            first_name=kwargs.pop("first_name", code),
            department=self.department,
            designation=self.designation,
            employment_status=EmploymentStatus.ACTIVE,
            **kwargs,
        )

    def _run(self, *args, **kwargs):
        out = StringIO()
        call_command("link_employee_logins", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _sheet(self, directory, rows):
        path = Path(directory) / "logins.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["employee_code", "email"])
            writer.writeheader()
            writer.writerows(rows)
        return path

    # -- the guard rails -----------------------------------------------------

    def test_refuses_to_run_with_no_matcher_selected(self):
        with self.assertRaises(CommandError):
            self._run()

    def test_dry_run_writes_nothing(self):
        employee = self._employee("JWPL0001")
        user = User.objects.create_user(email="a@x.test", password="pw", full_name="A")
        with TemporaryDirectory() as directory:
            path = self._sheet(directory, [{"employee_code": "JWPL0001", "email": "a@x.test"}])
            output = self._run("--file", str(path))
        employee.refresh_from_db()
        self.assertIsNone(employee.user_id)
        self.assertIn("Dry run", output)
        self.assertIn("to link             : 1", output)
        del user

    def test_commit_links_from_a_sheet(self):
        employee = self._employee("JWPL0001")
        user = User.objects.create_user(email="a@x.test", password="pw", full_name="A")
        with TemporaryDirectory() as directory:
            path = self._sheet(directory, [{"employee_code": "JWPL0001", "email": "a@x.test"}])
            self._run("--file", str(path), "--commit")
        employee.refresh_from_db()
        self.assertEqual(employee.user_id, user.pk)

    def test_rerun_is_a_no_op_not_a_rewrite(self):
        employee = self._employee("JWPL0001")
        User.objects.create_user(email="a@x.test", password="pw", full_name="A")
        with TemporaryDirectory() as directory:
            path = self._sheet(directory, [{"employee_code": "JWPL0001", "email": "a@x.test"}])
            self._run("--file", str(path), "--commit")
            output = self._run("--file", str(path), "--commit")
        self.assertIn("already correct     : 1", output)
        self.assertIn("to link             : 0", output)

    def test_will_not_move_a_login_that_already_belongs_to_someone(self):
        """The one-to-one's other side: this login is already another employee."""
        taken_by = self._employee("JWPL0001")
        other = self._employee("JWPL0002")
        user = User.objects.create_user(email="a@x.test", password="pw", full_name="A")
        taken_by.user = user
        taken_by.save(update_fields=["user"])

        with TemporaryDirectory() as directory:
            path = self._sheet(directory, [{"employee_code": "JWPL0002", "email": "a@x.test"}])
            output = self._run("--file", str(path), "--commit")

        other.refresh_from_db()
        taken_by.refresh_from_db()
        self.assertIsNone(other.user_id)
        self.assertEqual(taken_by.user_id, user.pk)
        self.assertIn("refused (taken)     : 1", output)

    def test_will_not_replace_an_employees_existing_different_login(self):
        employee = self._employee("JWPL0001")
        first = User.objects.create_user(email="first@x.test", password="pw", full_name="F")
        User.objects.create_user(email="second@x.test", password="pw", full_name="S")
        employee.user = first
        employee.save(update_fields=["user"])

        with TemporaryDirectory() as directory:
            path = self._sheet(
                directory, [{"employee_code": "JWPL0001", "email": "second@x.test"}]
            )
            self._run("--file", str(path), "--commit")

        employee.refresh_from_db()
        self.assertEqual(employee.user_id, first.pk, "an existing link must never be moved")

    def test_unknown_code_and_unknown_email_are_reported_not_guessed(self):
        self._employee("JWPL0001")
        User.objects.create_user(email="a@x.test", password="pw", full_name="A")
        with TemporaryDirectory() as directory:
            path = self._sheet(
                directory,
                [
                    {"employee_code": "NOPE", "email": "a@x.test"},
                    {"employee_code": "JWPL0001", "email": "nobody@x.test"},
                ],
            )
            output = self._run("--file", str(path), "--commit")
        self.assertIn("unmatched / ambiguous: 2", output)

    # -- the free matchers ---------------------------------------------------

    def test_matches_by_employee_code(self):
        employee = self._employee("JWPL0007")
        user = User.objects.create_user(
            email="g@x.test", password="pw", full_name="G", employee_code="JWPL0007"
        )
        self._run("--by-employee-code", "--commit")
        employee.refresh_from_db()
        self.assertEqual(employee.user_id, user.pk)

    def test_matches_by_email_case_insensitively(self):
        employee = self._employee("JWPL0008", email="Mixed@X.test")
        user = User.objects.create_user(email="mixed@x.test", password="pw", full_name="M")
        self._run("--by-email", "--commit")
        employee.refresh_from_db()
        self.assertEqual(employee.user_id, user.pk)

    def test_two_matchers_disagreeing_about_one_login_link_nobody(self):
        """Ambiguity must not resolve to whichever matcher ran last."""
        by_code = self._employee("JWPL0009", email="")
        by_email = self._employee("JWPL0010", email="shared@x.test")
        User.objects.create_user(
            email="shared@x.test", password="pw", full_name="S", employee_code="JWPL0009"
        )

        output = self._run("--by-employee-code", "--by-email", "--commit")

        by_code.refresh_from_db()
        by_email.refresh_from_db()
        self.assertIsNone(by_code.user_id)
        self.assertIsNone(by_email.user_id)
        self.assertIn("matched to both", output)

    def test_company_filter_restricts_the_search(self):
        other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        other_department = Department.objects.create(
            company=other_company, code="PROD", name="Production"
        )
        elsewhere = Employee.objects.create(
            company=other_company,
            employee_code="JWPL0001",
            first_name="Elsewhere",
            department=other_department,
            employment_status=EmploymentStatus.ACTIVE,
        )
        User.objects.create_user(
            email="a@x.test", password="pw", full_name="A", employee_code="JWPL0001"
        )
        here = self._employee("JWPL0001")

        self._run("--by-employee-code", "--company", "JIVO_OIL", "--commit")

        here.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertIsNotNone(here.user_id)
        self.assertIsNone(elsewhere.user_id)

    def test_unknown_company_code_is_an_error(self):
        with self.assertRaises(CommandError):
            self._run("--by-email", "--company", "NOPE")
