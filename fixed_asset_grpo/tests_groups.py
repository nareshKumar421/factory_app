"""The "Fixed Asset GRPO" group migration, run as a function.

The test settings disable migrations (the schema is built from the models),
so 0002 never runs in a test database. Its two functions are called directly
here against the real app registry, which is what ``RunPython`` hands them
on a migrated database.

* the group holds exactly this app's three rights (every right its views
  check), and running it twice adds nothing;
* grpo's own "grpo" group is left alone;
* reverse takes the rights back and drops the group only when that empties it.

    manage.py test fixed_asset_grpo.tests_groups --settings=config.sqlite_test_settings
"""

import importlib

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from .constants import FA_GRPO_GROUP, LINK_PERMISSION, POST_PERMISSION, VIEW_PERMISSION

MIGRATION = importlib.import_module("fixed_asset_grpo.migrations.0002_fixed_asset_grpo_group")


class GroupMigrationTests(TestCase):
    def test_the_group_holds_the_three_rights(self):
        MIGRATION.create_group(apps, None)
        MIGRATION.create_group(apps, None)

        group = Group.objects.get(name=FA_GRPO_GROUP)
        held = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
        self.assertEqual(held, {VIEW_PERMISSION, POST_PERMISSION, LINK_PERMISSION})

    def test_grpo_s_group_is_left_alone(self):
        grpo_group = Group.objects.create(name="grpo")
        grpo_group.permissions.add(Permission.objects.get(codename="add_grpoposting"))

        MIGRATION.create_group(apps, None)

        self.assertEqual(
            list(grpo_group.permissions.values_list("codename", flat=True)), ["add_grpoposting"]
        )

    def test_reverse_keeps_a_group_someone_added_to(self):
        MIGRATION.create_group(apps, None)
        group = Group.objects.get(name=FA_GRPO_GROUP)
        group.permissions.add(Permission.objects.get(codename="add_grpoposting"))

        MIGRATION.remove_group(apps, None)

        group.refresh_from_db()
        self.assertEqual(
            list(group.permissions.values_list("codename", flat=True)), ["add_grpoposting"]
        )

    def test_reverse_drops_the_group_it_emptied(self):
        MIGRATION.create_group(apps, None)
        MIGRATION.remove_group(apps, None)
        self.assertFalse(Group.objects.filter(name=FA_GRPO_GROUP).exists())
