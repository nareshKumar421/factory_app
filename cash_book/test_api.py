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
        cls.approver = cls._user("approver@example.com", ["can_view_cash_book", "can_approve_cash_entries"])
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
            approver=self.approver,
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
        response = self.client.get(f"{BASE}/entries/", {"f_direction": "OUT"})
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
                    "approver": self.approver.id,
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
                    "approver": self.approver.id,
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
                    "approver": self.approver.id,
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
    """Bundling, downloading, and saying it has gone."""

    def approved(self, amount="6000.00"):
        entry = self.payment(amount)
        services.decide_entries(
            user=self.approver,
            company=self.company,
            entry_ids=[entry.id],
            approve=True,
        )
        return entry

    def test_a_batch_is_made_from_approved_entries(self):
        first, second = self.approved(), self.approved("2000.00")
        self.as_user(self.custodian)
        response = self.client.post(
            f"{BASE}/bunches/",
            {"entry_ids": [first.id, second.id], "remarks": "June vouchers"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["number"], 1)
        self.assertEqual(response.data["entry_count"], 2)
        self.assertEqual(Decimal(response.data["total"]), Decimal("8000.00"))
        self.assertFalse(response.data["is_sent"])

    def test_an_unapproved_entry_is_refused(self):
        waiting = self.payment()
        self.as_user(self.custodian)
        response = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [waiting.id]}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_it_downloads_as_a_spreadsheet(self):
        entry = self.approved()
        self.as_user(self.custodian)
        bunch_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [entry.id]}, format="json"
        ).data["id"]

        response = self.client.get(f"{BASE}/bunches/{bunch_id}/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("bunch-1.xlsx", response["Content-Disposition"])
        # A real xlsx is a zip, so it starts with the zip magic number.
        self.assertTrue(response.content.startswith(b"PK"))

    def test_the_spreadsheet_carries_the_vouchers_and_their_total(self):
        import io

        import openpyxl

        first, second = self.approved("6000.00"), self.approved("2000.00")
        self.as_user(self.custodian)
        bunch_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [first.id, second.id]}, format="json"
        ).data["id"]

        content = self.client.get(f"{BASE}/bunches/{bunch_id}/export/").content
        sheet = openpyxl.load_workbook(io.BytesIO(content)).active
        cells = [
            [cell for cell in row if cell is not None]
            for row in sheet.iter_rows(values_only=True)
        ]
        flat = [str(value) for row in cells for value in row]
        self.assertIn("Total", flat)
        self.assertIn(str(first.id), flat)
        self.assertIn("8000", flat)

    def test_marking_it_sent_is_recorded_and_reversible(self):
        entry = self.approved()
        self.as_user(self.custodian)
        bunch_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [entry.id]}, format="json"
        ).data["id"]

        sent = self.client.post(f"{BASE}/bunches/{bunch_id}/sent/", {}, format="json")
        self.assertTrue(sent.data["is_sent"])
        self.assertEqual(sent.data["sent_by_name"], self.custodian.full_name)

        back = self.client.post(
            f"{BASE}/bunches/{bunch_id}/sent/", {"sent": False}, format="json"
        )
        self.assertFalse(back.data["is_sent"])

    def test_a_voucher_can_be_pulled_out_before_the_batch_goes(self):
        entry = self.approved()
        self.as_user(self.custodian)
        self.client.post(f"{BASE}/bunches/", {"entry_ids": [entry.id]}, format="json")

        response = self.client.delete(f"{BASE}/entries/{entry.id}/bunch/")
        self.assertEqual(response.status_code, 204)
        entry.refresh_from_db()
        self.assertIsNone(entry.bunch_id)

    def test_the_list_can_be_narrowed_to_what_has_not_gone(self):
        first, second = self.approved(), self.approved("2000.00")
        self.as_user(self.custodian)
        sent_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [first.id]}, format="json"
        ).data["id"]
        self.client.post(f"{BASE}/bunches/", {"entry_ids": [second.id]}, format="json")
        self.client.post(f"{BASE}/bunches/{sent_id}/sent/", {}, format="json")

        unsent = self.client.get(f"{BASE}/bunches/", {"state": "UNSENT"}).data
        self.assertEqual([row["number"] for row in unsent], [2])

    def test_another_companys_batch_cannot_be_reached(self):
        elsewhere = self.payment(company=self.other_company)
        services.decide_entries(
            user=self.approver,
            company=self.other_company,
            entry_ids=[elsewhere.id],
            approve=True,
        )
        self.as_user(self.custodian, company=self.other_company)
        bunch_id = self.client.post(
            f"{BASE}/bunches/", {"entry_ids": [elsewhere.id]}, format="json"
        ).data["id"]

        self.as_user(self.custodian, company=self.company)
        self.assertEqual(
            self.client.get(f"{BASE}/bunches/{bunch_id}/").status_code, 404
        )

    def test_the_summary_counts_what_is_still_outstanding(self):
        self.payment()
        self.payment("2000.00")

        self.as_user(self.viewer)
        summary = self.client.get(f"{BASE}/summary/")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data["awaiting_approval"], 2)
        self.assertEqual(summary.data["rejected_entries"], 0)


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
                    "approver": self.approver.id,
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


