"""The SAP posting queue: what a try leaves behind, and what the worker does with it.

A fake handler stands in for SAP: each test says what the next try comes to.
The goods return's own handler is tested against its flow in
goods_return/tests_posting.py.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

from . import services
from .models import SapPosting, SapPostingOutcome, SapPostingStatus
from .services import Outcome


class FakeHandler:
    """Answers each try with the next queued outcome."""

    answers = []
    sent = []

    def send(self, posting):
        FakeHandler.sent.append(posting.pk)
        answer = FakeHandler.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class QueueTestCase(TestCase):
    def setUp(self):
        FakeHandler.answers = []
        FakeHandler.sent = []
        patcher = mock.patch.dict(services.HANDLERS, {"test.thing": f"{__name__}.FakeHandler"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create(email="clerk@example.com", full_name="Clerk")

    def post(self, *answers, source_id=1):
        FakeHandler.answers.extend(answers)
        return services.post_now(
            kind="test.thing", company=self.company, source_id=source_id,
            title=f"Thing {source_id}", link=f"/things/{source_id}", params={"x": 1},
            user=self.user,
        )


class PostNowTests(QueueTestCase):
    def test_a_posting_that_goes_through_is_logged_as_posted(self):
        posting, outcome = self.post(Outcome.posted("SAP Return 160001", result={"doc_nums": ["160001"]}))
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.POSTED)
        self.assertEqual(posting.result, {"doc_nums": ["160001"]})
        self.assertIsNotNone(posting.posted_at)
        attempt = posting.attempt_log.get()
        self.assertEqual((attempt.number, attempt.outcome, attempt.by_worker), (1, "POSTED", False))
        self.assertIsNotNone(attempt.finished_at)

    def test_sap_not_answering_leaves_it_waiting_for_the_worker(self):
        before = timezone.now()
        posting, outcome = self.post(Outcome.waiting("SAP could not be reached"))
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)
        self.assertEqual(posting.last_error, "SAP could not be reached")
        self.assertGreaterEqual(posting.next_attempt_at, before + timedelta(seconds=30))

    def test_a_refusal_stops_it_for_a_person(self):
        posting, _ = self.post(Outcome.rejected("-5002 Quantity exceeds"))
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.REJECTED)
        self.assertIsNone(posting.next_attempt_at)

    def test_a_handler_that_crashes_is_logged_not_lost(self):
        posting, outcome = self.post(RuntimeError("bug"))
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.REJECTED)
        self.assertIn("Unexpected error: bug", posting.last_error)
        self.assertEqual(posting.attempt_log.get().outcome, SapPostingOutcome.REJECTED)

    def test_trying_a_waiting_record_again_reuses_its_posting(self):
        first, _ = self.post(Outcome.waiting("down"))
        second, _ = self.post(Outcome.posted("up again"))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SapPosting.objects.count(), 1)
        self.assertEqual(list(second.attempt_log.values_list("number", flat=True)), [1, 2])

    def test_a_record_being_sent_is_not_sent_twice(self):
        posting, _ = self.post(Outcome.waiting("down"))
        SapPosting.objects.filter(pk=posting.pk).update(status=SapPostingStatus.SENDING)
        with self.assertRaises(services.PostingInProgress):
            self.post(Outcome.posted())

    def test_the_log_is_not_left_inside_someone_elses_transaction(self):
        # It would roll back with the posting it records, which is the one
        # thing this module exists not to do.
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.post(Outcome.posted())

    def test_the_wait_grows_and_then_stops_growing(self):
        self.assertEqual(
            [services._backoff(n).total_seconds() for n in range(1, 8)],
            [30, 60, 120, 240, 300, 300, 300],
        )


class WorkerTests(QueueTestCase):
    def due_now(self, posting):
        SapPosting.objects.filter(pk=posting.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )

    def run_due(self, down=False):
        with mock.patch("sap_client.health.failing_fast", return_value=down), \
             mock.patch("notifications.services.NotificationService.send_notification_to_user") as notify:
            sent = services.run_due()
        return sent, notify

    def test_it_sends_what_is_due_and_tells_whoever_made_it(self):
        posting, _ = self.post(Outcome.waiting("down"))
        self.due_now(posting)
        FakeHandler.answers.append(Outcome.posted("SAP Return 160002"))

        sent, notify = self.run_due()

        self.assertEqual(sent, 1)
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.POSTED)
        self.assertTrue(posting.attempt_log.get(number=2).by_worker)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["user"], self.user)
        self.assertEqual(notify.call_args.kwargs["title"], "Posted to SAP: Thing 1")
        self.assertEqual(notify.call_args.kwargs["click_action_url"], "/things/1")

    def test_nothing_is_sent_before_its_time(self):
        self.post(Outcome.waiting("down"))
        sent, _ = self.run_due()
        self.assertEqual(sent, 0)

    def test_nothing_is_sent_while_sap_is_known_down(self):
        posting, _ = self.post(Outcome.waiting("down"))
        self.due_now(posting)
        sent, _ = self.run_due(down=True)
        self.assertEqual(sent, 0)
        self.assertEqual(FakeHandler.sent, [posting.pk])  # only the first try

    def test_another_wait_is_not_a_message(self):
        posting, _ = self.post(Outcome.waiting("down"))
        self.due_now(posting)
        FakeHandler.answers.append(Outcome.waiting("still down"))
        _, notify = self.run_due()
        notify.assert_not_called()
        posting.refresh_from_db()
        self.assertEqual(posting.attempts, 2)
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)

    def test_a_refusal_from_the_worker_is_a_message(self):
        posting, _ = self.post(Outcome.waiting("down"))
        self.due_now(posting)
        FakeHandler.answers.append(Outcome.rejected("-5002 refused"))
        _, notify = self.run_due()
        self.assertEqual(notify.call_args.kwargs["title"], "SAP refused: Thing 1")

    def test_oldest_first(self):
        a, _ = self.post(Outcome.waiting("down"), source_id=1)
        b, _ = self.post(Outcome.waiting("down"), source_id=2)
        self.due_now(a)
        self.due_now(b)
        FakeHandler.answers.extend([Outcome.posted(), Outcome.posted()])
        self.run_due()
        self.assertEqual(FakeHandler.sent[-2:], [a.pk, b.pk])

    def test_a_posting_whose_process_died_mid_send_is_put_back(self):
        posting, _ = self.post(Outcome.waiting("down"))
        SapPosting.objects.filter(pk=posting.pk).update(
            status=SapPostingStatus.SENDING,
            updated_at=timezone.now() - services.STUCK_AFTER - timedelta(minutes=1),
        )
        self.assertEqual(services.release_stuck(), 1)
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)

    def test_sap_coming_back_brings_every_wait_forward(self):
        posting, _ = self.post(Outcome.waiting("down"))
        self.assertEqual(services.bring_forward(), 1)
        posting.refresh_from_db()
        self.assertLessEqual(posting.next_attempt_at, timezone.now())


class RetryCancelTests(QueueTestCase):
    def test_a_refused_posting_can_be_sent_again_once_fixed(self):
        posting, _ = self.post(Outcome.rejected("refused"))
        FakeHandler.answers.append(Outcome.posted("fixed"))
        services.retry(posting.pk)
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.POSTED)

    def test_a_posted_one_cannot(self):
        posting, _ = self.post(Outcome.posted())
        with self.assertRaises(ValueError):
            services.retry(posting.pk)

    def test_cancelling_needs_a_reason_and_stops_it(self):
        posting, _ = self.post(Outcome.waiting("down"))
        with self.assertRaises(ValueError):
            services.cancel(posting.pk, self.user, "  ")
        services.cancel(posting.pk, self.user, "Posted by hand in SAP as 160099")
        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.CANCELLED)
        self.assertEqual(posting.cancelled_by, self.user)
        self.assertIsNone(posting.next_attempt_at)


class ApiTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        UserCompany.objects.create(
            user=self.user, company=self.company, role=UserRole.objects.create(name="Admin"),
            is_active=True,
        )
        self.api = APIClient()

    def as_user(self, *codenames):
        for codename in codenames:
            self.user.user_permissions.add(
                Permission.objects.get(content_type__app_label="sap_postings", codename=codename)
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.api.force_authenticate(self.user)

    def get(self, path, **params):
        return self.api.get(f"/api/v1/sap-postings/{path}", params, HTTP_COMPANY_CODE="JIVO_OIL")

    def post_to(self, path, data=None):
        return self.api.post(
            f"/api/v1/sap-postings/{path}", data or {}, format="json", HTTP_COMPANY_CODE="JIVO_OIL"
        )

    def test_the_log_needs_the_view_permission(self):
        self.api.force_authenticate(self.user)
        self.assertEqual(self.get("").status_code, 403)

    def test_the_log_lists_and_filters_by_tab(self):
        self.as_user("view_sapposting")
        self.post(Outcome.waiting("down"), source_id=1)
        self.post(Outcome.rejected("refused"), source_id=2)
        self.post(Outcome.posted(), source_id=3)

        body = self.get("").json()
        self.assertEqual(body["count"], 3)
        # Newest first.
        self.assertEqual([p["title"] for p in body["results"]], ["Thing 3", "Thing 2", "Thing 1"])
        waiting = self.get("", status="QUEUED").json()["results"]
        self.assertEqual([p["title"] for p in waiting], ["Thing 1"])
        self.assertEqual(waiting[0]["status_label"], "Waiting for SAP")

    def test_the_log_comes_a_page_at_a_time(self):
        self.as_user("view_sapposting")
        for n in range(1, 6):
            self.post(Outcome.posted(), source_id=n)
        first = self.get("", page_size=2).json()
        self.assertEqual((first["count"], first["total_pages"], first["next"]), (5, 3, True))
        self.assertEqual([p["title"] for p in first["results"]], ["Thing 5", "Thing 4"])
        last = self.get("", page_size=2, page=3).json()
        self.assertEqual([p["title"] for p in last["results"]], ["Thing 1"])
        self.assertFalse(last["next"])
        # A page past the end is the last page, not an empty one.
        self.assertEqual(self.get("", page_size=2, page=99).json()["page"], 3)
        # And nobody gets the whole log by asking for it.
        self.assertEqual(self.get("", page_size=10_000).json()["page_size"], 100)

    def test_search_finds_a_title_or_an_sap_document_number(self):
        self.as_user("view_sapposting")
        self.post(Outcome.posted(result={"doc_nums": ["1626096501"]}), source_id=1)
        self.post(Outcome.posted(result={"doc_nums": ["1626096502"]}), source_id=2)
        by_doc = self.get("", q="1626096502").json()["results"]
        self.assertEqual([p["title"] for p in by_doc], ["Thing 2"])
        by_title = self.get("", q="thing 1").json()["results"]
        self.assertEqual([p["title"] for p in by_title], ["Thing 1"])

    def test_dates_bound_the_log(self):
        self.as_user("view_sapposting")
        old, _ = self.post(Outcome.posted(), source_id=1)
        self.post(Outcome.posted(), source_id=2)
        SapPosting.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
        since = (timezone.localdate() - timedelta(days=7)).isoformat()
        recent = self.get("", date_from=since).json()["results"]
        self.assertEqual([p["title"] for p in recent], ["Thing 2"])
        until = (timezone.localdate() - timedelta(days=30)).isoformat()
        older = self.get("", date_to=until).json()["results"]
        self.assertEqual([p["title"] for p in older], ["Thing 1"])

    def test_the_counts_are_what_the_badge_polls(self):
        self.as_user("view_sapposting")
        self.post(Outcome.waiting("down"), source_id=1)
        self.post(Outcome.rejected("refused"), source_id=2)
        self.post(Outcome.rejected("refused"), source_id=3)
        body = self.get("counts/").json()
        self.assertEqual(body["counts"], {"QUEUED": 1, "REJECTED": 2})
        self.assertIn({"value": "goods_return.receive", "label": "Goods return (A/R Return)"}, body["kinds"])

    def test_another_companys_postings_are_not_listed(self):
        self.as_user("view_sapposting")
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        FakeHandler.answers.append(Outcome.posted())
        services.post_now(kind="test.thing", company=other, source_id=9, title="Mart thing", user=self.user)
        self.assertEqual(self.get("").json()["results"], [])

    def test_the_detail_carries_every_attempt(self):
        self.as_user("view_sapposting")
        posting, _ = self.post(Outcome.waiting("down"))
        self.post(Outcome.posted("SAP Return 160003"))
        body = self.get(f"{posting.pk}/").json()
        self.assertEqual([a["outcome"] for a in body["attempts_log"]], ["WAITING", "POSTED"])

    def test_sending_again_and_cancelling_need_the_change_permission(self):
        self.as_user("view_sapposting")
        posting, _ = self.post(Outcome.rejected("refused"))
        self.assertEqual(self.post_to(f"{posting.pk}/retry/").status_code, 403)
        self.assertEqual(self.post_to(f"{posting.pk}/cancel/", {"reason": "x"}).status_code, 403)

    def test_sending_again_answers_with_the_new_attempt(self):
        self.as_user("view_sapposting", "change_sapposting")
        posting, _ = self.post(Outcome.rejected("refused"))
        FakeHandler.answers.append(Outcome.posted("SAP Return 160004"))
        body = self.post_to(f"{posting.pk}/retry/").json()
        self.assertEqual(body["status"], "POSTED")
        self.assertEqual(len(body["attempts_log"]), 2)

    def test_cancel_without_a_reason_is_refused(self):
        self.as_user("view_sapposting", "change_sapposting")
        posting, _ = self.post(Outcome.waiting("down"))
        self.assertEqual(self.post_to(f"{posting.pk}/cancel/").status_code, 400)


# The worker drops stale connections every pass, as a long-lived process must;
# inside a TestCase that would close the test's own connection mid-transaction.
CLOSE_CONNECTIONS = "sap_postings.management.commands.run_sap_postings.close_old_connections"


class WorkerCommandTests(TestCase):
    """manage.py run_sap_postings: one pass, and knowing when to step aside."""

    def snapshot(self, status):
        return {"ok": status == "up", "components": {"service_layer": {"status": status}}}

    def test_sap_coming_back_sends_every_wait_at_once(self):
        from .management.commands.run_sap_postings import Command

        with mock.patch("sap_client.health.snapshot", return_value=self.snapshot("up")) as snap, \
             mock.patch.object(services, "bring_forward") as forward, \
             mock.patch.object(services, "run_due", return_value=0) as run_due:
            is_up = Command().one_pass(was_up=False)
        self.assertTrue(is_up)
        forward.assert_called_once()
        run_due.assert_called_once()
        # The worker is the process that alerts.
        snap.assert_called_once_with(alert=True)

    def test_starting_up_with_sap_up_sends_every_wait_at_once(self):
        # A restart right after SAP came back must not leave postings sitting
        # out a five-minute backoff.
        from .management.commands.run_sap_postings import Command

        with mock.patch("sap_client.health.snapshot", return_value=self.snapshot("up")), \
             mock.patch.object(services, "bring_forward", return_value=0) as forward, \
             mock.patch.object(services, "run_due", return_value=0):
            Command().one_pass(was_up=None)
        forward.assert_called_once()

    def test_up_all_along_is_just_a_pass(self):
        from .management.commands.run_sap_postings import Command

        with mock.patch("sap_client.health.snapshot", return_value=self.snapshot("up")), \
             mock.patch.object(services, "bring_forward") as forward, \
             mock.patch.object(services, "run_due", return_value=0):
            Command().one_pass(was_up=True)
        forward.assert_not_called()

    def test_it_exits_when_a_deploy_moves_the_live_release(self):
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "current"
            link.symlink_to(tmp)  # anything but the release this runs from
            with mock.patch("sap_client.health.snapshot", return_value=self.snapshot("up")), \
                 mock.patch.object(services, "run_due", return_value=0), \
                 mock.patch(CLOSE_CONNECTIONS), \
                 mock.patch("time.sleep") as sleep:
                call_command("run_sap_postings", exit_when_moved=str(link), interval=0)
            sleep.assert_not_called()  # it left after the first pass

    def test_a_developer_worker_will_not_send_to_live_sap(self):
        from django.core.management import CommandError, call_command
        from django.test import override_settings

        with override_settings(COMPANY_DB={"JIVO_OIL": "JIVO_OIL_HANADB", "JIVO_MART": "TEST_JIVO_MART_HANADB"}):
            with self.assertRaisesMessage(CommandError, "JIVO_OIL_HANADB is live SAP"):
                call_command("run_sap_postings", require_test_sap=True, once=True)
        with override_settings(COMPANY_DB={"JIVO_OIL": "TEST_JIVO_OIL_HANADB"}), \
             mock.patch("sap_client.health.snapshot", return_value=self.snapshot("up")), \
             mock.patch(CLOSE_CONNECTIONS), \
             mock.patch.object(services, "run_due", return_value=0) as run_due:
            call_command("run_sap_postings", require_test_sap=True, once=True)
        run_due.assert_called_once()
