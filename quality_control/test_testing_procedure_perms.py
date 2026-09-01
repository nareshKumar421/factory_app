"""The 0042 data migration must actually put the new perms on the QC groups."""

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase

migration = importlib.import_module(
    "quality_control.migrations.0042_grant_testing_procedure_permissions"
)


class GrantTestingProcedurePermissionsTests(TestCase):
    # The migration functions only read `schema_editor.connection.alias`; a
    # real SQLite schema editor cannot be opened inside a TestCase transaction.
    class _Editor:
        connection = connection

    def _run_grant(self):
        migration.grant(global_apps, self._Editor())

    def _run_revoke(self):
        migration.revoke(global_apps, self._Editor())

    def test_grants_view_and_manage_to_manager_groups(self):
        group = Group.objects.create(name="qc_manager")
        self._run_grant()
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn(migration.VIEW, codenames)
        self.assertIn(migration.MANAGE, codenames)

    def test_grants_view_only_to_reader_groups(self):
        group = Group.objects.create(name="qc_chemist")
        self._run_grant()
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(codenames, {migration.VIEW})

    def test_creates_the_permission_rows_if_absent(self):
        Permission.objects.filter(
            codename__in=[migration.VIEW, migration.MANAGE],
            content_type__app_label="quality_control",
        ).delete()
        self._run_grant()
        self.assertEqual(
            Permission.objects.filter(
                codename__in=[migration.VIEW, migration.MANAGE],
                content_type__app_label="quality_control",
            ).count(),
            2,
        )

    def test_is_idempotent_and_skips_missing_groups(self):
        group = Group.objects.create(name="factory head")
        self._run_grant()
        self._run_grant()
        self.assertEqual(group.permissions.count(), 2)

    def test_reverse_revokes_the_grant(self):
        group = Group.objects.create(name="qc_manager")
        self._run_grant()
        self._run_revoke()
        self.assertEqual(group.permissions.count(), 0)
