"""
Tests for advances against salary.

The distinction this module exists for is the one worth proving: an advance
against a wage is **not** a float. A float is the factory's cash in somebody's
pocket, settled by spending it and explaining what on. This is money that
became theirs when it was handed over, and it is settled out of their pay --
which is a decision HR take, not the cash approver.

So the two things asserted hardest are that recording one moves no balance,
and that the cash approver's right does not carry HR's.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from employee_hierarchy.constants import EmploymentStatus
from employee_hierarchy.models import Department, Employee

from . import services
from .models import CashBranch, CashDirection, SalaryAdvance, SalaryAdvanceState

User = get_user_model()


class SalaryAdvanceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.branch = CashBranch.objects.create(company=cls.company, name="Oil")
        cls.role, _ = UserRole.objects.get_or_create(name="Accounts")

        cls.custodian = cls._user("custodian@example.com", "can_manage_cash_book")
        cls.hr = cls._user("hr@example.com", "can_approve_salary_advances")
        # Holds the cash book's approve right and nothing else. The point of
        # this one is everything it may NOT do below.
        cls.cash_approver = cls._user(
            "approver@example.com", "can_approve_cash_entries"
        )

        cls.packing = Department.objects.create(
            company=cls.company, code="PACK", name="Packing"
        )
        cls.parveen = cls._employee("EMP001", "Parveen", "Khatun")
        cls.shyam = cls._employee("EMP002", "Shyam", "Shukla")
        cls.outsider = cls._employee(
            "EMP900", "Someone", "Else", company=cls.other_company
        )

    # ------------------------------------------------------------- fixtures

    @classmethod
    def _user(cls, email, *codenames):
        user = User.objects.create(email=email, full_name=email.split("@")[0])
        user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label="cash_book", codename__in=codenames
            )
        )
        UserCompany.objects.create(user=user, company=cls.company, role=cls.role)
        return User.objects.get(pk=user.pk)

    @classmethod
    def _employee(cls, code, first, last, *, company=None, status=None):
        return Employee.objects.create(
            company=company or cls.company,
            employee_code=code,
            first_name=first,
            last_name=last,
            joining_date=date(2024, 1, 1),
            employment_status=status or EmploymentStatus.ACTIVE,
        )

    def advance(self, *, employee=None, amount="5000.00", paid_on=None, **kwargs):
        return services.record_salary_advance(
            user=self.custodian,
            company=self.company,
            employee=employee or self.parveen,
            paid_on=paid_on or date(2026, 9, 17),
            amount=Decimal(amount),
            **kwargs,
        )

    def client_for(self, user):
        client = APIClient(headers={"Company-Code": self.company.code})
        client.force_authenticate(user=user)
        return client

    # -------------------------------------------------------------- the row

    def test_it_reaches_hr_the_moment_it_is_recorded(self):
        """There is no draft. Telling HR is the whole purpose of the row."""
        advance = self.advance()
        self.assertEqual(advance.state, SalaryAdvanceState.PENDING)
        self.assertIsNone(advance.decided_at)
        self.assertIsNone(advance.deduct_from)

    def test_it_moves_no_cash_book_balance(self):
        """The money left the box on the voucher, not on this row.

        Counting it in both places would take it off the balance twice.
        """
        before = services.current_balance(self.company)
        self.advance(amount="9000.00")
        self.assertEqual(services.current_balance(self.company), before)

    def test_it_refuses_another_company_payroll(self):
        with self.assertRaises(ValidationError):
            self.advance(employee=self.outsider)

    # ------------------------------------------------------------- HR's say

    def test_approving_marks_it_for_the_month_after_it_was_paid(self):
        """A wage already run cannot be docked, so it is never this month."""
        advance = self.advance(paid_on=date(2026, 9, 17))
        (decided,) = services.decide_salary_advances(
            user=self.hr,
            company=self.company,
            advance_ids=[advance.id],
            approve=True,
        )
        self.assertEqual(decided.state, SalaryAdvanceState.APPROVED)
        self.assertEqual(decided.deduct_from, date(2026, 10, 1))
        self.assertEqual(decided.decided_by, self.hr)
        self.assertTrue(decided.is_outstanding)

    def test_december_rolls_into_january(self):
        advance = self.advance(paid_on=date(2026, 12, 31))
        (decided,) = services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        self.assertEqual(decided.deduct_from, date(2027, 1, 1))

    def test_hr_may_name_another_month(self):
        advance = self.advance()
        (decided,) = services.decide_salary_advances(
            user=self.hr,
            company=self.company,
            advance_ids=[advance.id],
            approve=True,
            deduct_from=date(2026, 12, 1),
        )
        self.assertEqual(decided.deduct_from, date(2026, 12, 1))

    def test_rejecting_must_say_why(self):
        """The cash is already with them, so a rejection owes an explanation."""
        advance = self.advance()
        with self.assertRaises(ValidationError):
            services.decide_salary_advances(
                user=self.hr,
                company=self.company,
                advance_ids=[advance.id],
                approve=False,
            )

    def test_a_rejected_advance_carries_no_month(self):
        advance = self.advance()
        (decided,) = services.decide_salary_advances(
            user=self.hr,
            company=self.company,
            advance_ids=[advance.id],
            approve=False,
            note="Recovering it in cash instead.",
        )
        self.assertEqual(decided.state, SalaryAdvanceState.REJECTED)
        self.assertIsNone(decided.deduct_from)
        self.assertFalse(decided.is_outstanding)

    def test_it_cannot_be_decided_twice(self):
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        with self.assertRaises(ValidationError):
            services.decide_salary_advances(
                user=self.hr,
                company=self.company,
                advance_ids=[advance.id],
                approve=True,
            )

    def test_one_bad_id_decides_nothing(self):
        """A batch verdict is all or nothing; a half-applied one is worse."""
        advance = self.advance()
        with self.assertRaises(ValidationError):
            services.decide_salary_advances(
                user=self.hr,
                company=self.company,
                advance_ids=[advance.id, 9999],
                approve=True,
            )
        advance.refresh_from_db()
        self.assertEqual(advance.state, SalaryAdvanceState.PENDING)

    # ---------------------------------------------------------- corrections

    def test_a_pending_advance_can_be_corrected(self):
        advance = self.advance(amount="5000.00")
        fixed = services.update_salary_advance(
            user=self.custodian, advance=advance, amount=Decimal("4000.00")
        )
        self.assertEqual(fixed.amount, Decimal("4000.00"))

    def test_a_decided_advance_cannot_be(self):
        """HR agreed to an amount for a person. Changing either behind them
        would leave a verdict standing for something nobody agreed to."""
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        # The verdict was written to rows the service fetched for itself, so
        # the object here is stale -- exactly as the view's would be if it did
        # not re-read. It does, so the guard is read off the database.
        advance.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.update_salary_advance(
                user=self.custodian, advance=advance, amount=Decimal("1.00")
            )

    # ----------------------------------------------------------- the ticking

    def test_deducting_closes_the_loop(self):
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        advance.refresh_from_db()
        taken = services.mark_salary_advance_deducted(
            user=self.hr, advance=advance, deducted_on=date(2026, 10, 31)
        )
        self.assertEqual(taken.deducted_on, date(2026, 10, 31))
        self.assertFalse(taken.is_outstanding)

    def test_only_an_approved_advance_can_be_deducted(self):
        advance = self.advance()
        with self.assertRaises(ValidationError):
            services.mark_salary_advance_deducted(user=self.hr, advance=advance)

    def test_it_cannot_be_deducted_twice(self):
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        advance.refresh_from_db()
        services.mark_salary_advance_deducted(user=self.hr, advance=advance)
        with self.assertRaises(ValidationError):
            services.mark_salary_advance_deducted(user=self.hr, advance=advance)

    def test_a_deduction_can_be_undone(self):
        """For the payroll run that was reversed, or the wrong name ticked."""
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        advance.refresh_from_db()
        services.mark_salary_advance_deducted(user=self.hr, advance=advance)
        back = services.undo_salary_advance_deduction(user=self.hr, advance=advance)
        self.assertIsNone(back.deducted_on)
        # It goes back to owed, not back to HR -- their verdict has not changed.
        self.assertEqual(back.state, SalaryAdvanceState.APPROVED)
        self.assertTrue(back.is_outstanding)

    # -------------------------------------------------------- what HR are owed

    def test_outstanding_is_approved_minus_what_was_already_taken(self):
        """Not "approved", which would keep counting a recovered advance."""
        pending = self.advance(amount="1000.00")
        owed = self.advance(employee=self.shyam, amount="2000.00")
        taken = self.advance(amount="3000.00")
        services.decide_salary_advances(
            user=self.hr,
            company=self.company,
            advance_ids=[owed.id, taken.id],
            approve=True,
        )
        taken.refresh_from_db()
        services.mark_salary_advance_deducted(user=self.hr, advance=taken)

        summary = services.salary_advance_summary(self.company)
        self.assertEqual(summary["pending"]["amount"], Decimal("1000.00"))
        self.assertEqual(summary["pending"]["count"], 1)
        self.assertEqual(summary["outstanding"]["amount"], Decimal("2000.00"))
        self.assertEqual(summary["deducted"]["amount"], Decimal("3000.00"))
        self.assertEqual(summary["approved"]["amount"], Decimal("5000.00"))
        self.assertEqual(pending.state, SalaryAdvanceState.PENDING)

    def test_a_cancelled_advance_counts_for_nothing(self):
        advance = self.advance(amount="7000.00")
        services.cancel_salary_advance(user=self.custodian, advance=advance)
        summary = services.salary_advance_summary(self.company)
        self.assertEqual(summary["pending"]["count"], 0)
        self.assertEqual(summary["pending"]["amount"], Decimal("0.00"))
        self.assertFalse(
            services.salary_advances(self.company).filter(pk=advance.pk).exists()
        )
        self.assertTrue(
            services.salary_advances(self.company, include_cancelled=True)
            .filter(pk=advance.pk)
            .exists()
        )

    # ------------------------------------------------------------- the picker

    def test_the_picker_offers_this_payroll_only(self):
        names = {row.employee_code for row in services.salary_advance_employees(self.company)}
        self.assertIn("EMP001", names)
        self.assertNotIn("EMP900", names)

    def test_the_picker_leaves_out_people_who_have_gone(self):
        """No wage, no deduction. Chasing them is somebody else's job."""
        self._employee("EMP003", "Gone", "Away", status=EmploymentStatus.RESIGNED)
        # Suspended is in -- they are still on the payroll.
        self._employee("EMP004", "Suspended", "Still", status=EmploymentStatus.SUSPENDED)
        codes = {
            row.employee_code for row in services.salary_advance_employees(self.company)
        }
        self.assertNotIn("EMP003", codes)
        self.assertIn("EMP004", codes)

    def test_the_picker_searches_on_name_and_code(self):
        by_name = services.salary_advance_employees(self.company, "shyam")
        self.assertEqual([row.employee_code for row in by_name], ["EMP002"])
        by_code = services.salary_advance_employees(self.company, "EMP001")
        self.assertEqual([row.employee_code for row in by_code], ["EMP001"])

    # --------------------------------------------------------- the two rights

    def test_the_cash_approver_may_not_decide_a_deduction(self):
        """The control this page adds. Agreeing to a payment and agreeing to
        dock somebody's wages are different decisions by different people."""
        advance = self.advance()
        response = self.client_for(self.cash_approver).post(
            reverse("cash-book-salary-advances-decide"),
            {"advance_ids": [advance.id], "approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        advance.refresh_from_db()
        self.assertEqual(advance.state, SalaryAdvanceState.PENDING)

    def test_hr_may_not_record_an_advance_for_somebody(self):
        response = self.client_for(self.hr).post(
            reverse("cash-book-salary-advances"),
            {
                "employee": self.parveen.id,
                "paid_on": "2026-09-17",
                "amount": "1000.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SalaryAdvance.objects.exists())

    def test_hr_may_read_one_advance_but_not_change_it(self):
        """Refusing them the row whose list they already read would be a
        distinction without a reason -- but correcting it is book-keeping."""
        advance = self.advance()
        client = self.client_for(self.hr)
        url = reverse("cash-book-salary-advance-detail", args=[advance.id])

        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(
            client.patch(url, {"amount": "1.00"}, format="json").status_code, 403
        )
        self.assertEqual(client.delete(url).status_code, 403)
        advance.refresh_from_db()
        self.assertEqual(advance.amount, Decimal("5000.00"))
        self.assertTrue(advance.is_active)

    def test_accounts_may_not_tick_off_a_deduction(self):
        """Only the payroll knows a wage was actually docked."""
        advance = self.advance()
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[advance.id], approve=True
        )
        response = self.client_for(self.custodian).post(
            reverse("cash-book-salary-advance-deducted", args=[advance.id]),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        advance.refresh_from_db()
        self.assertIsNone(advance.deducted_on)

    def test_hr_read_the_screen_without_the_cash_book(self):
        """They decide on advances; they are not book-keepers, and the screen
        has to let them in on their own right."""
        self.advance()
        response = self.client_for(self.hr).get(reverse("cash-book-salary-advances"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertTrue(response.data["can_decide"])
        self.assertFalse(response.data["can_record"])

    def test_the_custodian_is_told_they_cannot_decide(self):
        self.advance()
        response = self.client_for(self.custodian).get(
            reverse("cash-book-salary-advances")
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["can_record"])
        self.assertFalse(response.data["can_decide"])

    # -------------------------------------------------------------- the wire

    def test_the_list_says_who_and_how_much(self):
        advance = self.advance(reason="Advance for his daughter's fees")
        response = self.client_for(self.custodian).get(
            reverse("cash-book-salary-advances"), {"state": "PENDING"}
        )
        self.assertEqual(response.status_code, 200)
        (row,) = response.data["results"]
        self.assertEqual(row["id"], advance.id)
        self.assertEqual(row["employee_name"], "Parveen Khatun")
        self.assertEqual(row["employee_code"], "EMP001")
        self.assertEqual(row["department"], "")
        self.assertEqual(row["state_label"], "With HR")
        self.assertEqual(row["reason"], "Advance for his daughter's fees")

    def test_the_summary_is_over_the_book_not_the_tab(self):
        """A tab showing four pending advances must not restate what is
        outstanding as though the other tabs were empty."""
        owed = self.advance(amount="2000.00")
        services.decide_salary_advances(
            user=self.hr, company=self.company, advance_ids=[owed.id], approve=True
        )
        self.advance(employee=self.shyam, amount="800.00")

        response = self.client_for(self.custodian).get(
            reverse("cash-book-salary-advances"), {"state": "PENDING"}
        )
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            Decimal(response.data["summary"]["outstanding"]["amount"]),
            Decimal("2000.00"),
        )

    def test_an_unknown_state_is_refused_rather_than_ignored(self):
        response = self.client_for(self.custodian).get(
            reverse("cash-book-salary-advances"), {"state": "MAYBE"}
        )
        self.assertEqual(response.status_code, 400)

    def test_the_voucher_it_was_paid_on_travels_with_it(self):
        """So the page can say which line of the register this money left on."""
        entry = services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=date(2026, 9, 17),
            direction=CashDirection.OUT,
            amount=Decimal("5000.00"),
            detail="Cash paid Advance to Parveen",
            branch=self.branch,
            gl_account_code="1101015",
            gl_account_name="SUNDRY DEBTORS STAFF",
            require_approver=False,
        )
        advance = self.advance(cash_entry=entry)
        response = self.client_for(self.custodian).get(
            reverse("cash-book-salary-advance-detail", args=[advance.id])
        )
        self.assertEqual(response.data["voucher_number"], entry.serial_number)
