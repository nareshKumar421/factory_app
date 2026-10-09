"""
SAP Approvals through the real permission stack.

    python manage.py test sap_approvals --settings=config.sqlite_test_settings

Every request goes through IsAuthenticated + HasCompanyContext + the app's own
right. SAP is mocked where the views look ``SAPClient`` up. What is pinned:

* the list/detail/badge read the caller's OWN mapped SAP user and nobody
  else's, and rows carry the flags the page follows;
* the decision guards run in the ported order — 409 stale, 403 unmapped, 403
  not the authorizer, 409 duplicate, 400 nothing to sign with — each before SAP;
* a decision already taken can be changed (approved ↔ rejected) only by the
  SAP user who took it, never on a posted, withdrawn or leftover request, with
  the same duplicate and password guards, and the audit says what it changed;
* a typed SAP password reaches ``SAPClient`` and nothing else: not the
  response, not the audit row, not a log line;
* the withdraw rules, the audit trail, the rights and the group command.
"""

import logging
from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
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
        approved_by_me = _row(wdd_code=5, status="APPROVED", waiting_on_me=False, decided_by="user37")
        rejected_by_me = _row(wdd_code=6, status="REJECTED", waiting_on_me=False, decided_by="USER37")
        approved_by_other = _row(wdd_code=7, status="APPROVED", waiting_on_me=False, decided_by="USER24")
        posted_by_me = _row(wdd_code=8, status="GENERATED", waiting_on_me=False, decided_by="USER37")
        leftover = _row(wdd_code=9, status="APPROVED", stale_pending=True, waiting_on_me=False,
                        decided_by="USER37")
        sap.return_value.list_approval_inbox.return_value = [
            mine, others, raised, decided,
            approved_by_me, rejected_by_me, approved_by_other, posted_by_me, leftover,
        ]
        rows = self.client.get(f"{BASE}requests/", **self.headers).data["results"]
        flags = [
            (r["is_mine"], r["can_decide"], r["can_change_decision"],
             r["is_originator"], r["can_withdraw"])
            for r in rows
        ]
        self.assertEqual(flags, [
            (True, True, False, False, False),
            (False, False, False, False, False),
            (False, False, False, True, True),
            # Decided, but SAP names nobody as the decider: nothing to change.
            (False, False, False, True, False),
            # Only the one who decided may change it, and only until it posts.
            (False, False, True, False, False),
            (False, False, True, False, False),
            (False, False, False, False, False),
            (False, False, False, False, False),
            (False, False, False, False, False),
        ])
        self.assertTrue(all(r["credentials_configured"] for r in rows))

    def test_viewing_does_not_imply_deciding_or_withdrawing(self, sap):
        self.revoke("can_decide_sap_approvals", "can_withdraw_own_sap_approvals")
        sap.return_value.list_approval_inbox.return_value = [
            _row(originator_code="USER37"),
            _row(wdd_code=2, status="APPROVED", waiting_on_me=False, decided_by="USER37"),
        ]
        pending, approved = self.client.get(f"{BASE}requests/", **self.headers).data["results"]
        self.assertTrue(pending["is_mine"])
        self.assertFalse(pending["can_decide"])
        self.assertFalse(pending["can_withdraw"])
        self.assertFalse(approved["can_change_decision"])

    def test_a_full_page_says_it_may_be_truncated(self, sap):
        sap.return_value.list_approval_inbox.return_value = [_row(wdd_code=n) for n in range(2)]
        data = self.client.get(f"{BASE}requests/?limit=2", **self.headers).data
        self.assertTrue(data["truncated"])

    def test_the_next_page_is_asked_for_by_offset(self, sap):
        sap.return_value.list_approval_inbox.return_value = []
        data = self.client.get(f"{BASE}requests/?limit=200&offset=400", **self.headers).data
        self.assertEqual(sap.return_value.list_approval_inbox.call_args.kwargs["offset"], 400)
        self.assertEqual(data["offset"], 400)
        self.assertEqual(
            self.client.get(f"{BASE}requests/?offset=-1", **self.headers).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

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

    def test_approving_an_approved_request_again_is_409(self, sap):
        """Changing a decision needs the other decision; the same one changes nothing."""
        sap.return_value.approval_inbox_stage.return_value = _stage(
            status="APPROVED", decided_by="USER37"
        )
        response = self._post({"approve": True})
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
        response = self._post({"approve": False, "category": "OTHER", "remarks": "duplicate"})
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
            self._post({"approve": False, "category": "OTHER", "remarks": "wrong rate"}).status_code, status.HTTP_200_OK
        )
        remarks = sap.return_value.decide_approval_request.call_args.kwargs["remarks"]
        self.assertTrue(remarks.startswith("wrong rate — Honey Singh"))
        audit = SapApprovalDecision.objects.get()
        self.assertEqual((audit.action, audit.category), ("REJECT", "OTHER"))

    def test_reject_needs_a_category_which_stays_out_of_sap(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self._post({"approve": False, "remarks": "GL"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("category", response.data)
        response = self._post({"approve": False, "remarks": "GL", "category": "PETROL"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sap.return_value.decide_approval_request.assert_not_called()

        response = self._post({"approve": False, "remarks": "GL", "category": "ELECTRICITY"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        remarks = sap.return_value.decide_approval_request.call_args.kwargs["remarks"]
        self.assertNotIn("ELECTRICITY", remarks.upper())
        self.assertEqual(SapApprovalDecision.objects.get().category, "ELECTRICITY")

    def test_an_approval_keeps_no_category(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        sap.return_value.decide_approval_request.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self._post({"approve": True, "category": "FUEL"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(SapApprovalDecision.objects.get().category, "")

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
# Rejection history
# ---------------------------------------------------------------------------


def _rejection(**overrides):
    """One A/P invoice USER39 raised, rejected from the app by USER03."""
    row = {
        "wdd_code": 77046,
        "object_type": "18",
        "object_type_label": "A/P Invoice",
        "draft_entry": 58129,
        "rejected_at": "2026-10-08T13:25:00",
        "rejected_by": "USER03",
        "rejected_by_name": "BHAWANI",
        "remarks": "GL — Bhawani (Factory app)",
        "originator_code": "USER39",
        "originator_name": "MUQEEM",
        "doc_num": 726093130,
        "doc_date": "2026-09-26",
        "card_code": "ORGV000052",
        "party_name": "SUSHIL KUMAR SINGH IT 20000 IMPREST JWPL0010",
        "total_amount": "2052.00",
        "gl_account": "5680022",
        "gl_account_name": "COMPUTER AND HARDWARE",
        "raised_on": "2026-10-07",
        "reference": "HF2707I007629873",
        "now": {"stage": "POSTED", "via": "reference", "doc_num": 726093200, "posted": True},
    }
    row.update(overrides)
    return row


@patch("sap_approvals.views.SAPClient")
class RejectionHistoryTests(SapApprovalsTestCase):
    url = f"{BASE}rejections/"

    def _get(self, **params):
        return self.client.get(self.url, params, **self.headers)

    def test_the_window_defaults_to_this_month_and_filters_pass_through(self, sap):
        sap.return_value.list_approval_rejections.return_value = []
        with patch("sap_approvals.views.timezone.localdate") as today:
            today.return_value = date(2026, 10, 9)
            response = self._get()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual((response.data["date_from"], response.data["date_to"]),
                         ("2026-10-01", "2026-10-09"))

        self._get(date_from="2026-09-01", date_to="2026-09-30", originator=" user39 ")
        kwargs = sap.return_value.list_approval_rejections.call_args.kwargs
        self.assertEqual(str(kwargs["date_from"]), "2026-09-01")
        self.assertEqual(str(kwargs["date_to"]), "2026-09-30")
        self.assertEqual(kwargs["originator_code"], "USER39")
        self.assertEqual(
            self._get(date_from="2026-09-30", date_to="2026-09-01").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_the_category_picked_here_wins_and_the_gl_name_fills_in(self, sap):
        SapApprovalDecision.objects.create(
            company=self.company, wdd_code=77046, action="REJECT", category="IMPREST",
            created_by=self.user,
        )
        SapApprovalDecision.objects.create(
            company=self.company, wdd_code=77046, action="APPROVE", created_by=self.user,
        )
        sap.return_value.list_approval_rejections.return_value = [
            _rejection(),
            _rejection(wdd_code=19724, remarks="Bad copy", gl_account="5670001",
                       gl_account_name="FREIGHT AND CARTAGE"),
            _rejection(wdd_code=9, object_type_label="Outgoing Payment", gl_account=None,
                       gl_account_name=None, remarks=None),
        ]
        rows = self._get().data["results"]
        self.assertEqual(
            [(r["category_label"], r["category_source"]) for r in rows],
            [("Imprest", "app"), ("FREIGHT AND CARTAGE", "gl"), ("Outgoing Payment", "document")],
        )

    def test_a_rejection_later_approved_still_counts(self, sap):
        """SAP keeps only the latest decision; this app's record brings it back."""
        decision = SapApprovalDecision.objects.create(
            company=self.company, wdd_code=500, action="REJECT", category="FUEL",
            signed_as="USER03", remarks="Budget", created_by=self.user,
        )
        sap.return_value.list_approval_rejections.return_value = [
            _rejection(wdd_code=500, rejected_at=None, rejected_by=None, remarks=None,
                       now={"stage": "APPROVED", "via": "same_request", "doc_num": None,
                            "posted": False}),
        ]
        row = self._get().data["results"][0]
        self.assertEqual(sap.return_value.list_approval_rejections.call_args.kwargs["also_codes"], [500])
        self.assertEqual((row["rejected_by"], row["reason"], row["category_label"]),
                         ("USER03", "Budget", "Fuel"))
        self.assertEqual(
            row["rejected_at"], timezone.localtime(decision.created_at).strftime("%Y-%m-%dT%H:%M:00")
        )
        self.assertEqual(row["now"]["stage"], "APPROVED")

    def test_the_reason_is_shown_as_it_was_typed(self, sap):
        sap.return_value.list_approval_rejections.return_value = [
            _rejection(remarks="GL — Bhawani (Factory app)"),
            _rejection(remarks="Rate — not GST — changed to rejected by Bhawani (Factory app)"),
            _rejection(remarks="Changed to rejected by Bhawani (Factory app)"),
            _rejection(remarks="Typed in the SAP client"),
        ]
        self.assertEqual(
            [r["reason"] for r in self._get().data["results"]],
            ["GL", "Rate — not GST", "", "Typed in the SAP client"],
        )

    def test_originators_are_ranked_and_their_uncorrected_ones_counted(self, sap):
        still = {"stage": "STILL_REJECTED", "via": None, "doc_num": None, "posted": False}
        sap.return_value.list_approval_rejections.return_value = [
            _rejection(originator_code="USER08", originator_name="DIVJOT", total_amount="57.00"),
            _rejection(originator_code="USER39", total_amount="2052.00", now=still),
            _rejection(originator_code="USER39", total_amount="7000.00"),
            _rejection(originator_code="USER07", originator_name="HARSH", total_amount="1161061.00"),
        ]
        ranked = self._get().data["by_originator"]
        self.assertEqual(
            [(g["originator_code"], g["count"], g["still_rejected"], g["amount"]) for g in ranked],
            [("USER39", 2, 1, "9052.00"), ("USER07", 1, 0, "1161061.00"),
             ("USER08", 1, 0, "57.00")],
        )

    def test_all_companies_reads_each_company_the_user_belongs_to(self, sap):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        UserCompany.objects.create(user=self.user, company=mart, role=self.role)
        # A company with no SAP database is not asked.
        other = Company.objects.create(name="Jivo Wellness", code="JIVO_WELLNESS")
        UserCompany.objects.create(user=self.user, company=other, role=self.role)
        sap.return_value.list_approval_rejections.side_effect = [
            [_rejection(wdd_code=1, rejected_at="2026-10-08T10:00:00")],
            [_rejection(wdd_code=2, rejected_at="2026-10-08T11:00:00")],
        ]
        data = self._get(all_companies="true").data
        self.assertEqual([c["code"] for c in data["companies"]], ["JIVO_MART", "JIVO_OIL"])
        self.assertEqual(
            [(r["wdd_code"], r["company_code"]) for r in data["results"]],
            [(2, "JIVO_OIL"), (1, "JIVO_MART")],
        )
        self.assertEqual(
            [call.kwargs["company_code"] for call in sap.call_args_list],
            ["JIVO_MART", "JIVO_OIL"],
        )

    def test_a_company_sap_cannot_answer_for_is_named_not_dropped(self, sap):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        UserCompany.objects.create(user=self.user, company=mart, role=self.role)
        sap.return_value.list_approval_rejections.side_effect = [
            SAPConnectionError("down"), [_rejection()],
        ]
        data = self._get(all_companies="true").data
        self.assertEqual(data["unavailable"], [{"code": "JIVO_MART", "name": "Jivo Mart"}])
        self.assertEqual(data["count"], 1)

        sap.return_value.list_approval_rejections.side_effect = SAPConnectionError("down")
        self.assertEqual(self._get().status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_anyone_who_can_open_the_inbox_can_open_it(self, sap):
        sap.return_value.list_approval_rejections.return_value = []
        self.revoke("can_decide_sap_approvals", "can_withdraw_own_sap_approvals")
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)
        self.revoke("can_view_sap_approval_inbox")
        self.assertEqual(self._get().status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Changing a decision already taken (approved ↔ rejected)
# ---------------------------------------------------------------------------


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "stored"}})
@patch("sap_approvals.views.SAPClient")
class ChangeDecisionTests(SapApprovalsTestCase):
    """SAP Portal let an approver change a decision (routes/sap.js, a280164).

    JI lets the SAP user SAP records as the decider change it, through the same
    decision endpoint, while the request is approved or rejected — never once
    the document is posted, withdrawn, or a leftover SAP never closed.
    """

    def _decided(self, outcome="APPROVED", **overrides):
        # A decided request: no stage waits, so no authorizer is undecided.
        values = dict(
            status=outcome, waiting_on_me=False, authorizer_codes=[],
            approver_code=None, approver_name=None,
            decided_by="USER37", decided_by_name="HONEY SINGH",
            decided_at="2026-09-17T11:00:00",
        )
        values.update(overrides)
        return _stage(**values)

    def _post(self, body, code=75424):
        return self.client.post(decision_url(code), body, format="json", **self.headers)

    def _sap_accepts(self, sap, message="A/R Credit Note changed to rejected in SAP."):
        sap.return_value.decide_approval_request.return_value = {
            "message": message, "signed_as": "USER37",
        }

    def test_the_approver_changes_approved_to_rejected(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        self._sap_accepts(sap)
        response = self._post({"approve": False, "category": "OTHER", "remarks": "wrong party"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["action"], "REJECT")
        self.assertEqual(response.data["changed_from"], "APPROVED")
        self.assertEqual(response.data["signed_as"], "USER37")
        kwargs = sap.return_value.decide_approval_request.call_args.kwargs
        self.assertTrue(kwargs["change"])
        self.assertFalse(kwargs["approve"])
        self.assertEqual(kwargs["approver"], "USER37")
        self.assertIsNone(kwargs["password"])
        self.assertEqual(
            kwargs["remarks"], "wrong party — changed to rejected by Honey Singh (Factory app)"
        )
        # Rejecting never needs the duplicate read.
        self.assertFalse(sap.return_value.approval_inbox_stage.call_args.kwargs["with_duplicates"])
        audit = SapApprovalDecision.objects.get()
        self.assertEqual(
            (audit.action, audit.changed_from, audit.signed_as, audit.remarks),
            ("REJECT", "APPROVED", "USER37", "wrong party"),
        )
        self.assertEqual(audit.created_by, self.user)

    def test_the_rejecter_changes_rejected_to_approved(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "REJECTED", rejection_reason="rate wrong"
        )
        self._sap_accepts(sap, "A/R Credit Note changed to approved in SAP.")
        response = self._post({"approve": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["changed_from"], "REJECTED")
        self.assertEqual(response.data["message"], "A/R Credit Note changed to approved in SAP.")
        kwargs = sap.return_value.decide_approval_request.call_args.kwargs
        self.assertTrue(kwargs["change"])
        self.assertTrue(kwargs["approve"])
        self.assertEqual(kwargs["remarks"], "Changed to approved by Honey Singh (Factory app)")
        # Approving asks the reader for the posted-duplicate check.
        self.assertTrue(sap.return_value.approval_inbox_stage.call_args.kwargs["with_duplicates"])
        audit = SapApprovalDecision.objects.get()
        self.assertEqual((audit.action, audit.changed_from), ("APPROVE", "REJECTED"))

    def test_a_first_decision_is_not_recorded_as_a_change(self, sap):
        sap.return_value.approval_inbox_stage.return_value = _stage()
        self._sap_accepts(sap, "ok")
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["changed_from"])
        self.assertFalse(sap.return_value.decide_approval_request.call_args.kwargs["change"])
        self.assertEqual(SapApprovalDecision.objects.get().changed_from, "")

    def test_asking_for_the_same_decision_is_409(self, sap):
        for outcome, approve, word in (("APPROVED", True, "approved"), ("REJECTED", False, "rejected")):
            with self.subTest(outcome=outcome):
                sap.return_value.approval_inbox_stage.return_value = self._decided(outcome)
                response = self._post({"approve": approve, "remarks": "again", "category": "OTHER"})
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(response.data["code"], "STALE_REQUEST")
                self.assertIn(f"already {word}", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_a_posted_or_withdrawn_request_is_final(self, sap):
        for outcome in ("GENERATED", "CANCELLED"):
            with self.subTest(outcome=outcome):
                sap.return_value.approval_inbox_stage.return_value = self._decided(outcome)
                for approve in (True, False):
                    response = self._post({"approve": approve, "remarks": "change", "category": "OTHER"})
                    self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                    self.assertEqual(response.data["code"], "STALE_REQUEST")
        sap.return_value.decide_approval_request.assert_not_called()
        self.assertFalse(SapApprovalDecision.objects.exists())

    def test_a_leftover_sap_never_closed_cannot_be_changed(self, sap):
        """OWDD still says 'W' but the draft was approved: there is no decision line to change."""
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "APPROVED", stale_pending=True
        )
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("nothing left to approve or reject", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_only_the_one_who_decided_may_change_it_even_with_a_typed_password(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "APPROVED", decided_by="USER24", decided_by_name="PANKAJ"
        )
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change", "sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("approved by USER24 (PANKAJ)", response.data["error"])
        self.assertIn("You act as USER37", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_being_on_the_originator_side_is_not_enough(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "APPROVED", decided_by="USER24", originator_code="USER37"
        )
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap.return_value.decide_approval_request.assert_not_called()

    def test_no_recorded_decider_is_403(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "REJECTED", decided_by=None, decided_by_name=None
        )
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("SAP does not say who rejected", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_an_unmapped_user_is_403(self, sap):
        self.unmap()
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("SAP Identities", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

    def test_the_decider_is_matched_whatever_the_case_and_signs_as_sap_spells_it(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "APPROVED", decided_by="User37"
        )
        self._sap_accepts(sap)
        self.assertEqual(
            self._post({"approve": False, "category": "OTHER", "remarks": "change"}).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            sap.return_value.decide_approval_request.call_args.kwargs["approver"], "User37"
        )

    def test_changing_to_approved_over_a_posted_duplicate_needs_confirmation(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided(
            "REJECTED", posted_duplicates=[POSTED]
        )
        response = self._post({"approve": True})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "DUPLICATE_DOCUMENT")
        sap.return_value.decide_approval_request.assert_not_called()

        self._sap_accepts(sap, "ok")
        with self.assertLogs("sap_approvals.views", level="WARNING"):
            response = self._post({"approve": True, "confirm_duplicate": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        audit = SapApprovalDecision.objects.get()
        self.assertEqual((audit.confirmed_duplicate, audit.changed_from), (True, "REJECTED"))

    def test_changing_to_rejected_needs_a_reason(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        response = self._post({"approve": False})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("remarks", response.data)
        sap.return_value.decide_approval_request.assert_not_called()

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_nothing_to_sign_with_is_400_and_a_typed_password_signs(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("No SAP password is stored for USER37", response.data["error"])
        sap.return_value.decide_approval_request.assert_not_called()

        self._sap_accepts(sap)
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change", "sap_password": TYPED})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            sap.return_value.decide_approval_request.call_args.kwargs["password"], TYPED
        )
        self.assertNotIn(TYPED, response.content.decode())
        self.assertTrue(SapApprovalDecision.objects.get().typed_password)

    def test_a_change_sap_refuses_is_400_with_sap_s_words_and_no_audit(self, sap):
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        sap.return_value.decide_approval_request.side_effect = SAPValidationError(
            "(-2028) No matching records found"
        )
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("-2028", response.data["error"])
        self.assertFalse(SapApprovalDecision.objects.exists())

    def test_changing_needs_the_decide_right(self, sap):
        self.revoke("can_decide_sap_approvals")
        sap.return_value.approval_inbox_stage.return_value = self._decided("APPROVED")
        response = self._post({"approve": False, "category": "OTHER", "remarks": "change"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        sap.return_value.approval_inbox_stage.assert_not_called()

    def test_the_detail_offers_the_change_to_the_decider_only(self, sap):
        stages = [{"step_code": 20, "user_code": "USER37", "status": "APPROVED", "is_current": True}]
        sap.return_value.approval_inbox_detail.return_value = self._decided(
            "APPROVED", stages=stages, lines=[], lines_available=True
        )
        data = self.client.get(f"{BASE}requests/75424/", **self.headers).data
        self.assertTrue(data["can_change_decision"])
        self.assertFalse(data["can_decide"])

        sap.return_value.approval_inbox_detail.return_value = self._decided(
            "APPROVED", decided_by="USER24", stages=stages, lines=[], lines_available=True
        )
        data = self.client.get(f"{BASE}requests/75424/", **self.headers).data
        self.assertFalse(data["can_change_decision"])


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
        checked = {
            guards.VIEW_PERMISSION, guards.DECIDE_PERMISSION, guards.WITHDRAW_PERMISSION,
        }
        self.assertEqual(checked - granted, set())

    def test_a_rerun_changes_nothing_and_adds_no_members(self):
        before = {g.name: set(g.permissions.all()) for g in Group.objects.all()}
        call_command("setup_sap_approvals_groups", stdout=StringIO())
        after = {g.name: set(g.permissions.all()) for g in Group.objects.all()}
        self.assertEqual(before, after)
        for name in SAP_APPROVALS_GROUPS:
            self.assertFalse(Group.objects.get(name=name).user_set.exists())
