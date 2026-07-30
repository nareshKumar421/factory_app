"""
Activity Center tests.

The registry is the risky part — every entry names a model, fields and statuses by
string, so a rename elsewhere in the codebase would silently drop a job from everyone's
list. ``test_registry_is_consistent_with_models`` fails loudly in that case.
"""

from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import models as django_models
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Department
from company.models import Company, UserCompany, UserRole
from maintenance.models import MaintenanceWorkOrder

from .registry import ACTIVITY_SOURCES, OWNED, QUEUE
from .services import (
    completed_for_user,
    overview_all_users,
    pending_for_user,
    summary_for_user,
)

User = get_user_model()


class RegistryIntegrityTests(TestCase):
    """Every string in the registry must resolve against the real models."""

    def test_keys_are_unique(self):
        keys = [source.key for source in ACTIVITY_SOURCES]
        self.assertEqual(len(keys), len(set(keys)), "Duplicate activity source keys")

    def test_registry_is_consistent_with_models(self):
        problems = []
        for source in ACTIVITY_SOURCES:
            try:
                model = apps.get_model(*source.model.split("."))
            except LookupError:
                problems.append("%s: unknown model %s" % (source.key, source.model))
                continue

            field_names = {field.name for field in model._meta.get_fields()}

            for field in (source.owner_field, source.actor_field):
                if field and field not in field_names:
                    problems.append("%s: no field %r on %s" % (source.key, field, source.model))

            for field in (source.age_field, source.actor_date_field, source.company_field):
                if field and field not in field_names:
                    problems.append("%s: no field %r on %s" % (source.key, field, source.model))

            if source.reference_field and source.reference_field not in field_names:
                problems.append(
                    "%s: no reference field %r on %s"
                    % (source.key, source.reference_field, source.model)
                )

            if source.mode == OWNED and not source.owner_field:
                problems.append("%s: OWNED source without owner_field" % source.key)

            # The filter must be executable — this catches bad lookups and, for
            # choice fields, values that are not in the model's choices.
            try:
                model.objects.filter(**source.pending_filter).exists()
            except Exception as exc:  # pragma: no cover - diagnostic path
                problems.append("%s: pending_filter failed (%s)" % (source.key, exc))

            status_field = next(
                (f for f in model._meta.get_fields() if getattr(f, "name", None) == "status"),
                None,
            )
            if status_field is not None and getattr(status_field, "choices", None):
                valid = {choice[0] for choice in status_field.choices}
                wanted = set()
                if "status" in source.pending_filter:
                    wanted = {source.pending_filter["status"]}
                elif "status__in" in source.pending_filter:
                    wanted = set(source.pending_filter["status__in"])
                unknown = wanted - valid
                if unknown:
                    problems.append(
                        "%s: status %s not valid for %s"
                        % (source.key, sorted(unknown), source.model)
                    )

        self.assertEqual(problems, [], "Registry is out of sync:\n" + "\n".join(problems))

    def test_permissions_exist(self):
        missing = []
        for source in ACTIVITY_SOURCES:
            app_label, codename = source.permission.split(".", 1)
            if not Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).exists():
                missing.append(source.permission)
        self.assertEqual(sorted(set(missing)), [], "Registry references unknown permissions")