class RegisterSortAndValueFilterTests(CashBookAPITestCase):
    """Sorting is the server's because the register is paged.

    Ordering one page of fifty in the browser would only shuffle the rows that
    happened to land on it, which reads as a sort and is not one.
    """

    def setUp(self):
        super().setUp()
        self.small = self.payment("100.00")
        self.large = self.payment("9000.00")
        self.middle = self.payment("500.00")

    def amounts(self, **params):
        self.as_user(self.viewer)
        rows = self.client.get(f"{BASE}/entries/", params).data["results"]
        return [Decimal(row["amount"]) for row in rows]

    def test_the_default_order_is_newest_recorded_first(self):
        self.assertEqual(
            self.amounts(),
            [Decimal("500.00"), Decimal("9000.00"), Decimal("100.00")],
        )

    def test_it_sorts_by_amount_both_ways(self):
        self.assertEqual(
            self.amounts(sort="amount"),
            [Decimal("100.00"), Decimal("500.00"), Decimal("9000.00")],
        )
        self.assertEqual(
            self.amounts(sort="-amount"),
            [Decimal("9000.00"), Decimal("500.00"), Decimal("100.00")],
        )

    def test_an_unknown_sort_falls_back_rather_than_failing(self):
        """A stale bookmark should show the register, not an error."""
        self.assertEqual(self.amounts(sort="spaceship"), self.amounts())

    def test_the_order_is_total_so_paging_cannot_lose_a_row(self):
        """Every sort ends in id, so equal values never swap between pages."""
        for _ in range(3):
            self.payment("100.00")
        self.as_user(self.viewer)
        first = self.client.get(
            f"{BASE}/entries/", {"sort": "amount", "page_size": 3, "page": 1}
        ).data["results"]
        second = self.client.get(
            f"{BASE}/entries/", {"sort": "amount", "page_size": 3, "page": 2}
        ).data["results"]
        ids = [row["id"] for row in first] + [row["id"] for row in second]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_column_filter_keeps_only_the_ticked_values(self):
        self.assertEqual(
            self.amounts(f_amount="100.00|9000.00"),
            [Decimal("9000.00"), Decimal("100.00")],
        )

    def test_two_columns_filter_together(self):
        self.assertEqual(
            self.amounts(f_amount="100.00|9000.00", f_direction="OUT"),
            [Decimal("9000.00"), Decimal("100.00")],
        )
        self.assertEqual(self.amounts(f_amount="100.00", f_direction="IN"), [])

    def test_an_empty_filter_narrows_nothing(self):
        self.assertEqual(self.amounts(f_branch=""), self.amounts())

    def test_the_options_say_what_the_columns_are(self):
        self.as_user(self.viewer)
        columns = self.client.get(f"{BASE}/options/").data["entry_columns"]
        self.assertIn("amount", columns)
        self.assertIn("branch", columns)


