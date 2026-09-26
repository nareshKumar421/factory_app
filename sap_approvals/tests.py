"""
SAP Approvals through the real permission stack.

    python manage.py test sap_approvals --settings=config.sqlite_test_settings

Every request goes through IsAuthenticated + HasCompanyContext + the app's own
right. SAP is mocked where the views look ``SAPClient`` up. What is pinned:

* the list/detail/badge read the caller's OWN mapped SAP user and nobody
  else's, and rows carry the flags the page follows;
* the decision guards run in the ported order — 409 stale, 403 unmapped, 403
  not the authorizer, 409 duplicate, 400 nothing to sign with — each before SAP;
* a typed SAP password reaches ``SAPClient`` and nothing else: not the
  response, not the audit row, not a log line;
* the withdraw rules, the audit trail, the rights and the group command.
"""

import logging
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.models import SapApproverIdentity

from . import permissions as guards
from .management.commands.setup_sap_approvals_groups import SAP_APPROVALS_GROUPS
from .models import SapApprovalDecision

BASE = "/api/v1/sap-approvals/"
ALL_PERMISSIONS = [
    "can_view_sap_approval_inbox",
    "can_decide_sap_approvals",
    "can_withdraw_own_sap_approvals",
]
TYPED = "Typed-S3cret!pw"


def decision_url(code):
    return f"{BASE}requests/{code}/decision/"


def withdraw_url(code):
    return f"{BASE}requests/{code}/withdraw/"


def _row(**overrides):
    """One pending A/R credit note waiting on USER37, raised by USER12."""
    row = {
        "wdd_code": 75424,
        "object_type": "14",
        "object_type_label": "A/R Credit Note",
        "draft_entry": 57198,
        "is_draft": True,
        "status": "PENDING",
        "stale_pending": False,
        "superseded": False,
        "current_step": 20,
        "template_code": 106,
        "template_name": "USER37 RETURNS",
        "remarks": None,
        "created_at": "2026-09-16T10:05:00",
        "originator_code": "USER12",
        "originator_name": "ATUL SHARMA",
        "approver_code": "USER37",
        "approver_name": "HONEY SINGH",
        "decided_by": None,
        "decided_by_name": None,
        "decided_at": None,
        "rejection_reason": None,
        "waiting_on_me": True,
        "document": {"doc_num": 626092648, "card_code": "CUSTA000844",
                     "party_name": "ILAHI CO.", "total_amount": "17455.00"},
        "request_count": 1,
        "pending_request_count": 1,
        "sibling_requests": [],
        "already_posted_as": None,
        "duplicate_of_posted": [],
        "posted_duplicates": [],
        "is_duplicate": False,
    }
    row.update(overrides)
    return row


def _stage(**overrides):
    stage = _row(authorizer_codes=["USER37"])
    stage.update(overrides)
    return stage


POSTED = {"doc_entry": 1963, "doc_num": 626096824, "doc_date": "2026-09-16",
          "total_amount": "17455.00", "currency": "INR", "table": "ORIN",
          "from_draft_entry": 57100}


