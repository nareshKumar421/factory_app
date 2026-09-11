"""
Tests for the issue tracker.

Three things are worth pinning down, and they are the three that would quietly
break: the **search box grammar** (a mistyped qualifier must not silently match
everything), the **timeline** (every change writes its event, and events are
frozen), and the **permission split** between filing and triage.
"""

import shutil
import tempfile
from io import StringIO

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User

from . import services
from .constants import (
    REPORTER_GROUP,
    IssuePriority,
    IssueState,
    StateReason,
    TimelineEvent,
)
from .models import Issue, IssueArea, IssueLabel
from .search import parse_query


def make_user(email, **extra):
    return User.objects.create_user(
        email=email,
        password="pass1234",
        full_name=extra.pop("full_name", email.split("@")[0].title()),
        employee_code=extra.pop("employee_code", email.split("@")[0].upper()),
        **extra,
    )


def grant(user, *codenames):
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="issues", codename__in=codenames
        )
    )
    # has_perm caches per instance.
    return User.objects.get(pk=user.pk)


class SearchGrammarTests(TestCase):
    """The one text input, parsed."""

    def test_free_text_and_qualifiers_separate(self):
        parsed = parse_query("is:open label:bug gate pass")
        self.assertEqual(parsed.state, IssueState.OPEN)
        self.assertEqual(parsed.labels, ["bug"])
        self.assertEqual(parsed.text, "gate pass")

    def test_quoted_phrase_stays_together(self):
        parsed = parse_query('"gate pass" label:"data issue"')
        self.assertEqual(parsed.labels, ["data issue"])
        self.assertEqual(parsed.text, "gate pass")

    def test_negated_label_excludes(self):
        parsed = parse_query("-label:duplicate")
        self.assertEqual(parsed.exclude_labels, ["duplicate"])
        self.assertEqual(parsed.labels, [])

    def test_unknown_qualifier_is_reported_not_swallowed(self):
        parsed = parse_query("labels:bug")
        self.assertEqual(parsed.unknown, ["labels:bug"])
        # And it must NOT leak into the free-text search, which would make it
        # look like it filtered.
        self.assertEqual(parsed.text, "")

    def test_bad_value_for_a_known_qualifier_is_reported(self):
        parsed = parse_query("priority:catastrophic")
        self.assertEqual(parsed.priorities, [])
        self.assertEqual(parsed.unknown, ["priority:catastrophic"])

    def test_bare_number_is_a_jump_not_a_body_search(self):
        self.assertEqual(parse_query("#41").numbers, [41])
        self.assertEqual(parse_query("41").numbers, [41])
        self.assertEqual(parse_query("41").text, "")

    def test_no_assignee(self):
        self.assertEqual(parse_query("no:assignee").empty, ["assignee"])

    def test_no_priority_is_rejected(self):
        # Priority always holds a value, so `no:priority` could never match --
        # better reported as a mistake than accepted and quietly ignored.
        parsed = parse_query("no:priority")
        self.assertEqual(parsed.empty, [])
        self.assertEqual(parsed.unknown, ["no:priority"])

    def test_sort_alias_resolves(self):
        self.assertEqual(parse_query("sort:oldest").sort, "created")
        self.assertEqual(parse_query("sort:comments").sort, "-comments")


class NumberAllocationTests(TestCase):
    def setUp(self):
        self.user = make_user("author@example.com")

    def test_numbers_are_sequential(self):
        first = services.create_issue(author=self.user, title="One")
        second = services.create_issue(author=self.user, title="Two")
        self.assertEqual(first.number, 1)
        self.assertEqual(second.number, 2)

    def test_deleting_an_issue_does_not_recycle_its_number(self):
        services.create_issue(author=self.user, title="One")
        second = services.create_issue(author=self.user, title="Two")
        second.delete()
        third = services.create_issue(author=self.user, title="Three")
        self.assertEqual(third.number, 3)

    def test_title_is_required(self):
        with self.assertRaises(services.IssueError):
            services.create_issue(author=self.user, title="   ")