class ActivityScopingTests(TestCase):
    """Pending work must be attributed to the right person, and only to them."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code="TEST_CO")
        cls.role = UserRole.objects.create(name="Employee")
        cls.department = Department.objects.create(name="Utilities")

        cls.technician = User.objects.create_user(
            email="tech@example.com", full_name="Tech", employee_code="T1", password="x"
        )
        cls.approver = User.objects.create_user(
            email="approver@example.com", full_name="Approver", employee_code="A1", password="x"
        )
        cls.bystander = User.objects.create_user(
            email="nobody@example.com", full_name="Nobody", employee_code="N1", password="x"
        )
        for user in (cls.technician, cls.approver, cls.bystander):
            UserCompany.objects.create(
                user=user, company=cls.company, role=cls.role, is_default=True
            )

        approvers = Group.objects.create(name="WO Approvers")
        approvers.permissions.add(
            Permission.objects.get(
                content_type__app_label="maintenance", codename="can_approve_work_order"
            )
        )
        cls.approver.groups.add(approvers)

        starters = Group.objects.create(name="WO Doers")
        starters.permissions.add(
            Permission.objects.get(
                content_type__app_label="maintenance", codename="can_start_work_order"
            )
        )
        cls.technician.groups.add(starters)

        # Awaiting approval — a shared queue, nobody's personal job.
        cls.open_wo = MaintenanceWorkOrder.objects.create(
            company=cls.company,
            department=cls.department,
            work_order_no="WO-OPEN-1",
            title="Pump leaking",
            status="OPEN",
            created_by=cls.technician,
        )
        # Assigned to the technician — theirs alone.
        cls.assigned_wo = MaintenanceWorkOrder.objects.create(
            company=cls.company,
            department=cls.department,
            work_order_no="WO-ASSIGNED-1",
            title="Belt replacement",
            status="ASSIGNED",
            created_by=cls.approver,
            assigned_to=cls.technician,
        )

    def test_owned_item_goes_only_to_its_owner(self):
        keys = {item["record_id"] for item in pending_for_user(self.technician, self.company)}
        self.assertIn(self.assigned_wo.pk, keys)

        others = {item["record_id"] for item in pending_for_user(self.approver, self.company)}
        self.assertNotIn(self.assigned_wo.pk, others)

    def test_queue_item_goes_to_permission_holders_only(self):
        approver_items = pending_for_user(self.approver, self.company)
        self.assertIn(
            self.open_wo.pk,
            {item["record_id"] for item in approver_items if item["source_key"] == "wo_approve"},
        )

        self.assertEqual(pending_for_user(self.bystander, self.company), [])

    def test_queue_items_are_flagged_as_shared(self):
        item = next(
            item
            for item in pending_for_user(self.approver, self.company)
            if item["source_key"] == "wo_approve"
        )
        self.assertEqual(item["mode"], QUEUE)

    def test_owner_keeps_draft_even_without_permission(self):
        """A draft you created is yours to submit even if your access changed."""
        draft = MaintenanceWorkOrder.objects.create(
            company=self.company,
            department=self.department,
            work_order_no="WO-DRAFT-1",
            title="Draft job",
            status="DRAFT",
            created_by=self.bystander,
        )
        keys = {item["record_id"] for item in pending_for_user(self.bystander, self.company)}
        self.assertIn(draft.pk, keys)

    def test_completed_requires_the_user_to_be_the_recorded_actor(self):
        self.open_wo.status = "APPROVED"
        self.open_wo.approved_by = self.approver
        self.open_wo.approved_at = timezone.now()
        self.open_wo.save()

        since = timezone.now() - timedelta(hours=1)
        mine = completed_for_user(self.approver, self.company, since=since)
        self.assertIn(self.open_wo.pk, {item["record_id"] for item in mine})

        theirs = completed_for_user(self.technician, self.company, since=since)
        self.assertNotIn(self.open_wo.pk, {item["record_id"] for item in theirs})

    def test_summary_counts_match_the_lists(self):
        summary = summary_for_user(
            self.technician, self.company, since=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(summary["pending"], len(pending_for_user(self.technician, self.company)))
        self.assertEqual(summary["owned"] + summary["queued"], summary["pending"])

    def test_overdue_uses_the_source_threshold(self):
        old = timezone.now() - timedelta(days=10)
        MaintenanceWorkOrder.objects.filter(pk=self.assigned_wo.pk).update(created_at=old)
        item = next(
            item
            for item in pending_for_user(self.technician, self.company)
            if item["record_id"] == self.assigned_wo.pk
        )
        self.assertTrue(item["is_overdue"])

    def test_overview_separates_owned_from_shared_queue(self):
        rows = {row["user_id"]: row for row in overview_all_users(self.company)["users"]}
        self.assertGreaterEqual(rows[self.technician.pk]["owned_pending"], 1)
        self.assertGreaterEqual(rows[self.approver.pk]["queue_pending"], 1)
        self.assertEqual(rows[self.bystander.pk]["owned_pending"], 0)
        self.assertEqual(rows[self.bystander.pk]["queue_pending"], 0)


class ActivityApiTests(TestCase):
    """The API must not let a user read anybody else's work without the permission."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Api Co", code="API_CO")
        cls.role = UserRole.objects.create(name="Employee")
        cls.user = User.objects.create_user(
            email="me@example.com", full_name="Me", employee_code="M1", password="x"
        )
        cls.other = User.objects.create_user(
            email="other@example.com", full_name="Other", employee_code="O1", password="x"
        )
        for user in (cls.user, cls.other):
            UserCompany.objects.create(
                user=user, company=cls.company, role=cls.role, is_default=True
            )

        cls.own_group = Group.objects.create(name="AC Users")
        cls.own_group.permissions.add(
            Permission.objects.get(
                content_type__app_label="activity_center", codename="can_view_my_activities"
            )
        )
        cls.user.groups.add(cls.own_group)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def test_my_summary_requires_the_permission(self):
        self.assertEqual(
            self.client_for(self.other).get(reverse("activity-my-summary")).status_code, 403
        )
        self.assertEqual(
            self.client_for(self.user).get(reverse("activity-my-summary")).status_code, 200
        )

    def test_all_users_view_requires_supervisor_permission(self):
        self.assertEqual(
            self.client_for(self.user).get(reverse("activity-users")).status_code, 403
        )

        supervisors = Group.objects.create(name="AC Supervisors")
        supervisors.permissions.add(
            Permission.objects.get(
                content_type__app_label="activity_center", codename="can_view_all_activities"
            )
        )
        self.user.groups.add(supervisors)
        self.user.refresh_from_db()

        client = self.client_for(User.objects.get(pk=self.user.pk))
        self.assertEqual(client.get(reverse("activity-users")).status_code, 200)

    def test_user_detail_allows_self_but_not_others(self):
        client = self.client_for(self.user)
        self.assertEqual(
            client.get(reverse("activity-user-detail", args=[self.user.pk])).status_code, 200
        )
        self.assertEqual(
            client.get(reverse("activity-user-detail", args=[self.other.pk])).status_code, 403
        )

    def test_days_parameter_is_validated(self):
        client = self.client_for(self.user)
        self.assertEqual(
            client.get(reverse("activity-my-completed"), {"days": "abc"}).status_code, 400
        )
        self.assertEqual(
            client.get(reverse("activity-my-completed"), {"days": "999"}).status_code, 400
        )
        self.assertEqual(
            client.get(reverse("activity-my-completed"), {"days": "7"}).status_code, 200
        )

    def test_definitions_flags_what_the_caller_owns(self):
        response = self.client_for(self.user).get(reverse("activity-definitions"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), len(ACTIVITY_SOURCES))
        self.assertTrue(all("is_mine" in row for row in response.data))
