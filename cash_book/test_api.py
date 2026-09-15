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
from rest_framework.test import APITestCase

from accounts.models import Department
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError

from . import services
from .models import BunchStatus, CashDirection, CashEntry

User = get_user_model()

BASE = "/api/v1/cash-book"


class CashBookAPITestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.role = UserRole.objects.create(name="Accounts")
        cls.department = Department.objects.create(name="Canola")

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
        return services.record_entry(
            user=self.custodian,
            company=company or self.company,
            entry_date="2026-06-04",
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            detail="Cash paid to Ravi kumar for refreshment",
            department=self.department,
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
            [row["name"] for row in response.data["departments"]], ["Canola"]
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
                    "department": self.department.id,
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
                    "department": self.department.id,
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
                    "department": self.department.id,
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