class TimelineTests(TestCase):
    def setUp(self):
        self.author = make_user("author@example.com")
        self.maintainer = make_user("maint@example.com")
        self.bug = IssueLabel.objects.create(name="bug", color="#d73a4a")
        self.ui = IssueLabel.objects.create(name="ui", color="#c5def5")
        self.issue = services.create_issue(author=self.author, title="Scan fails")

    def test_opening_writes_one_event(self):
        events = list(self.issue.events.values_list("event", flat=True))
        self.assertEqual(events, [TimelineEvent.OPENED])

    def test_labelling_writes_an_event_per_label(self):
        services.set_labels(self.issue, self.maintainer, [self.bug, self.ui])
        labeled = self.issue.events.filter(event=TimelineEvent.LABELED)
        self.assertEqual(labeled.count(), 2)
        self.assertEqual(
            sorted(event.detail["label"] for event in labeled), ["bug", "ui"]
        )

    def test_removing_a_label_writes_unlabeled(self):
        services.set_labels(self.issue, self.maintainer, [self.bug, self.ui])
        services.set_labels(self.issue, self.maintainer, [self.bug])
        removed = self.issue.events.filter(event=TimelineEvent.UNLABELED)
        self.assertEqual(removed.count(), 1)
        self.assertEqual(removed.first().detail["label"], "ui")

    def test_setting_the_same_labels_again_writes_nothing(self):
        services.set_labels(self.issue, self.maintainer, [self.bug])
        before = self.issue.events.count()
        services.set_labels(self.issue, self.maintainer, [self.bug])
        self.assertEqual(self.issue.events.count(), before)

    def test_renaming_records_both_titles(self):
        services.update_issue(self.issue, self.maintainer, title="Scan fails on Chrome")
        event = self.issue.events.get(event=TimelineEvent.RENAMED)
        self.assertEqual(event.detail["previous"], "Scan fails")
        self.assertEqual(event.detail["current"], "Scan fails on Chrome")

    def test_saving_without_changing_anything_writes_nothing(self):
        before = self.issue.events.count()
        services.update_issue(
            self.issue,
            self.maintainer,
            title=self.issue.title,
            body=self.issue.body,
            priority=self.issue.priority,
        )
        self.assertEqual(self.issue.events.count(), before)

    def test_a_renamed_label_does_not_rewrite_history(self):
        services.set_labels(self.issue, self.maintainer, [self.bug])
        self.bug.name = "defect"
        self.bug.save(update_fields=["name"])
        event = self.issue.events.get(event=TimelineEvent.LABELED)
        self.assertEqual(event.detail["label"], "bug")

    def test_closing_and_reopening(self):
        services.close_issue(self.issue, self.maintainer, reason=StateReason.COMPLETED)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, IssueState.CLOSED)
        self.assertEqual(self.issue.closed_by, self.maintainer)
        self.assertIsNotNone(self.issue.closed_at)

        services.reopen_issue(self.issue, self.maintainer)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.state, IssueState.OPEN)
        # The close reason no longer holds, so it must be cleared.
        self.assertEqual(self.issue.state_reason, "")
        self.assertIsNone(self.issue.closed_at)
        self.assertEqual(
            list(self.issue.events.values_list("event", flat=True)),
            [TimelineEvent.OPENED, TimelineEvent.CLOSED, TimelineEvent.REOPENED],
        )

    def test_closing_as_duplicate_needs_the_other_issue(self):
        with self.assertRaises(services.IssueError):
            services.close_issue(
                self.issue, self.maintainer, reason=StateReason.DUPLICATE
            )

    def test_an_issue_cannot_duplicate_itself(self):
        with self.assertRaises(services.IssueError):
            services.close_issue(
                self.issue,
                self.maintainer,
                reason=StateReason.DUPLICATE,
                duplicate_of=self.issue,
            )

    def test_comments_bump_the_count_and_activity(self):
        before = self.issue.last_activity_at
        services.add_comment(self.issue, self.author, "Still broken")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.comment_count, 1)
        self.assertGreaterEqual(self.issue.last_activity_at, before)

    def test_deleting_a_comment_keeps_the_count_honest(self):
        comment = services.add_comment(self.issue, self.author, "One")
        services.add_comment(self.issue, self.author, "Two")
        services.delete_comment(comment)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.comment_count, 1)

    def test_a_locked_conversation_takes_no_comments(self):
        services.update_issue(self.issue, self.maintainer, locked=True)
        with self.assertRaises(services.IssueError):
            services.add_comment(self.issue, self.author, "Anything")

    def test_timeline_interleaves_comments_and_events(self):
        services.add_comment(self.issue, self.author, "Still broken")
        services.close_issue(self.issue, self.maintainer)
        kinds = [entry["kind"] for entry in services.timeline(self.issue)]
        self.assertEqual(kinds, ["event", "comment", "event"])


class SearchQuerysetTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice@example.com", full_name="Alice Kaur")
        self.bob = make_user("bob@example.com", full_name="Bob Singh")
        self.bug = IssueLabel.objects.create(name="bug", color="#d73a4a")
        self.ui = IssueLabel.objects.create(name="ui", color="#c5def5")
        self.dispatch = IssueArea.objects.create(name="Dispatch", code="dispatch")

        self.open_bug = services.create_issue(
            author=self.alice,
            title="Docking scan rejects a valid box",
            labels=[self.bug],
            area=self.dispatch,
            assignees=[self.bob],
            priority=IssuePriority.URGENT,
        )
        self.open_ui = services.create_issue(
            author=self.bob, title="Button overlaps on mobile", labels=[self.ui]
        )
        self.closed = services.create_issue(author=self.alice, title="Old thing")
        services.close_issue(self.closed, self.alice)

    def run_query(self, raw, viewer=None):
        parsed = parse_query(raw)
        return set(
            services.search_issues(parsed, viewer or self.alice).values_list(
                "number", flat=True
            )
        )

    def test_state_filter(self):
        self.assertEqual(
            self.run_query("is:open"), {self.open_bug.number, self.open_ui.number}
        )
        self.assertEqual(self.run_query("is:closed"), {self.closed.number})

    def test_label_filter(self):
        self.assertEqual(self.run_query("label:bug"), {self.open_bug.number})

    def test_repeated_labels_and_together(self):
        # Nothing carries both, so the result is empty rather than the union.
        self.assertEqual(self.run_query("label:bug label:ui"), set())

    def test_assignee_at_me(self):
        self.assertEqual(
            self.run_query("assignee:@me", viewer=self.bob), {self.open_bug.number}
        )

    def test_author_by_partial_name(self):
        self.assertEqual(
            self.run_query("author:Alice"), {self.open_bug.number, self.closed.number}
        )

    def test_assignee_nobody_matches_nothing(self):
        # An assignee filter that resolves to no user must return nothing, not
        # fall back to "no filter".
        self.assertEqual(self.run_query("assignee:nobodyhere"), set())

    def test_no_assignee(self):
        self.assertEqual(
            self.run_query("no:assignee"), {self.open_ui.number, self.closed.number}
        )

    def test_area_by_code(self):
        self.assertEqual(self.run_query("area:dispatch"), {self.open_bug.number})

    def test_free_text_hits_title(self):
        self.assertEqual(self.run_query("overlaps"), {self.open_ui.number})

    def test_priority(self):
        self.assertEqual(self.run_query("priority:urgent"), {self.open_bug.number})

    def test_state_counts_ignore_the_state_filter(self):
        parsed = parse_query("is:open")
        counts = services.state_counts(parsed, self.alice)
        self.assertEqual(counts, {"open": 2, "closed": 1})

    def test_pinned_issues_sort_first(self):
        services.update_issue(self.closed, self.alice, pinned=True)
        parsed = parse_query("")
        ordered = list(
            services.order_issues(
                services.search_issues(parsed, self.alice), ""
            ).values_list("number", flat=True)
        )
        self.assertEqual(ordered[0], self.closed.number)