class ColumnValuesTests(CashBookAPITestCase):
    """The list behind a column's filter button.

    It has to behave the way a spreadsheet's does, which is subtler than it
    looks: drawn from the whole book rather than the page on screen, narrowed
    by the OTHER columns' filters but never by its own.
    """

    def setUp(self):
        super().setUp()
        self.other_branch_row = CashBranch.objects.create(
            company=self.company, name="Water"
        )
        self.payment("100.00")
        self.payment("200.00")
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.OUT,
            amount=Decimal("300.00"),
            detail="Water spend",
            branch=self.other_branch_row,
            gl_account_code="5680024",
            gl_account_name="WATER EXPENSES",
            approver=self.approver,
        )

    def values(self, column, **params):
        self.as_user(self.viewer)
        response = self.client.get(
            f"{BASE}/entries/columns/", {"column": column, **params}
        )
        self.assertEqual(response.status_code, 200)
        return {row["value"]: row["count"] for row in response.data["values"]}

    def test_it_lists_the_values_with_a_count_each(self):
        self.assertEqual(self.values("branch"), {"Oil": 2, "Water": 1})

    def test_it_draws_from_the_whole_book_not_one_page(self):
        """A filter offering only page one's values would hide most options."""
        self.assertEqual(
            self.values("branch", page_size=1, page=1), {"Oil": 2, "Water": 1}
        )

    def test_another_columns_filter_narrows_the_list(self):
        self.assertEqual(
            self.values("gl", f_branch="Water"), {"WATER EXPENSES": 1}
        )

    def test_a_columns_own_filter_does_not_narrow_its_own_list(self):
        """Reopening a filter still offers what it is currently hiding."""
        self.assertEqual(
            self.values("branch", f_branch="Oil"), {"Oil": 2, "Water": 1}
        )

    def test_a_blank_is_offered_as_a_value_of_its_own(self):
        """A receipt has no branch, and "no branch" has to be tickable."""
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.IN,
            amount=Decimal("500.00"),
            detail="Cash receive by ATM card",
        )
        self.assertEqual(self.values("branch").get("\u2014"), 1)

    def test_a_blank_can_then_be_filtered_on(self):
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.IN,
            amount=Decimal("500.00"),
            detail="Cash receive by ATM card",
        )
        self.as_user(self.viewer)
        rows = self.client.get(
            f"{BASE}/entries/", {"f_branch": "\u2014"}
        ).data["results"]
        self.assertEqual([row["direction"] for row in rows], ["IN"])

    def test_an_unknown_column_is_named_rather_than_crashed_on(self):
        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/entries/columns/", {"column": "spaceship"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("branch", response.data["detail"])

    def test_it_is_scoped_to_the_company(self):
        self.payment("999.00", company=self.other_company)
        self.assertNotIn("999.00", self.values("amount"))


class AmountAndInColumnTests(CashBookAPITestCase):
    """The register's two money columns, and the filters behind them.

    Amount and In are one field read two ways: a payment is written in the
    Amount column and a receipt in the In column, so each is blank on the
    other's rows. The filters have to agree with that. They did not -- In had
    no filter at all, and ticking a figure under Amount also brought back a
    receipt of the same figure whose Amount cell was empty.
    """

    def setUp(self):
        super().setUp()
        self.paid = self.payment("1410.00")
        # The decisive fixture: the same figure, the other way round.
        self.received = services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.IN,
            amount=Decimal("1410.00"),
            detail="Cash receive by Atm card",
        )

    def ids(self, **params):
        self.as_user(self.viewer)
        rows = self.client.get(f"{BASE}/entries/", params).data["results"]
        return {row["id"] for row in rows}

    def values(self, column, **params):
        self.as_user(self.viewer)
        response = self.client.get(
            f"{BASE}/entries/columns/", {"column": column, **params}
        )
        return {row["value"]: row["count"] for row in response.data["values"]}

    def test_the_in_column_can_be_filtered_at_all(self):
        """It had no server-side column, so its header carried no funnel."""
        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/entries/columns/", {"column": "in"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["column"], "in")

    def test_filtering_amount_leaves_the_receipt_out(self):
        """Its Amount cell is blank, so its figure is not on that filter."""
        self.assertEqual(self.ids(f_amount="1410.00"), {self.paid.id})

    def test_filtering_in_leaves_the_payment_out(self):
        self.assertEqual(self.ids(f_in="1410.00"), {self.received.id})

    def test_a_receipt_reads_as_blank_under_amount(self):
        listed = self.values("amount")
        self.assertEqual(listed.get("1410.00"), 1, "the payment is the only one")
        self.assertEqual(listed.get("—"), 1, "the receipt reads blank here")

    def test_a_payment_reads_as_blank_under_in(self):
        listed = self.values("in")
        self.assertEqual(listed.get("1410.00"), 1)
        self.assertEqual(listed.get("—"), 1)

    def test_ticking_blank_under_in_gives_the_payments(self):
        self.assertEqual(self.ids(f_in="—"), {self.paid.id})

    def test_every_blank_row_counts_once_however_many_amounts_are_behind_it(self):
        """Receipts of different sizes are all one "(blank)" under Amount."""
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-05",
            direction=CashDirection.IN,
            amount=Decimal("50000.00"),
            detail="Cash receive by Atm card",
        )
        self.assertEqual(self.values("amount").get("—"), 2)