class _CaptureAllLogs(logging.Handler):
    """Every record, every logger, every level — to prove what never appears."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


class SapApprovalsTestCase(APITestCase):
    """One company, one user mapped to USER37, and whatever rights a test grants."""

    permissions = ALL_PERMISSIONS
    mapped = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="honey@example.com",
            password="testpass",
            full_name="Honey Singh",
            employee_code="E-37",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Staff")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        if self.mapped:
            SapApproverIdentity.objects.create(
                user=self.user, company=self.company,
                sap_user_code="USER37", sap_user_name="HONEY SINGH",
            )
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.grant(*self.permissions)

    def grant(self, *codenames):
        """Add rights, then re-fetch the user: has_perm() caches per instance."""
        if codenames:
            self.user.user_permissions.add(
                *Permission.objects.filter(
                    content_type__app_label="sap_approvals", codename__in=codenames
                )
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def revoke(self, *codenames):
        self.user.user_permissions.remove(
            *Permission.objects.filter(
                content_type__app_label="sap_approvals", codename__in=codenames
            )
        )
        self.grant()

    def unmap(self):
        SapApproverIdentity.objects.filter(user=self.user).delete()


# ---------------------------------------------------------------------------
# List, detail, badge
# ---------------------------------------------------------------------------


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "stored"}})
@patch("sap_approvals.views.SAPClient")
class ListApiTests(SapApprovalsTestCase):
    def test_the_list_reads_the_callers_own_sap_user_with_the_filters(self, sap):
        sap.return_value.list_approval_inbox.return_value = []
        response = self.client.get(
            f"{BASE}requests/?scope=raised_by_me&status=REJECTED&object_type=18"
            "&date_from=2026-09-01&date_to=2026-09-30&search=ilahi",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        sap.assert_called_once_with(company_code="JIVO_OIL")
        args, kwargs = sap.return_value.list_approval_inbox.call_args
        self.assertEqual(args, ("USER37",))
        self.assertEqual(kwargs["scope"], "raised_by_me")
        self.assertEqual(kwargs["status"], "REJECTED")
        self.assertEqual(kwargs["object_type"], "18")
        self.assertEqual(str(kwargs["date_from"]), "2026-09-01")
        self.assertEqual(kwargs["search"], "ilahi")
        self.assertEqual(response.data["identity"], {
            "sap_user_code": "USER37", "credentials_configured": True,
        })
        self.assertIn({"code": "59", "label": "Goods Receipt"}, response.data["object_types"])

    def test_all_statuses_is_no_status_filter(self, sap):
        sap.return_value.list_approval_inbox.return_value = []
        self.client.get(f"{BASE}requests/?status=ALL", **self.headers)
        self.assertIsNone(sap.return_value.list_approval_inbox.call_args.kwargs["status"])

    def test_bad_filters_are_refused_before_sap(self, sap):
        for query in ("status=DONE", "scope=everyone", "object_type=1;DROP",
                      "date_from=2026-09-30&date_to=2026-09-01", "limit=9999"):
            with self.subTest(query=query):
                response = self.client.get(f"{BASE}requests/?{query}", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sap.return_value.list_approval_inbox.assert_not_called()

    def test_rows_carry_what_the_caller_can_do(self, sap):
        mine = _row()
        others = _row(wdd_code=2, waiting_on_me=False, approver_code="USER24")
        raised = _row(wdd_code=3, waiting_on_me=False, originator_code="user37")
        decided = _row(wdd_code=4, status="APPROVED", waiting_on_me=True, originator_code="USER37")
        sap.return_value.list_approval_inbox.return_value = [mine, others, raised, decided]
        rows = self.client.get(f"{BASE}requests/", **self.headers).data["results"]
        flags = [(r["is_mine"], r["can_decide"], r["is_originator"], r["can_withdraw"]) for r in rows]
        self.assertEqual(flags, [
            (True, True, False, False),
            (False, False, False, False),
            (False, False, True, True),
            # Decided: nothing left to decide or withdraw (re-deciding is refused).
            (False, False, True, False),
        ])
        self.assertTrue(all(r["credentials_configured"] for r in rows))

    def test_viewing_does_not_imply_deciding_or_withdrawing(self, sap):
        self.revoke("can_decide_sap_approvals", "can_withdraw_own_sap_approvals")
        sap.return_value.list_approval_inbox.return_value = [_row(originator_code="USER37")]
        row = self.client.get(f"{BASE}requests/", **self.headers).data["results"][0]
        self.assertTrue(row["is_mine"])
        self.assertFalse(row["can_decide"])
        self.assertFalse(row["can_withdraw"])

    def test_a_full_page_says_it_may_be_truncated(self, sap):
        sap.return_value.list_approval_inbox.return_value = [_row(wdd_code=n) for n in range(2)]
        data = self.client.get(f"{BASE}requests/?limit=2", **self.headers).data
        self.assertTrue(data["truncated"])

    def test_an_unmapped_user_gets_an_explanation_not_someone_elses_list(self, sap):
        self.unmap()
        response = self.client.get(f"{BASE}requests/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"], [])
        self.assertIsNone(response.data["identity"])
        self.assertIn("SAP Identities", response.data["message"])
        sap.return_value.list_approval_inbox.assert_not_called()

    def test_a_mapping_in_another_company_does_not_count(self, sap):
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        UserCompany.objects.create(user=self.user, company=other, role=self.role)
        response = self.client.get(f"{BASE}requests/", HTTP_COMPANY_CODE="JIVO_MART")
        self.assertIsNone(response.data["identity"])
        sap.return_value.list_approval_inbox.assert_not_called()

    def test_sap_outage_is_503_and_broken_sap_is_502(self, sap):
        sap.return_value.list_approval_inbox.side_effect = SAPConnectionError("down")
        self.assertEqual(
            self.client.get(f"{BASE}requests/", **self.headers).status_code,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        sap.return_value.list_approval_inbox.side_effect = SAPDataError("bad")
        self.assertEqual(
            self.client.get(f"{BASE}requests/", **self.headers).status_code,
            status.HTTP_502_BAD_GATEWAY,
        )


@patch("sap_approvals.views.SAPClient")
class DetailApiTests(SapApprovalsTestCase):
    def _detail(self, **overrides):
        stages = [
            {"step_code": 6, "user_code": "USER26", "status": "APPROVED", "is_current": False},
            {"step_code": 20, "user_code": "USER37", "status": "PENDING", "is_current": True},
        ]
        return _row(stages=overrides.pop("stages", stages), lines=[], **overrides)

    def test_the_authorizer_of_the_current_stage_opens_it(self, sap):
        sap.return_value.approval_inbox_detail.return_value = self._detail()
        response = self.client.get(f"{BASE}requests/75424/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(sap.return_value.approval_inbox_detail.call_args[0], (75424, "USER37"))
        self.assertTrue(response.data["can_decide"])

    def test_the_originator_opens_their_own(self, sap):
        sap.return_value.approval_inbox_detail.return_value = self._detail(
            originator_code="USER37", stages=[]
        )
        self.assertEqual(
            self.client.get(f"{BASE}requests/75424/", **self.headers).status_code,
            status.HTTP_200_OK,
        )

    def test_a_pending_request_waiting_at_someone_elses_stage_is_closed(self, sap):
        """Your line exists but is not the current stage: the portal hid it too."""
        sap.return_value.approval_inbox_detail.return_value = self._detail(
            stages=[
                {"step_code": 6, "user_code": "USER26", "status": "PENDING", "is_current": True},
                {"step_code": 20, "user_code": "USER37", "status": "PENDING", "is_current": False},
            ],
        )
        self.assertEqual(
            self.client.get(f"{BASE}requests/75424/", **self.headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_decided_request_opens_for_anyone_on_a_line(self, sap):
        sap.return_value.approval_inbox_detail.return_value = self._detail(
            status="APPROVED",
            stages=[{"step_code": 6, "user_code": "USER37", "status": "APPROVED", "is_current": True}],
        )
        self.assertEqual(
            self.client.get(f"{BASE}requests/75424/", **self.headers).status_code,
            status.HTTP_200_OK,
        )

    def test_missing_is_404_and_unmapped_is_403(self, sap):
        sap.return_value.approval_inbox_detail.return_value = None
        self.assertEqual(
            self.client.get(f"{BASE}requests/1/", **self.headers).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.unmap()
        self.assertEqual(
            self.client.get(f"{BASE}requests/1/", **self.headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )


@patch("sap_approvals.views.SAPClient")
class PendingCountApiTests(SapApprovalsTestCase):
    def test_the_badge_counts_what_waits_on_the_caller(self, sap):
        sap.return_value.count_approval_inbox_waiting.return_value = 7
        response = self.client.get(f"{BASE}pending-count/", **self.headers)
        self.assertEqual(response.data, {"total": 7})
        sap.return_value.count_approval_inbox_waiting.assert_called_once_with("USER37")

    def test_an_unmapped_user_counts_zero_without_asking_sap(self, sap):
        self.unmap()
        self.assertEqual(self.client.get(f"{BASE}pending-count/", **self.headers).data, {"total": 0})
        sap.return_value.count_approval_inbox_waiting.assert_not_called()


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "stored"}})
@patch("sap_approvals.views.SAPClient")
class DecisionGuardTests(SapApprovalsTestCase):
    def _post(self, body, code=75424):
        return self.client.post(decision_url(code), body, format="json", **self.headers)

    def test_1_a_request_no_longer_pending_is_409_even_before_identity(self, sap):
        """A leftover is reported first — even to someone who could not decide it."""
        self.unmap()
        sap.return_value.approval_inbox_stage.return_value = _stage(
            status="CANCELLED", stale_pending=True
        )
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "STALE_REQUEST")
        self.assertIn("was cancelled", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_re_deciding_an_approved_request_is_refused(self, sap):
        """The portal allowed it; JI does not (open business decision)."""
        sap.return_value.approval_inbox_stage.return_value = _stage(status="APPROVED")
        response = self._post({"approve": False, "remarks": "changed my mind"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("already approved", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_2_an_unmapped_user_is_403(self, sap):
        self.unmap()
        sap.return_value.approval_inbox_stage.return_value = _stage()
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("SAP Identities", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_3_someone_else_s_stage_is_403_even_with_a_typed_password(self, sap):
        """A typed password never lets anyone act as someone else."""
        sap.return_value.approval_inbox_stage.return_value = _stage(
            authorizer_codes=["USER24"], approver_code="USER24", approver_name="PANKAJ"
        )
        response = self._post({"approve": True, "sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("USER24 (PANKAJ)", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_4_approving_a_posted_duplicate_is_409_until_confirmed(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(posted_duplicates=[POSTED])
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "DUPLICATE_DOCUMENT")
        self.assertEqual(response.data["duplicate_of"][0]["doc_num"], 626096824)
        self.assertIn("A/R Credit Note #626096824", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()
        # The approve path asks for the duplicate check; the reader fails it closed.
        self.assertTrue(sap.return_value.approval_inbox_stage.call_args.kwargs["with_duplicates"])

    def test_a_confirmed_duplicate_goes_through_and_is_recorded(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(posted_duplicates=[POSTED])
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        with self.assertLogs("sap_approvals.views", level="WARNING") as logs:
            response = self._post({"approve": True, "confirm_duplicate": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("posted duplicate", logs.output[0])
        self.assertTrue(SapApprovalDecision.objects.get().confirmed_duplicate)

    def test_rejecting_a_duplicate_is_never_blocked(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(posted_duplicates=[POSTED])
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self._post({"approve": False, "remarks": "duplicate"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(sap.return_value.approval_inbox_stage.call_args.kwargs["with_duplicates"])

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_5_nothing_to_sign_with_is_400(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        response = self._post({"approve": True, "sap_password": ""})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Type your SAP password", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_the_stored_password_signs_when_none_is_typed(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {
            "message": "A/R Credit Note approved in SAP.", "signed_as": "USER37",
        }
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kwargs = sap.return_value.decide_approval_request.call_args.kwargs
        self.assertIsNone(kwargs["password"])
        self.assertEqual(kwargs["approver"], "USER37")
        self.assertTrue(kwargs["approve"])
        self.assertIn("Honey Singh", kwargs["remarks"])
        self.assertEqual(kwargs["subject"], "A/R Credit Note")
        audit = SapApprovalDecision.objects.get()
        self.assertEqual((audit.action, audit.signed_as, audit.typed_password),
                         ("APPROVE", "USER37", False))
        self.assertEqual(audit.created_by, self.user)

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_a_typed_password_reaches_sap_and_nothing_else(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {
            "message": "A/R Credit Note approved in SAP.", "signed_as": "USER37",
        }
        capture = _CaptureAllLogs()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            response = self._post({"approve": True, "remarks": "ok", "sap_password": TYPED})
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            sap.return_value.decide_approval_request.call_args.kwargs["password"], TYPED
        )
        self.assertNotIn(TYPED, response.content.decode())
        self.assertFalse(any(TYPED in line for line in capture.lines))
        audit = SapApprovalDecision.objects.get()
        self.assertTrue(audit.typed_password)
        for field in SapApprovalDecision._meta.fields:
            self.assertNotIn(TYPED, str(getattr(audit, field.attname)))

    def test_a_typed_password_is_used_as_typed_not_trimmed(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        self._post({"approve": True, "sap_password": " pw with spaces "})
        self.assertEqual(
            sap.return_value.decide_approval_request.call_args.kwargs["password"],
            " pw with spaces ",
        )

    def test_a_password_sap_refuses_is_400_and_not_echoed(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.side_effect = SAPValidationError(
            "SAP refused the Service Layer login for user 'USER37': (-304) Fail to get DB"
        )
        response = self._post({"approve": True, "sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn(TYPED, response.content.decode())
        self.assertFalse(SapApprovalDecision.objects.exists())

    def test_the_body_cannot_choose_who_signs(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        self._post({"approve": True, "approver": "USER24", "approver_code": "USER24"})
        self.assertEqual(sap.return_value.decide_approval_request.call_args.kwargs["approver"], "USER37")

    def test_a_second_authorizer_on_the_stage_may_sign_as_themselves(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(
            authorizer_codes=["USER26", "User37"], approver_code="USER26"
        )
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "User37"}
        self.assertEqual(self._post({"approve": True}).status_code, status.HTTP_200_OK)
        # Signed as SAP spells it on the stage.
        self.assertEqual(sap.return_value.decide_approval_request.call_args.kwargs["approver"], "User37")

    def test_reject_needs_a_reason_which_carries_the_actor(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        self.assertEqual(self._post({"approve": False}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self._post({"approve": False, "remarks": "wrong rate"}).status_code, status.HTTP_200_OK
        )
        remarks = sap.return_value.decide_approval_request.call_args.kwargs["remarks"]
        self.assertTrue(remarks.startswith("wrong rate — Honey Singh"))
        self.assertEqual(SapApprovalDecision.objects.get().action, "REJECT")

    def test_a_missing_request_is_404(self, sap):
        sap.return_value.approval_inbox_stage.return_value = None
        self.assertEqual(self._post({"approve": True}).status_code, status.HTTP_404_NOT_FOUND)

    def test_a_stage_with_no_authorizer_is_refused(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(authorizer_codes=[])
        self.assertEqual(self._post({"approve": True}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_failed_audit_write_never_undoes_the_sap_decision(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        with patch("sap_approvals.views.SapApprovalDecision.objects.create", side_effect=RuntimeError):
            with self.assertLogs("sap_approvals.views", level="ERROR"):
                response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Withdrawing
# ---------------------------------------------------------------------------


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "stored"}})
@patch("sap_approvals.views.SAPClient")
class WithdrawTests(SapApprovalsTestCase):
    def _mine(self, **overrides):
        return _stage(originator_code="USER37", waiting_on_me=False, **overrides)

    def _post(self, body=None):
        return self.client.post(withdraw_url(75424), body or {}, format="json", **self.headers)

    def test_the_originator_withdraws_with_the_stored_password(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._mine()
        sap.return_value.withdraw_approval_request.return_value = {
            "message": "A/R Credit Note withdrawn in SAP.", "signed_as": "USER37",
        }
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        args, kwargs = sap.return_value.withdraw_approval_request.call_args
        self.assertEqual(args, (75424,))
        self.assertEqual(kwargs["originator"], "USER37")
        self.assertIsNone(kwargs["password"])
        audit = SapApprovalDecision.objects.get()
        self.assertEqual((audit.action, audit.signed_as), ("WITHDRAW", "USER37"))

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_a_typed_password_withdraws_and_is_not_kept(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._mine()
        sap.return_value.withdraw_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self._post({"sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(sap.return_value.withdraw_approval_request.call_args.kwargs["password"], TYPED)
        self.assertNotIn(TYPED, response.content.decode())
        self.assertTrue(SapApprovalDecision.objects.get().typed_password)

    def test_only_a_pending_request_can_be_withdrawn(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._mine(status="REJECTED")
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "STALE_REQUEST")
        sap.return_value.withdraw_approval_request.assert_not_called()

    def test_an_unmapped_user_cannot_withdraw(self, sap):
        self.unmap()
        sap.return_value.approval_inbox_stage.return_value = self._mine()
        self.assertEqual(self._post().status_code, status.HTTP_403_FORBIDDEN)
        sap.return_value.withdraw_approval_request.assert_not_called()

    def test_only_the_originator_can_withdraw(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage(originator_code="USER12")
        response = self._post({"sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("USER12", response.data["error"])
        sap.return_value.withdraw_approval_request.assert_not_called()

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_nothing_to_sign_with_is_400(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._mine()
        self.assertEqual(self._post().status_code, status.HTTP_400_BAD_REQUEST)
        sap.return_value.withdraw_approval_request.assert_not_called()

    def test_deciding_does_not_grant_withdrawing(self, sap):
        self.revoke("can_withdraw_own_sap_approvals")
        self.assertEqual(self._post().status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------


@patch("sap_approvals.views.SAPClient")
class PermissionTests(SapApprovalsTestCase):
    permissions = []

    def test_missing_company_header_is_refused(self, sap):
        self.grant(*ALL_PERMISSIONS)
        self.assertEqual(self.client.get(f"{BASE}requests/").status_code, status.HTTP_403_FORBIDDEN)

    def test_no_rights_is_refused_everywhere(self, sap):
        for method, path in (
            ("get", "requests/"), ("get", "requests/1/"), ("get", "pending-count/"),
            ("post", "requests/1/decision/"), ("post", "requests/1/withdraw/"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(f"{BASE}{path}", **self.headers)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap.assert_not_called()

    def test_viewers_read_but_cannot_act(self, sap):
        sap.return_value.list_approval_inbox.return_value = []
        self.grant("can_view_sap_approval_inbox")
        self.assertEqual(self.client.get(f"{BASE}requests/", **self.headers).status_code, status.HTTP_200_OK)
        for path in ("requests/1/decision/", "requests/1/withdraw/"):
            self.assertEqual(
                self.client.post(f"{BASE}{path}", {"approve": True}, format="json", **self.headers).status_code,
                status.HTTP_403_FORBIDDEN,
            )

    def test_deciding_implies_viewing(self, sap):
        sap.return_value.list_approval_inbox.return_value = []
        self.grant("can_decide_sap_approvals")
        self.assertEqual(self.client.get(f"{BASE}requests/", **self.headers).status_code, status.HTTP_200_OK)

    def test_withdrawing_implies_viewing_but_not_deciding(self, sap):
        sap.return_value.count_approval_inbox_waiting.return_value = 0
        self.grant("can_withdraw_own_sap_approvals")
        self.assertEqual(self.client.get(f"{BASE}pending-count/", **self.headers).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.post(decision_url(1), {"approve": True}, format="json", **self.headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class PermissionSurfaceTests(TestCase):
    def test_only_the_declared_rights_exist(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="sap_approvals").values_list(
                "codename", flat=True
            )
        )
        self.assertEqual(codenames, set(ALL_PERMISSIONS))


class GroupCommandTests(TestCase):
    """Every group right exists, and every right the API checks is handed out."""

    def setUp(self):
        call_command("setup_sap_approvals_groups", stdout=StringIO())

    def test_every_group_is_created_with_its_rights(self):
        for name, codes in SAP_APPROVALS_GROUPS.items():
            with self.subTest(group=name):
                held = {
                    f"sap_approvals.{codename}"
                    for codename in Group.objects.get(name=name).permissions.values_list(
                        "codename", flat=True
                    )
                }
                self.assertEqual(held, set(codes))

    def test_every_right_the_api_checks_is_granted_by_some_group(self):
        granted = {code for codes in SAP_APPROVALS_GROUPS.values() for code in codes}
        checked = {guards.VIEW_PERMISSION, guards.DECIDE_PERMISSION, guards.WITHDRAW_PERMISSION}
        self.assertEqual(checked - granted, set())

    def test_a_rerun_changes_nothing_and_adds_no_members(self):
        before = {g.name: set(g.permissions.all()) for g in Group.objects.all()}
        call_command("setup_sap_approvals_groups", stdout=StringIO())
        after = {g.name: set(g.permissions.all()) for g in Group.objects.all()}
        self.assertEqual(before, after)
        for name in SAP_APPROVALS_GROUPS:
            self.assertFalse(Group.objects.get(name=name).user_set.exists())
