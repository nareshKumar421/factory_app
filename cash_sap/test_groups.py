"""The groups, and the permissions they hand out (cash_book's own rule).

Every permission a group hands out must exist, and every permission a view
checks must be handed out by some group. Both groups also hold the cash
book's view right, because posting or configuring a book you cannot read is
useless. The migration's group step and the command must agree.
"""

import importlib

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase

from cash_sap import permissions as guards
from cash_sap.management.commands.setup_cash_sap_groups import CASH_SAP_GROUPS

seed = importlib.import_module("cash_sap.migrations.0002_seed_and_groups")


class CashSapGroupTests(TestCase):
    def setUp(self):
        call_command("setup_cash_sap_groups", verbosity=0)

    def test_every_granted_permission_exists(self):
        for codes in CASH_SAP_GROUPS.values():
            for code in codes:
                app_label, codename = code.split(".", 1)
                with self.subTest(code=code):
                    self.assertTrue(Permission.objects.filter(
                        content_type__app_label=app_label, codename=codename).exists())

    def test_both_groups_hold_their_right_and_the_view_right(self):
        for name, codes in CASH_SAP_GROUPS.items():
            group = Group.objects.get(name=name)
            held = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.select_related("content_type")}
            self.assertEqual(held, set(codes))
            self.assertIn("cash_book.can_view_cash_book", held)

    def test_every_checked_permission_is_granted(self):
        granted = {code for codes in CASH_SAP_GROUPS.values() for code in codes}
        self.assertEqual({guards.POST_PERMISSION, guards.SETTINGS_PERMISSION} - granted, set())

    def test_the_command_only_adds(self):
        group = Group.objects.get(name="Cash Book SAP Poster")
        extra = Permission.objects.get(content_type__app_label="cash_book", codename="can_manage_cash_book")
        group.permissions.add(extra)
        call_command("setup_cash_sap_groups", verbosity=0)
        self.assertTrue(group.permissions.filter(pk=extra.pk).exists())

    def test_the_migration_makes_the_same_groups(self):
        Group.objects.filter(name__in=list(CASH_SAP_GROUPS)).delete()
        seed.create_groups(django_apps, None)
        for name, codes in CASH_SAP_GROUPS.items():
            group = Group.objects.get(name=name)
            held = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.select_related("content_type")}
            self.assertEqual(held, set(codes))