class PeoplePickerTests(CashBookAPITestCase):
    """Who can be offered an advance.

    The cash book's people are drivers, tradesmen and contractors: they have
    no login and no company membership, because they are not staff. A picker
    built from the company directory alone cannot see them -- so somebody
    already holding 804.00 could not be offered their next advance, which is
    how this was found.
    """

    def setUp(self):
        super().setUp()
        User = get_user_model()
        # Exactly what the importer makes: a person, not a login.
        self.manoj = User.objects.create(
            email="manoj@cash-book.local", full_name="Manoj", is_active=False
        )
        self.manoj.set_unusable_password()
        self.manoj.save()
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.manoj,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("804.00"),
            detail="Manoj ko deye",
        )

    def names(self, **params):
        self.as_user(self.custodian)
        return [row["name"] for row in self.client.get(f"{BASE}/people/", params).data]

    def test_a_cash_book_person_can_be_given_another_advance(self):
        self.assertIn("Manoj", self.names())

    def test_they_can_be_searched_for_by_name(self):
        self.assertIn("Manoj", self.names(search="manoj"))

    def test_the_staff_directory_is_still_there(self):
        """Widening it must not have replaced the ordinary list."""
        listed = self.names()
        self.assertIn("Manoj", listed)
        self.assertGreaterEqual(len(listed), 2, "the staff list went missing")

    def test_somebody_who_is_neither_is_still_left_out(self):
        """Belonging needs one of the two reasons, not neither.

        A user with no link to this company and no float in its book is a
        stranger to it, and widening the picker must not have swept in the
        whole user table.
        """
        stranger = get_user_model().objects.create(
            email="stranger@example.com", full_name="Stranger"
        )
        self.as_user(self.custodian)
        emails = [
            row["email"] for row in self.client.get(f"{BASE}/people/").data
        ]
        self.assertIn("manoj@cash-book.local", emails)
        self.assertNotIn(stranger.email, emails)

    def test_the_holding_list_is_unaffected(self):
        holding = self.names(holding="true")
        self.assertIn("Manoj", holding)