class ApiTests(TestCase):
    def setUp(self):
        self.reporter = grant(
            make_user("reporter@example.com"),
            "can_view_issues",
            "can_create_issues",
        )
        self.maintainer = grant(
            make_user("maint@example.com"),
            "can_view_issues",
            "can_create_issues",
            "can_triage_issues",
        )
        self.outsider = make_user("nobody@example.com")
        self.bug = IssueLabel.objects.create(name="bug", color="#d73a4a")

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_outsider_cannot_read_the_list(self):
        response = self.client_for(self.outsider).get(reverse("issue-list"))
        self.assertEqual(response.status_code, 403)

    def test_reporter_can_file_an_issue(self):
        response = self.client_for(self.reporter).post(
            reverse("issue-list"),
            {
                "title": "Gate pass will not print",
                "body": "Nothing happens when I hit print.",
                "page_url": "/dispatch/gate-passes",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["number"], 1)
        self.assertEqual(response.data["author"]["email"], self.reporter.email)
        self.assertEqual(response.data["page_url"], "/dispatch/gate-passes")

    def test_reporter_cannot_label_even_their_own_issue(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        response = self.client_for(self.reporter).patch(
            reverse("issue-detail", args=[issue.number]),
            {"label_ids": [self.bug.pk]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_reporter_can_retitle_their_own_issue(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        response = self.client_for(self.reporter).patch(
            reverse("issue-detail", args=[issue.number]),
            {"title": "Mine, but clearer"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["title"], "Mine, but clearer")

    def test_reporter_cannot_edit_someone_elses_issue(self):
        issue = services.create_issue(author=self.maintainer, title="Theirs")
        response = self.client_for(self.reporter).patch
        result = response(
            reverse("issue-detail", args=[issue.number]),
            {"title": "Hijacked"},
            format="json",
        )
        self.assertEqual(result.status_code, 403)

    def test_reporter_can_close_their_own_issue(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        result = self.client_for(self.reporter).post(
            reverse("issue-state", args=[issue.number]),
            {"state": "CLOSED", "reason": "NOT_PLANNED"},
            format="json",
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["state"], "CLOSED")
        self.assertEqual(result.data["state_reason"], "NOT_PLANNED")

    def test_maintainer_can_label_and_assign(self):
        issue = services.create_issue(author=self.reporter, title="Theirs")
        result = self.client_for(self.maintainer).patch(
            reverse("issue-detail", args=[issue.number]),
            {"label_ids": [self.bug.pk], "assignee_ids": [self.maintainer.pk]},
            format="json",
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual([row["name"] for row in result.data["labels"]], ["bug"])
        self.assertEqual(len(result.data["assignees"]), 1)

    def test_only_triage_can_delete(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        url = reverse("issue-detail", args=[issue.number])
        self.assertEqual(self.client_for(self.reporter).delete(url).status_code, 403)
        self.assertEqual(self.client_for(self.maintainer).delete(url).status_code, 204)

    def test_comment_then_edit_then_delete(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        client = self.client_for(self.reporter)
        created = client.post(
            reverse("issue-comment-list", args=[issue.number]),
            {"body": "Happens every time"},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        comment_id = created.data["id"]

        edited = client.patch(
            reverse("issue-comment-detail", args=[comment_id]),
            {"body": "Happens every morning"},
            format="json",
        )
        self.assertEqual(edited.status_code, 200)
        self.assertIsNotNone(edited.data["edited_at"])

        removed = client.delete(reverse("issue-comment-detail", args=[comment_id]))
        self.assertEqual(removed.status_code, 204)
        issue.refresh_from_db()
        self.assertEqual(issue.comment_count, 0)

    def test_cannot_edit_another_users_comment(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        comment = services.add_comment(issue, self.reporter, "Mine")
        other = grant(
            make_user("other@example.com"), "can_view_issues", "can_create_issues"
        )
        result = self.client_for(other).patch(
            reverse("issue-comment-detail", args=[comment.pk]),
            {"body": "Not yours"},
            format="json",
        )
        self.assertEqual(result.status_code, 403)

    def test_list_reports_state_counts_and_unknown_qualifiers(self):
        services.create_issue(author=self.reporter, title="Open one")
        closed = services.create_issue(author=self.reporter, title="Closed one")
        services.close_issue(closed, self.maintainer)
        result = self.client_for(self.maintainer).get(
            reverse("issue-list"), {"q": "labels:bug", "state": "ALL"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["state_counts"], {"open": 1, "closed": 1})
        self.assertEqual(result.data["unknown_qualifiers"], ["labels:bug"])
        self.assertEqual(result.data["count"], 2)

    def test_list_defaults_to_open(self):
        services.create_issue(author=self.reporter, title="Open one")
        closed = services.create_issue(author=self.reporter, title="Closed one")
        services.close_issue(closed, self.maintainer)
        result = self.client_for(self.reporter).get(reverse("issue-list"))
        self.assertEqual(
            [row["title"] for row in result.data["results"]], ["Open one"]
        )

    def test_a_garbled_state_param_falls_back_to_open(self):
        services.create_issue(author=self.reporter, title="Open one")
        closed = services.create_issue(author=self.reporter, title="Closed one")
        services.close_issue(closed, self.maintainer)
        result = self.client_for(self.reporter).get(
            reverse("issue-list"), {"state": "SOMETHING"}
        )
        self.assertEqual([row["title"] for row in result.data["results"]], ["Open one"])

    def test_explicit_state_param_beats_the_query_string(self):
        closed = services.create_issue(author=self.reporter, title="Closed one")
        services.close_issue(closed, self.maintainer)
        result = self.client_for(self.reporter).get(
            reverse("issue-list"), {"q": "is:open", "state": "CLOSED"}
        )
        self.assertEqual([row["title"] for row in result.data["results"]], ["Closed one"])

    def test_timeline_endpoint(self):
        issue = services.create_issue(author=self.reporter, title="Mine")
        services.add_comment(issue, self.reporter, "A note")
        result = self.client_for(self.reporter).get(
            reverse("issue-timeline", args=[issue.number])
        )
        self.assertEqual(result.status_code, 200)
        kinds = [row["kind"] for row in result.data]
        self.assertEqual(kinds, ["event", "comment"])
        self.assertEqual(result.data[1]["comment"]["body"], "A note")

    def test_meta_reports_my_rights(self):
        result = self.client_for(self.reporter).get(reverse("issue-meta"))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.data["permissions"]["can_create"])
        self.assertFalse(result.data["permissions"]["can_triage"])
        self.assertEqual(result.data["me"]["email"], self.reporter.email)

    def test_reporter_cannot_write_the_label_master(self):
        result = self.client_for(self.reporter).post(
            reverse("issue-label-list"), {"name": "invented"}, format="json"
        )
        self.assertEqual(result.status_code, 403)

    def test_deleting_a_label_deactivates_it_and_keeps_history(self):
        admin = grant(
            make_user("admin@example.com"),
            "can_view_issues",
            "can_create_issues",
            "can_triage_issues",
            "can_manage_issue_settings",
        )
        issue = services.create_issue(
            author=self.reporter, title="Labelled", labels=[self.bug]
        )
        services.set_labels(issue, admin, [self.bug])
        result = self.client_for(admin).delete(
            reverse("issue-label-detail", args=[self.bug.pk])
        )
        self.assertEqual(result.status_code, 204)
        self.bug.refresh_from_db()
        self.assertFalse(self.bug.is_active)
        # The label row survives, so the frozen event still reads correctly.
        self.assertTrue(IssueLabel.objects.filter(pk=self.bug.pk).exists())

    def test_bulk_close_needs_triage(self):
        first = services.create_issue(author=self.reporter, title="One")
        second = services.create_issue(author=self.reporter, title="Two")
        url = reverse("issue-bulk-state")
        body = {"numbers": [first.number, second.number], "state": "CLOSED"}
        self.assertEqual(
            self.client_for(self.reporter).post(url, body, format="json").status_code,
            403,
        )
        result = self.client_for(self.maintainer).post(url, body, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["changed"], 2)
        self.assertEqual(Issue.objects.filter(state=IssueState.CLOSED).count(), 2)


# Uploads land on disk, so they go to a throwaway directory rather than the
# project's media/ -- a littered media folder has broken exact-filename
# assertions in this repo before (Django uniquifies a name that already exists).
_MEDIA = tempfile.mkdtemp(prefix="issue-test-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class AttachmentTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = grant(
            make_user("reporter@example.com"), "can_view_issues", "can_create_issues"
        )

    def test_rejects_an_executable(self):
        upload = SimpleUploadedFile("payload.exe", b"MZ", content_type="application/exe")
        with self.assertRaises(services.IssueError):
            services.validate_upload(upload)

    def test_accepts_a_screenshot(self):
        upload = SimpleUploadedFile("screen.png", b"\x89PNG", content_type="image/png")
        services.validate_upload(upload)  # does not raise

    def test_only_the_uploader_can_claim_an_upload(self):
        attachment = services.store_upload(
            SimpleUploadedFile("screen.png", b"\x89PNG", content_type="image/png"),
            self.user,
        )
        thief = make_user("thief@example.com")
        issue = services.create_issue(author=thief, title="Theirs")
        claimed = services.claim_attachments(
            [attachment.pk], user=thief, issue=issue
        )
        self.assertEqual(claimed, 0)
        attachment.refresh_from_db()
        self.assertIsNone(attachment.issue_id)

class ReporterGroupTests(TestCase):
    """Everybody can file. The group is the mechanism, so pin both halves of it:
    a new account picks it up on its own, and the backfill catches the accounts
    that existed before the group did."""

    def setUp(self):
        call_command("setup_issue_groups", stdout=StringIO())

    def test_a_new_account_can_file_without_anyone_granting_it(self):
        user = make_user("fresh@example.com")
        self.assertTrue(user.groups.filter(name=REPORTER_GROUP).exists())
        self.assertTrue(user.has_perm("issues.can_create_issues"))

    def test_user_creation_survives_a_missing_group(self):
        # A fresh database makes its first superuser before any seeding runs.
        Group.objects.filter(name=REPORTER_GROUP).delete()
        user = make_user("early@example.com")
        self.assertFalse(user.groups.exists())

    def test_backfill_covers_accounts_that_predate_the_group(self):
        Group.objects.filter(name=REPORTER_GROUP).delete()
        old = make_user("old@example.com")
        call_command("setup_issue_groups", stdout=StringIO())
        call_command("setup_issue_groups", "--assign-everyone", stdout=StringIO())
        old.refresh_from_db()
        self.assertTrue(old.has_perm("issues.can_create_issues"))

    def test_backfill_skips_triagers_and_repeats_harmlessly(self):
        maintainer = make_user("maintainer@example.com")
        maintainer.groups.clear()
        maintainer.groups.add(Group.objects.get(name="Issue Maintainer"))

        call_command("setup_issue_groups", "--assign-everyone", stdout=StringIO())
        call_command("setup_issue_groups", "--assign-everyone", stdout=StringIO())

        names = set(maintainer.groups.values_list("name", flat=True))
        self.assertEqual(names, {"Issue Maintainer"})
        self.assertTrue(maintainer.has_perm("issues.can_create_issues"))

    def test_dry_run_writes_nothing(self):
        Group.objects.filter(name=REPORTER_GROUP).delete()
        user = make_user("quiet@example.com")
        call_command("setup_issue_groups", stdout=StringIO())

        out = StringIO()
        call_command("setup_issue_groups", "--assign-everyone", "--dry-run", stdout=out)

        self.assertIn("quiet@example.com", out.getvalue())
        self.assertFalse(user.groups.exists())

    def test_deactivated_accounts_are_left_out(self):
        Group.objects.filter(name=REPORTER_GROUP).delete()
        leaver = make_user("leaver@example.com", is_active=False)
        call_command("setup_issue_groups", stdout=StringIO())
        call_command("setup_issue_groups", "--assign-everyone", stdout=StringIO())
        self.assertFalse(leaver.groups.exists())
