"""The 0061 migration must keep Line Clearance QA's permissions with the groups
that hold them while it removes the unused flows' permissions and groups."""

import importlib

from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase

migration = importlib.import_module(
    "quality_control.migrations.0061_remove_unused_qc_flows"
)

VIEW = "can_view_line_clearance_qc"
APPROVE = "can_approve_line_clearance_qc"


class RemoveUnusedQCFlowsMigrationTests(TestCase):
    # The migration functions only read `schema_editor.connection.alias`.
    class _Editor:
        connection = connection

    def setUp(self):
        # What live has: the permissions declared on the model being removed.
        self.old_ct, _ = ContentType.objects.get_or_create(
            app_label="quality_control", model="productionqcsession"
        )
        self.new_ct = ContentType.objects.get(
            app_label="quality_control", model="rawmaterialinspection"
        )
        # The test database was built from the current models, which already
        # declare these on the new host; drop those to start from live's state.
        Permission.objects.filter(
            content_type=self.new_ct, codename__in=[VIEW, APPROVE]
        ).delete()
        self.old_view = Permission.objects.create(
            content_type=self.old_ct, codename=VIEW, name="Can view line clearance QC"
        )
        self.old_approve = Permission.objects.create(
            content_type=self.old_ct, codename=APPROVE, name="Can approve line clearance QC"
        )
        self.removed_perm = Permission.objects.create(
            content_type=self.old_ct, codename="can_view_production_qc",
            name="Can view production QC",
        )
        self.production_qc = Group.objects.create(name="Production QC")
        self.production_qc.permissions.add(self.old_view, self.old_approve, self.removed_perm)

    def _forwards(self):
        migration.move_line_clearance_permissions(global_apps, self._Editor())
        migration.drop_removed_permissions(global_apps, self._Editor())

    def _member(self, group, email="qc@t.com", code="E1"):
        user = get_user_model().objects.create_user(
            email=email, password="x", full_name="QC", employee_code=code
        )
        user.groups.add(group)
        return user

    def test_the_group_keeps_line_clearance_qc(self):
        user = self._member(self.production_qc)
        self._forwards()

        user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(user.has_perm(f"quality_control.{VIEW}"))
        self.assertTrue(user.has_perm(f"quality_control.{APPROVE}"))

    def test_the_permissions_move_to_the_inspection_model(self):
        self._forwards()

        self.assertFalse(Permission.objects.filter(content_type=self.old_ct).exists())
        moved = Permission.objects.get(content_type=self.new_ct, codename=VIEW)
        # Re-pointed, not recreated: the same row, so every grant followed it.
        self.assertEqual(moved.pk, self.old_view.pk)

    def test_grants_follow_when_the_new_row_already_exists(self):
        new_view = Permission.objects.create(
            content_type=self.new_ct, codename=VIEW, name="Can view line clearance QC"
        )
        user = self._member(self.production_qc)
        direct = self._member(Group.objects.create(name="other"), "d@t.com", "E2")
        direct.user_permissions.add(self.old_approve)

        self._forwards()

        self.assertIn(new_view, self.production_qc.permissions.all())
        self.assertFalse(Permission.objects.filter(pk=self.old_view.pk).exists())
        user = get_user_model().objects.get(pk=user.pk)
        direct = get_user_model().objects.get(pk=direct.pk)
        self.assertTrue(user.has_perm(f"quality_control.{VIEW}"))
        self.assertTrue(direct.has_perm(f"quality_control.{APPROVE}"))

    def test_the_removed_flows_permissions_are_dropped(self):
        user = self._member(self.production_qc)
        self._forwards()

        self.assertFalse(Permission.objects.filter(pk=self.removed_perm.pk).exists())
        user = get_user_model().objects.get(pk=user.pk)
        self.assertFalse(user.has_perm("quality_control.can_view_production_qc"))

    def test_emptied_groups_without_members_are_deleted(self):
        doomed = Permission.objects.create(
            content_type=self.old_ct, codename="can_view_qc_records", name="x"
        )
        Group.objects.create(name="QC Documents").permissions.add(doomed)
        Group.objects.create(name="QC Procedures")

        self._forwards()

        self.assertFalse(
            Group.objects.filter(name__in=["QC Documents", "QC Procedures"]).exists()
        )

    def test_an_emptied_group_with_members_is_kept(self):
        group = Group.objects.create(name="QC Documents")
        self._member(group)

        self._forwards()

        self.assertTrue(Group.objects.filter(name="QC Documents").exists())

    def test_running_twice_changes_nothing(self):
        self._forwards()
        self._forwards()

        self.assertEqual(
            set(self.production_qc.permissions.values_list("codename", flat=True)),
            {VIEW, APPROVE},
        )

    def test_reverse_moves_the_permissions_back(self):
        user = self._member(self.production_qc)
        self._forwards()
        migration.move_line_clearance_permissions_back(global_apps, self._Editor())

        self.assertEqual(
            set(
                Permission.objects.filter(content_type=self.old_ct).values_list(
                    "codename", flat=True
                )
            ),
            {VIEW, APPROVE},
        )
        user = get_user_model().objects.get(pk=user.pk)
        self.assertTrue(user.has_perm(f"quality_control.{APPROVE}"))