class AdvanceCancelEndpointTests(CashBookAPITestCase):
    """Taking a movement back out of somebody's ledger, over HTTP.

    The service was covered; the endpoint the button calls was not, and it is
    the part that has to refuse the wrong caller and the wrong company.
    """

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.holder = User.objects.create(
            email="holder@cash-book.local", full_name="Holder", is_active=False
        )
        self.given = services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.holder,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
            detail="Cash given",
        )

    def balance(self):
        return services.advance_balance(self.company, self.holder)

    def test_it_comes_off_what_they_are_holding(self):
        self.as_user(self.custodian)
        response = self.client.delete(f"{BASE}/advances/{self.given.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_the_row_is_kept_rather_than_deleted(self):
        """The ledger should still show it happened and was taken back."""
        self.as_user(self.custodian)
        self.client.delete(f"{BASE}/advances/{self.given.id}/")
        self.given.refresh_from_db()
        self.assertFalse(self.given.is_active)

    def test_a_viewer_cannot_take_one_out(self):
        self.as_user(self.viewer)
        response = self.client.delete(f"{BASE}/advances/{self.given.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.balance(), Decimal("15000.00"))

    def test_another_company_cannot_reach_it(self):
        """Company scoping is the endpoint's, not the caller's word for it."""
        self.as_user(self.custodian, company=self.other_company)
        response = self.client.delete(f"{BASE}/advances/{self.given.id}/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.balance(), Decimal("15000.00"))

    def test_taking_it_out_twice_is_harmless(self):
        self.as_user(self.custodian)
        self.client.delete(f"{BASE}/advances/{self.given.id}/")
        response = self.client.delete(f"{BASE}/advances/{self.given.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.balance(), Decimal("0.00"))


class TakenOutRowsAreStillReadableTests(CashBookAPITestCase):
    """Seeing what was taken out of somebody's ledger.

    The register has "Show cancelled" and the ledger had nothing: a row taken
    out simply vanished, so there was no way to check what had been removed or
    to see why an account stopped adding up.
    """

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.holder = User.objects.create(
            email="holder@cash-book.local", full_name="Holder", is_active=False
        )
        self.kept = services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.holder,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("5000.00"),
            detail="Kept",
        )
        self.taken_out = services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.holder,
            entry_date="2026-06-05",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("2000.00"),
            detail="Recorded by mistake",
        )
        services.cancel_advance(user=self.custodian, entry=self.taken_out)

    def statement(self, **params):
        self.as_user(self.viewer)
        return self.client.get(
            f"{BASE}/advances/holders/{self.holder.id}/", params
        ).data

    def test_it_is_hidden_by_default(self):
        rows = self.statement()["movements"]
        self.assertEqual([row["detail"] for row in rows], ["Kept"])

    def test_asking_for_it_brings_it_back(self):
        rows = self.statement(include_cancelled="true")["movements"]
        self.assertEqual(
            sorted(row["detail"] for row in rows), ["Kept", "Recorded by mistake"]
        )

    def test_it_is_marked_so_the_screen_can_strike_it_through(self):
        rows = self.statement(include_cancelled="true")["movements"]
        removed = next(r for r in rows if r["detail"] == "Recorded by mistake")
        self.assertFalse(removed["is_active"])

    def test_it_does_not_move_the_running_balance(self):
        """The rule the register follows: a row out of the book moves nothing."""
        rows = self.statement(include_cancelled="true")["movements"]
        self.assertEqual(
            [Decimal(row["balance_after"]) for row in rows],
            [Decimal("5000.00"), Decimal("5000.00")],
        )

    def test_the_balance_is_the_same_either_way(self):
        """Ticking a box to read history must not restate what they hold."""
        self.assertEqual(
            self.statement()["balance"],
            self.statement(include_cancelled="true")["balance"],
        )
        self.assertEqual(Decimal(self.statement()["balance"]), Decimal("5000.00"))


class AddingAPersonFromTheFormTests(CashBookAPITestCase):
    """Adding somebody to hold cash, at the moment cash is handed over.

    The book's people are drivers and tradesmen with no login, and the
    custodian meets them at the form rather than at an admin screen. The rule
    that matters is the one about not making a second one: ten duplicates
    reached the live book from an import that matched only exact full names,
    and a button offered to anybody typing a name is a faster way to make more.
    """

    def add(self, name, user=None):
        self.as_user(user or self.custodian)
        return self.client.post(f"{BASE}/people/new/", {"name": name}, format="json")

    def test_it_creates_somebody_who_can_hold_cash_but_not_sign_in(self):
        response = self.add("Ravi Kumar")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["created"])

        person = get_user_model().objects.get(id=response.data["id"])
        self.assertEqual(person.full_name, "Ravi Kumar")
        self.assertFalse(person.is_active)
        self.assertFalse(person.has_usable_password())
        self.assertTrue(person.email.endswith("@cash-book.local"))

    def test_they_can_be_given_cash_straight_away(self):
        person = get_user_model().objects.get(id=self.add("Ravi Kumar").data["id"])
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=person,
            entry_date="2026-06-04",
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("500.00"),
        )
        self.assertEqual(
            services.advance_balance(self.company, person), Decimal("500.00")
        )

    def test_the_same_name_does_not_make_a_second_person(self):
        first = self.add("Ravi Kumar")
        again = self.add("ravi kumar")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.data["created"])
        self.assertEqual(again.data["id"], first.data["id"])

    def test_spacing_is_not_a_different_person(self):
        first = self.add("Ravi Kumar")
        again = self.add("  Ravi   Kumar ")
        self.assertEqual(again.data["id"], first.data["id"])

    def test_it_returns_a_member_of_staff_rather_than_shadowing_them(self):
        """The exact failure that put ten duplicates on the live book."""
        staff = get_user_model().objects.create(
            email="gurnam@jivo.in", full_name="Gurnam Singh"
        )
        response = self.add("Gurnam Singh")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])
        self.assertEqual(response.data["id"], staff.id)

    def test_a_blank_name_is_refused(self):
        self.assertEqual(self.add("   ").status_code, 400)

    def test_a_viewer_cannot_add_people(self):
        self.assertEqual(self.add("Ravi Kumar", user=self.viewer).status_code, 403)
        self.assertFalse(
            get_user_model().objects.filter(full_name="Ravi Kumar").exists()
        )


