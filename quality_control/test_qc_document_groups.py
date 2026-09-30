"""The 0049 migration must create one usable group per QC document page.

Only the PDF library is left of the three pages it was written for: testing
procedures and the fillable record sheets were removed, and with their
permissions gone the migration skips their groups rather than creating them
half-empty.
"""

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import TestCase

migration = importlib.import_module(
    "quality_control.migrations.0049_qc_document_page_groups"
)


class QCDocumentPageGroupTests(TestCase):
    # The migration functions only read `schema_editor.connection.alias`; a
    # real SQLite schema editor cannot be opened inside a TestCase transaction.
    class _Editor:
        connection = connection

    def _forwards(self):
        migration.forwards(global_apps, self._Editor())

    def _backwards(self):
        migration.backwards(global_apps, self._Editor())

    PDF_GROUP = "QC PDF Documents"
    REMOVED_PAGE_GROUPS = ("QC Procedures", "QC Documents")

    def test_creates_the_pdf_library_group(self):
        self._forwards()
        self.assertTrue(Group.objects.filter(name=self.PDF_GROUP).exists())

    def test_the_removed_pages_get_no_group(self):
        self._forwards()
        self.assertFalse(Group.objects.filter(name__in=self.REMOVED_PAGE_GROUPS).exists())

    def test_the_group_carries_exactly_its_pages_permissions(self):
        self._forwards()
        group = Group.objects.get(name=self.PDF_GROUP)
        granted = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(granted, set(migration.GROUPS[self.PDF_GROUP]))

    def test_running_twice_changes_nothing(self):
        self._forwards()
        self._forwards()
        group = Group.objects.get(name=self.PDF_GROUP)
        self.assertEqual(group.permissions.count(), len(migration.GROUPS[self.PDF_GROUP]))
        self.assertEqual(Group.objects.filter(name__in=migration.GROUPS).count(), 1)

    def test_a_user_in_the_group_gains_its_permissions(self):
        from django.contrib.auth import get_user_model

        self._forwards()
        User = get_user_model()
        user = User.objects.create_user(
            email="pdfonly@t.com", password="x", full_name="PDF Only", employee_code="E1"
        )
        user.groups.add(Group.objects.get(name=self.PDF_GROUP))
        user = User.objects.get(pk=user.pk)

        self.assertTrue(user.has_perm("quality_control.can_view_document_files"))
        self.assertTrue(user.has_perm("quality_control.can_manage_document_files"))
        self.assertFalse(user.has_perm("quality_control.can_view_document_file_audit"))

    def test_reverse_removes_an_unused_group(self):
        self._forwards()
        self._backwards()
        self.assertFalse(
            Group.objects.filter(name__in=migration.GROUPS).exists()
        )

    def test_reverse_keeps_a_group_that_has_members(self):
        from django.contrib.auth import get_user_model

        self._forwards()
        User = get_user_model()
        user = User.objects.create_user(
            email="member@t.com", password="x", full_name="Member", employee_code="E2"
        )
        group = Group.objects.get(name=self.PDF_GROUP)
        user.groups.add(group)

        self._backwards()

        group.refresh_from_db()
        self.assertTrue(Group.objects.filter(name=self.PDF_GROUP).exists())
        self.assertEqual(group.permissions.count(), 0)
        self.assertTrue(user.groups.filter(name=self.PDF_GROUP).exists())

    def test_missing_permission_rows_are_recreated_before_the_grant(self):
        """The real post_migrate hazard: the permission rows may not exist yet.

        Custom permissions are normally created by a signal that fires *after*
        data migrations, so the migration forces them into existence first.
        Deleting them here reproduces that state.
        """
        codenames = migration.GROUPS[self.PDF_GROUP]
        Permission.objects.filter(
            codename__in=codenames, content_type__app_label="quality_control"
        ).delete()
        self.assertEqual(
            Permission.objects.filter(
                codename__in=codenames, content_type__app_label="quality_control"
            ).count(),
            0,
        )

        self._forwards()

        group = Group.objects.get(name=self.PDF_GROUP)
        self.assertEqual(
            set(group.permissions.values_list("codename", flat=True)), set(codenames)
        )
