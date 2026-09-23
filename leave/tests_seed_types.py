"""
Tests for ``seed_leave_types``.

    python manage.py test leave.tests_seed_types --settings=config.sqlite_test_settings

Unlike the demo seeder this one is safe to run against a live database, so the
behaviour that matters is what it *refuses* to change: a quota tuned for one
plant must survive a re-run. An import that quietly reset entitlements back to
the defaults would be discovered in November, by somebody who has run out of
leave they thought they had.
"""

from io import StringIO

from django.core.management import CommandError, call_command

from company.models import Company

from .constants import RecordStatus
from .management.commands.seed_leave_types import STANDARD_TYPES
from .models import LeaveType
from .tests_base import LeaveTestBase


class SeedLeaveTypesTests(LeaveTestBase):
    def _run(self, *args):
        out = StringIO()
        call_command("seed_leave_types", *args, stdout=out, stderr=out)
        return out.getvalue()

    def _codes(self, company):
        return set(
            LeaveType.objects.filter(company=company).values_list("code", flat=True)
        )

    # -- the guard rails -----------------------------------------------------

    def test_a_target_is_required(self):
        with self.assertRaises(CommandError):
            self._run("--commit")

    def test_unknown_company_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--company", "NOPE", "--commit")

    def test_dry_run_writes_nothing(self):
        before = self._codes(self.other_company)
        output = self._run("--company", "JIVO_MART")
        self.assertIn("Dry run", output)
        self.assertEqual(self._codes(self.other_company), before)

    # -- creating ------------------------------------------------------------

    def test_a_company_with_nothing_gets_the_full_set(self):
        self._run("--company", "JIVO_MART", "--commit")
        codes = self._codes(self.other_company)
        self.assertEqual(codes, {row[0] for row in STANDARD_TYPES})

    def test_every_seeded_type_is_active_and_described(self):
        self._run("--company", "JIVO_MART", "--commit")
        for leave_type in LeaveType.objects.filter(company=self.other_company):
            self.assertEqual(leave_type.status, RecordStatus.ACTIVE)
            self.assertTrue(leave_type.description, f"{leave_type.code} has no description")

    def test_all_companies_covers_every_active_one(self):
        self._run("--all-companies", "--commit")
        for company in Company.objects.filter(is_active=True):
            self.assertTrue(
                self._codes(company), f"{company.code} was left with no leave types"
            )

    def test_an_inactive_company_is_skipped(self):
        dormant = Company.objects.create(name="Dormant", code="DORMANT", is_active=False)
        self._run("--all-companies", "--commit")
        self.assertEqual(self._codes(dormant), set())

    # -- what it must never do ----------------------------------------------

    def test_an_existing_quota_survives_a_rerun(self):
        """The whole reason this command is safe on a live database."""
        self.casual.annual_quota = 7
        self.casual.name = "Casual Leave (plant policy)"
        self.casual.save(update_fields=["annual_quota", "name"])

        self._run("--company", "JIVO_OIL", "--commit")

        self.casual.refresh_from_db()
        self.assertEqual(self.casual.annual_quota, 7)
        self.assertEqual(self.casual.name, "Casual Leave (plant policy)")

    def test_a_retired_type_is_not_revived(self):
        self._run("--company", "JIVO_OIL", "--commit")
        self.retired_type.refresh_from_db()
        self.assertEqual(self.retired_type.status, RecordStatus.INACTIVE)

    def test_rerunning_creates_nothing(self):
        self._run("--company", "JIVO_MART", "--commit")
        output = self._run("--company", "JIVO_MART", "--commit")
        self.assertIn("0 type(s) created", output)

    def test_it_does_not_reach_into_another_company(self):
        before = self._codes(self.company)
        self._run("--company", "JIVO_MART", "--commit")
        self.assertEqual(self._codes(self.company), before)

    def test_matching_is_case_insensitive_on_the_code(self):
        LeaveType.objects.create(company=self.other_company, code="cl", name="lowercase")
        self._run("--company", "JIVO_MART", "--commit")
        self.assertEqual(
            LeaveType.objects.filter(company=self.other_company, code__iexact="CL").count(),
            1,
            "a differently-cased code must not produce a duplicate",
        )

    # -- reordering ----------------------------------------------------------

    def test_sort_order_is_left_alone_without_the_flag(self):
        self.casual.sort_order = 99
        self.casual.save(update_fields=["sort_order"])
        self._run("--company", "JIVO_OIL", "--commit")
        self.casual.refresh_from_db()
        self.assertEqual(self.casual.sort_order, 99)

    def test_reorder_fixes_collisions_without_touching_policy(self):
        self.casual.sort_order = 99
        self.casual.annual_quota = 7
        self.casual.save(update_fields=["sort_order", "annual_quota"])

        self._run("--company", "JIVO_OIL", "--reorder", "--commit")

        self.casual.refresh_from_db()
        self.assertEqual(self.casual.sort_order, 0, "CL is first in the canonical order")
        self.assertEqual(self.casual.annual_quota, 7, "reordering must not touch a quota")

    def test_reorder_puts_unpaid_leave_last(self):
        self._run("--company", "JIVO_MART", "--reorder", "--commit")
        orders = dict(
            LeaveType.objects.filter(company=self.other_company).values_list(
                "code", "sort_order"
            )
        )
        self.assertEqual(orders["LWP"], max(orders.values()))

    def test_a_companys_own_extra_type_is_not_shuffled_to_the_top(self):
        LeaveType.objects.create(
            company=self.other_company, code="XTRA", name="Plant special", sort_order=0
        )
        self._run("--company", "JIVO_MART", "--reorder", "--commit")
        orders = dict(
            LeaveType.objects.filter(company=self.other_company).values_list(
                "code", "sort_order"
            )
        )
        self.assertGreater(
            orders["XTRA"],
            orders["LWP"],
            "an unrecognised type belongs after the standard ones, not before them",
        )

    # -- the figures themselves ---------------------------------------------

    def test_maternity_meets_the_statutory_floor(self):
        """26 weeks under the Maternity Benefit (Amendment) Act 2017."""
        self._run("--company", "JIVO_MART", "--commit")
        maternity = LeaveType.objects.get(company=self.other_company, code="ML")
        self.assertGreaterEqual(maternity.annual_quota, 182)
        self.assertTrue(maternity.requires_document)

    def test_compensatory_off_is_untracked(self):
        """It is earned one day at a time, so a ceiling would be meaningless."""
        self._run("--company", "JIVO_MART", "--commit")
        comp_off = LeaveType.objects.get(company=self.other_company, code="CO")
        self.assertEqual(comp_off.annual_quota, 0)

    def test_leave_without_pay_is_unpaid_and_untracked(self):
        self._run("--company", "JIVO_MART", "--commit")
        unpaid = LeaveType.objects.get(company=self.other_company, code="LWP")
        self.assertFalse(unpaid.is_paid)
        self.assertEqual(unpaid.annual_quota, 0)

    def test_every_standard_code_is_unique(self):
        codes = [row[0] for row in STANDARD_TYPES]
        self.assertEqual(len(codes), len(set(codes)))