class AddressingAPaymentToAnApproverTests(CashBookAPITestCase):
    """Saying who should agree to a payment, and holding them to it.

    An approval that belongs to everybody belongs to nobody: the queue was a
    shared pile, and the custodian could not say who had agreed to what. A
    payment now names the person it is being sent to, and only they can decide
    it.
    """

    def post(self, **overrides):
        self.as_user(self.custodian)
        payload = {
            "entry_date": "2026-06-04",
            "direction": "OUT",
            "amount": "6000.00",
            "branch": self.branch.id,
            "gl_account_code": "5630004",
            "gl_account_name": "REFRESHMENT",
            "detail": "Cash paid to Ravi kumar",
            "approver": self.approver.id,
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        with patch("cash_book.views.GLAccountReader") as reader:
            reader.return_value.resolve.return_value = {
                "account_code": "5630004",
                "account_name": "REFRESHMENT",
            }
            return self.client.post(f"{BASE}/entries/", payload, format="json")

    # --- the choice itself ------------------------------------------------

    def test_a_payment_must_say_who_should_approve_it(self):
        response = self.post(approver=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn("approver", response.data)
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_it_keeps_who_it_was_sent_to(self):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["approver"], self.approver.id)
        entry = CashEntry.objects.get(id=response.data["id"])
        self.assertEqual(entry.approver_id, self.approver.id)

    def test_somebody_who_cannot_approve_is_refused(self):
        """Otherwise the payment waits forever on a person with no power."""
        response = self.post(approver=self.viewer.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("approver", response.data)
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_a_receipt_is_not_approved_by_anybody(self):
        response = self.post(
            direction="IN",
            branch=None,
            gl_account_code=None,
            gl_account_name=None,
            detail="Cash receive by ATM card",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("approver", response.data)

    # --- who may decide it ------------------------------------------------

    def test_only_the_person_it_was_sent_to_can_decide_it(self):
        entry = CashEntry.objects.get(id=self.post().data["id"])
        other = self._user(
            "other-approver@example.com",
            ["can_view_cash_book", "can_approve_cash_entries"],
        )

        self.as_user(other)
        response = self.client.post(
            f"{BASE}/entries/decide/", {"entry_ids": [entry.id]}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        entry.refresh_from_db()
        self.assertEqual(entry.approval_state, "PENDING")

        self.as_user(self.approver)
        response = self.client.post(
            f"{BASE}/entries/decide/", {"entry_ids": [entry.id]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.approval_state, "APPROVED")

    def test_an_unaddressed_payment_stays_open_to_any_approver(self):
        """The imported book named nobody; stranding it would be worse."""
        entry = services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-06-04",
            direction=CashDirection.OUT,
            amount=Decimal("100.00"),
            detail="From the sheet",
            branch=self.branch,
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
            require_approver=False,
        )
        self.assertIsNone(entry.approver_id)

        self.as_user(self.approver)
        response = self.client.post(
            f"{BASE}/entries/decide/", {"entry_ids": [entry.id]}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    # --- the queue --------------------------------------------------------

    def test_the_queue_is_the_approver_s_own_work(self):
        mine = CashEntry.objects.get(id=self.post().data["id"])
        other = self._user(
            "other-approver@example.com",
            ["can_view_cash_book", "can_approve_cash_entries"],
        )
        theirs = CashEntry.objects.get(
            id=self.post(approver=other.id, detail="Theirs").data["id"]
        )

        self.as_user(self.approver)
        listed = {
            row["id"]
            for row in self.client.get(f"{BASE}/approvals/").data["results"]
        }
        self.assertIn(mine.id, listed)
        self.assertNotIn(theirs.id, listed, "saw somebody else's payment")

    # --- who can be picked ------------------------------------------------

    def test_the_approver_list_is_people_made_approvers(self):
        self.as_user(self.custodian)
        rows = self.client.get(f"{BASE}/approvers/").data
        emails = [row["email"] for row in rows]
        self.assertIn(self.approver.email, emails)
        self.assertNotIn(self.custodian.email, emails)
        self.assertNotIn(self.viewer.email, emails)

    def test_a_superuser_is_not_offered_just_for_being_one(self):
        """On the live book that would be thirteen IT and developer logins."""
        root = User.objects.create(email="root@example.com", is_superuser=True)
        UserCompany.objects.create(user=root, company=self.company, role=self.role)
        self.assertTrue(root.has_perm("cash_book.can_approve_cash_entries"))

        self.as_user(self.custodian)
        emails = [row["email"] for row in self.client.get(f"{BASE}/approvers/").data]
        self.assertNotIn(root.email, emails)
