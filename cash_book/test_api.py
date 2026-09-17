"""
API tests for the cash book.

The service layer is proved in :mod:`cash_book.tests`; these cover what only
the HTTP layer decides -- company scoping, the three rights, the paged
envelope, and the one place SAP is touched on a write (the G/L snapshot).

SAP is patched out throughout. The reader is the only thing that talks to HANA,
so patching :func:`cash_book.views._snapshot_gl_account`'s reader keeps the
suite offline without weakening what is tested.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .models import (
    AdvanceDirection,
    BunchStatus,
    CashBranch,
    CashDirection,
    CashEntry,
)

User = get_user_model()

BASE = "/api/v1/cash-book"


class CashBookAPITestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.role = UserRole.objects.create(name="Accounts")
        cls.branch = CashBranch.objects.create(company=cls.company, name="Oil")
        cls.other_branch = CashBranch.objects.create(
            company=cls.other_company, name="Oil"
        )

        cls.custodian = cls._user("custodian@example.com", ["can_view_cash_book", "can_manage_cash_book"])
        cls.approver = cls._user("approver@example.com", ["can_view_cash_book", "can_approve_cash_bunch"])
        cls.viewer = cls._user("viewer@example.com", ["can_view_cash_book"])
        cls.outsider = cls._user("outsider@example.com", [])

    @classmethod
    def _user(cls, email, codenames):
        user = User.objects.create(email=email)
        user.set_password("x")
        user.save()
        for company in (cls.company, cls.other_company):
            UserCompany.objects.create(user=user, company=company, role=cls.role)
        if codenames:
            user.user_permissions.set(
                Permission.objects.filter(
                    content_type__app_label="cash_book", codename__in=codenames
                )
            )
        return user

    def as_user(self, user, company=None):
        self.client.force_authenticate(user=user)
        self.client.credentials(HTTP_COMPANY_CODE=(company or self.company).code)

    def payment(self, amount="6000.00", company=None):
        company = company or self.company
        return services.record_entry(
            user=self.custodian,
            company=company,
            entry_date="2026-06-04",
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            detail="Cash paid to Ravi kumar for refreshment",
            branch=self.branch if company == self.company else self.other_branch,
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
        )


class OptionsAndAccessTests(CashBookAPITestCase):
    def test_options_reports_the_rights_the_server_will_enforce(self):
        self.as_user(self.custodian)
        response = self.client.get(f"{BASE}/options/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["can_manage"])
        self.assertFalse(response.data["can_approve"])
        self.assertEqual(
            [row["name"] for row in response.data["branches"]], ["Oil"]
        )

    def test_a_user_with_no_cash_right_is_shut_out(self):
        self.as_user(self.outsider)
        self.assertEqual(self.client.get(f"{BASE}/entries/").status_code, 403)

    def test_a_viewer_reads_but_cannot_write(self):
        self.as_user(self.viewer)
        self.assertEqual(self.client.get(f"{BASE}/entries/").status_code, 200)
        response = self.client.post(
            f"{BASE}/entries/",
            {
                "entry_date": "2026-06-04",
                "direction": "IN",
                "amount": "50000.00",
                "detail": "Cash receive by ATM card",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_request_without_a_company_header_is_refused(self):
        self.client.force_authenticate(user=self.custodian)
        self.assertEqual(self.client.get(f"{BASE}/entries/").status_code, 403)


class RegisterTests(CashBookAPITestCase):
    def test_the_register_comes_back_paged_with_the_books_own_balance(self):
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.IN,
            amount=Decimal("50000.00"),
            detail="Cash receive by ATM card",
        )
        self.payment("6000.00")

        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/entries/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["page"], 1)
        self.assertEqual(Decimal(response.data["balance"]), Decimal("44000.00"))
        self.assertEqual(
            Decimal(response.data["totals"]["cash_out"]), Decimal("6000.00")
        )

    def test_the_balance_sent_is_the_books_not_the_filtered_sets(self):
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.IN,
            amount=Decimal("50000.00"),
            detail="Cash receive by ATM card",
        )
        self.payment("6000.00")

        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/entries/", {"direction": "OUT"})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(Decimal(response.data["balance"]), Decimal("44000.00"))

    def test_another_companys_book_is_not_visible(self):
        self.payment(company=self.other_company)
        self.as_user(self.viewer)
        self.assertEqual(self.client.get(f"{BASE}/entries/").data["count"], 0)

    def test_another_companys_entry_cannot_be_reached_by_id(self):
        elsewhere = self.payment(company=self.other_company)
        self.as_user(self.custodian)
        self.assertEqual(
            self.client.get(f"{BASE}/entries/{elsewhere.id}/").status_code, 404
        )


class RecordingTests(CashBookAPITestCase):
    def test_a_receipt_needs_no_gl_head_and_never_touches_sap(self):
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            response = self.client.post(
                f"{BASE}/entries/",
                {
                    "entry_date": "2026-06-04",
                    "direction": "IN",
                    "amount": "50000.00",
                    "detail": "Cash receive by ATM card",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        reader.assert_not_called()
        self.assertEqual(Decimal(response.data["balance_after"]), Decimal("50000.00"))

    def test_a_payment_snapshots_the_name_sap_holds_not_the_one_sent(self):
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.resolve.return_value = {
                "account_code": "5630004",
                "account_name": "REFRESHMENT",
            }
            response = self.client.post(
                f"{BASE}/entries/",
                {
                    "entry_date": "2026-06-04",
                    "direction": "OUT",
                    "amount": "6000.00",
                    "branch": self.branch.id,
                    "gl_account_code": "5630004",
                    "gl_account_name": "whatever the client said",
                    "item": "Refreshment",
                    "detail": "Cash paid to Ravi kumar",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["gl_account_name"], "REFRESHMENT")

    def test_an_account_sap_will_not_post_to_is_refused(self):
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.resolve.side_effect = SAPDataError("9999999 is not postable")
            response = self.client.post(
                f"{BASE}/entries/",
                {
                    "entry_date": "2026-06-04",
                    "direction": "OUT",
                    "amount": "6000.00",
                    "branch": self.branch.id,
                    "gl_account_code": "9999999",
                    "detail": "Cash paid",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("gl_account_code", response.data)
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_sap_being_down_does_not_stop_cash_being_recorded(self):
        """The custodian is holding real money; SAP's uptime is not their problem."""
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.resolve.side_effect = SAPConnectionError("unreachable")
            response = self.client.post(
                f"{BASE}/entries/",
                {
                    "entry_date": "2026-06-04",
                    "direction": "OUT",
                    "amount": "6000.00",
                    "branch": self.branch.id,
                    "gl_account_code": "5630004",
                    "gl_account_name": "REFRESHMENT",
                    "detail": "Cash paid to Ravi kumar",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["gl_account_name"], "REFRESHMENT")

    def test_the_gl_picker_answers_503_rather_than_an_empty_list_when_sap_is_down(self):
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.search.side_effect = SAPConnectionError("unreachable")
            response = self.client.get(f"{BASE}/gl-accounts/", {"search": "refresh"})
        self.assertEqual(response.status_code, 503)

    def test_cancelling_an_entry_leaves_the_line_in_the_book(self):
        entry = self.payment()
        self.as_user(self.custodian)
        self.assertEqual(
            self.client.delete(f"{BASE}/entries/{entry.id}/").status_code, 204
        )
        entry.refresh_from_db()
        self.assertFalse(entry.is_active)
        self.assertEqual(CashEntry.objects.count(), 1)


class BunchAPITests(CashBookAPITestCase):
    def send(self, entry):
        self.as_user(self.custodian)
        return self.client.post(
            f"{BASE}/bunches/",
            {"entry_ids": [entry.id], "remarks": "June vouchers"},
            format="json",
        )

    def test_the_custodian_sends_and_the_approver_decides(self):
        entry = self.payment()
        sent = self.send(entry)
        self.assertEqual(sent.status_code, 201)
        bunch_id = sent.data["id"]

        # The custodian may not decide on their own bunch.
        self.assertEqual(
            self.client.post(f"{BASE}/bunches/{bunch_id}/approve/", {}, format="json").status_code,
            403,
        )

        self.as_user(self.approver)
        approved = self.client.post(
            f"{BASE}/bunches/{bunch_id}/approve/", {}, format="json"
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.data["status"], BunchStatus.APPROVED)
        self.assertIsNotNone(approved.data["decided_at"])
        self.assertEqual(approved.data["decided_by_name"], self.approver.full_name)

    def test_a_rejection_without_a_reason_is_refused(self):
        entry = self.payment()
        bunch_id = self.send(entry).data["id"]

        self.as_user(self.approver)
        self.assertEqual(
            self.client.post(f"{BASE}/bunches/{bunch_id}/reject/", {}, format="json").status_code,
            400,
        )

    def test_a_rejected_bunch_unfreezes_and_can_be_sent_again(self):
        entry = self.payment()
        bunch_id = self.send(entry).data["id"]

        self.as_user(self.approver)
        self.client.post(
            f"{BASE}/bunches/{bunch_id}/reject/",
            {"note": "Bill number missing"},
            format="json",
        )

        self.as_user(self.custodian)
        corrected = self.client.patch(
            f"{BASE}/entries/{entry.id}/", {"detail": "Bill no. 128"}, format="json"
        )
        self.assertEqual(corrected.status_code, 200)

        resent = self.client.post(
            f"{BASE}/bunches/{bunch_id}/resend/", {}, format="json"
        )
        self.assertEqual(resent.status_code, 200)
        self.assertEqual(resent.data["status"], BunchStatus.PENDING)
        self.assertEqual(resent.data["number"], 1)

    def test_an_entry_awaiting_approval_is_refused_a_correction(self):
        entry = self.payment()
        self.send(entry)
        response = self.client.patch(
            f"{BASE}/entries/{entry.id}/", {"amount": "1.00"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_another_companys_bunch_cannot_be_decided(self):
        elsewhere = self.payment(company=self.other_company)
        self.as_user(self.custodian, company=self.other_company)
        bunch_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [elsewhere.id]}, format="json"
        ).data["id"]

        self.as_user(self.approver, company=self.company)
        self.assertEqual(
            self.client.post(f"{BASE}/bunches/{bunch_id}/approve/", {}, format="json").status_code,
            404,
        )

    def test_the_summary_counts_what_is_still_outstanding(self):
        entry = self.payment()
        self.payment("2000.00")
        self.send(entry)

        self.as_user(self.viewer)
        summary = self.client.get(f"{BASE}/summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["pending_bunches"], 1)
        self.assertEqual(summary.data["unsent_entries"], 1)


class BranchSettingsAPITests(CashBookAPITestCase):
    """The settings screen behind the entry form's Branch picker."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = cls._user(
            "branchadmin@example.com",
            ["can_view_cash_book", "can_manage_cash_book", "can_manage_cash_branches"],
        )

    def test_anyone_who_can_read_the_book_can_read_the_branches(self):
        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/branches/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["name"] for row in response.data], ["Oil"])

    def test_a_custodian_cannot_change_the_branch_list(self):
        """Keeping the book and configuring it are separate rights."""
        self.as_user(self.custodian)
        response = self.client.post(f"{BASE}/branches/", {"name": "Beverage"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_an_administrator_adds_a_branch(self):
        self.as_user(self.admin)
        response = self.client.post(
            f"{BASE}/branches/", {"name": "Beverage", "sort_order": 1}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            CashBranch.objects.filter(company=self.company, name="Beverage").exists()
        )

    def test_a_duplicate_name_is_refused(self):
        self.as_user(self.admin)
        response = self.client.post(f"{BASE}/branches/", {"name": "oil"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CashBranch.objects.filter(company=self.company).count(), 1)

    def test_adding_a_retired_name_revives_it_rather_than_duplicating(self):
        """Two branches called Water would split every report in half."""
        self.as_user(self.admin)
        self.client.delete(f"{BASE}/branches/{self.branch.id}/")
        response = self.client.post(f"{BASE}/branches/", {"name": "Oil"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CashBranch.objects.filter(company=self.company).count(), 1)
        self.branch.refresh_from_db()
        self.assertTrue(self.branch.is_active)

    def test_a_branch_is_renamed_in_place_so_its_entries_follow(self):
        entry = self.payment()
        self.as_user(self.admin)
        response = self.client.patch(
            f"{BASE}/branches/{self.branch.id}/", {"name": "Oils"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.branch.name, "Oils")

    def test_retiring_a_used_branch_hides_it_but_keeps_the_entries(self):
        """The FK is PROTECT, so retiring is the only way to take one out."""
        entry = self.payment()
        self.as_user(self.admin)
        self.assertEqual(
            self.client.delete(f"{BASE}/branches/{self.branch.id}/").status_code, 204
        )
        entry.refresh_from_db()
        self.assertEqual(entry.branch_id, self.branch.id)
        self.assertEqual(self.client.get(f"{BASE}/branches/").data, [])
        self.assertEqual(
            len(self.client.get(f"{BASE}/branches/", {"include_retired": "true"}).data), 1
        )

    def test_a_retired_branch_is_not_offered_by_the_entry_form(self):
        self.as_user(self.admin)
        self.client.delete(f"{BASE}/branches/{self.branch.id}/")
        self.assertEqual(self.client.get(f"{BASE}/options/").data["branches"], [])

    def test_the_branch_list_says_how_many_entries_each_holds(self):
        self.payment()
        self.payment()
        self.as_user(self.viewer)
        self.assertEqual(self.client.get(f"{BASE}/branches/").data[0]["entry_count"], 2)

    def test_another_companys_branch_cannot_be_reached(self):
        self.as_user(self.admin, company=self.company)
        response = self.client.patch(
            f"{BASE}/branches/{self.other_branch.id}/", {"name": "X"}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_a_payment_cannot_be_filed_against_another_companys_branch(self):
        self.as_user(self.custodian)
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.resolve.return_value = {
                "account_code": "5630004",
                "account_name": "REFRESHMENT",
            }
            response = self.client.post(
                f"{BASE}/entries/",
                {
                    "entry_date": "2026-06-04",
                    "direction": "OUT",
                    "amount": "10.00",
                    "branch": self.other_branch.id,
                    "gl_account_code": "5630004",
                    "detail": "Cash paid",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 400)


class BranchMigrationMappingTests(TestCase):
    """The department names the live book actually held all land somewhere.

    Migration 0002 carries existing entries across by department name. These
    are the names the dev database really has -- the ones the sheet import
    created plus the older ``accounts.Department`` rows -- so a rename that
    forgot one would show up here rather than as a pile of Common.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from importlib import import_module

        cls.migration = import_module(
            "cash_book.migrations.0002_branch_replaces_department"
        )

    def branch_of(self, name):
        return self.migration.BRANCH_OF.get(
            name.strip().lower(), self.migration.FALLBACK_BRANCH
        )

    def test_the_plant_lines_map_to_their_own_branch(self):
        self.assertEqual(self.branch_of("Canola"), "Oil")
        self.assertEqual(self.branch_of("WG"), "Beverage")
        self.assertEqual(self.branch_of("Wg"), "Beverage")
        self.assertEqual(self.branch_of("Water"), "Water")

    def test_everything_else_the_book_held_falls_to_common(self):
        for name in ("Mart", "Common", "Maintenance", "production", "QC",
                     "Utilities", "IT", "Ecom", "Store", "Mess", ""):
            self.assertEqual(self.branch_of(name), "Common", name)

    def test_every_mapping_lands_on_one_of_the_four(self):
        allowed = set(self.migration.DEFAULT_BRANCHES)
        self.assertEqual(allowed, {"Oil", "Beverage", "Water", "Common"})
        self.assertTrue(set(self.migration.BRANCH_OF.values()) <= allowed)
        self.assertIn(self.migration.FALLBACK_BRANCH, allowed)

    def test_the_importer_and_the_migration_agree(self):
        """Two code paths, one answer -- they must not drift apart."""
        from cash_book import sheet_import

        for name in ("Canola", "WG", "Water", "Mart", "Common", "Nowhere", ""):
            self.assertEqual(
                sheet_import.to_branch(name), self.branch_of(name), name
            )


class PeoplePickerTests(CashBookAPITestCase):
    """Who the picker offers, which is two different questions.

    Giving cash out may go to anybody on the staff. Taking it back, or clearing
    it with an expense, can only involve somebody who actually has some --
    offering the whole directory there lets a float be settled against a person
    who never took one, which is silent and wrong.
    """

    def test_without_holding_it_offers_everybody_in_the_company(self):
        self.as_user(self.custodian)
        response = self.client.get(f"{BASE}/people/")
        self.assertEqual(response.status_code, 200)
        emails = {row["email"] for row in response.data}
        self.assertIn("custodian@example.com", emails)
        self.assertIn("viewer@example.com", emails)

    def test_holding_offers_nobody_until_an_advance_is_given(self):
        self.as_user(self.custodian)
        response = self.client.get(f"{BASE}/people/", {"holding": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_holding_offers_the_person_once_they_hold_a_float(self):
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.viewer,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )
        self.as_user(self.custodian)
        rows = self.client.get(f"{BASE}/people/", {"holding": "true"}).data
        self.assertEqual([row["email"] for row in rows], ["viewer@example.com"])

    def test_a_holder_is_offered_with_what_they_are_holding(self):
        """The balance is why the list is narrowed, so it is shown."""
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.viewer,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )
        self.as_user(self.custodian)
        rows = self.client.get(f"{BASE}/people/", {"holding": "true"}).data
        self.assertEqual(Decimal(rows[0]["balance"]), Decimal("15000.00"))

    def test_the_open_list_carries_no_balance(self):
        self.as_user(self.custodian)
        rows = self.client.get(f"{BASE}/people/").data
        self.assertTrue(all(row["balance"] is None for row in rows))

    def test_holding_is_scoped_to_the_company(self):
        services.record_advance(
            user=self.custodian,
            company=self.other_company,
            person=self.viewer,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("500.00"),
        )
        self.as_user(self.custodian, company=self.company)
        self.assertEqual(self.client.get(f"{BASE}/people/", {"holding": "true"}).data, [])

    def test_both_lists_are_searchable(self):
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.viewer,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )
        self.as_user(self.custodian)
        self.assertEqual(
            len(self.client.get(f"{BASE}/people/", {"search": "viewer"}).data), 1
        )
        self.assertEqual(
            len(
                self.client.get(
                    f"{BASE}/people/", {"holding": "true", "search": "viewer"}
                ).data
            ),
            1,
        )
        self.assertEqual(
            len(
                self.client.get(
                    f"{BASE}/people/", {"holding": "true", "search": "nobody"}
                ).data
            ),
            0,
        )

    def test_somebody_settled_back_to_zero_is_still_offered(self):
        """They can still explain a spend; a zero balance is not a closed account."""
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.viewer,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("100.00"),
        )
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.viewer,
            entry_date="2026-06-05",
            direction=AdvanceDirection.RETURNED,
            amount=Decimal("100.00"),
        )
        self.as_user(self.custodian)
        rows = self.client.get(f"{BASE}/people/", {"holding": "true"}).data
        self.assertEqual([row["email"] for row in rows], ["viewer@example.com"])
        self.assertEqual(Decimal(rows[0]["balance"]), Decimal("0.00"))
