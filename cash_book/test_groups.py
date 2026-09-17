"""The role groups, and the permissions they hand out.

These tests exist because of a bug they would have caught. The approve
permission was declared on the bunch; when the bunch stopped being an approval
its model options were rewritten, and the permission went with them. Nothing
declared it afterwards, so a fresh database never created it -- and
``has_perm`` answers False for a permission that does not exist, silently, for
everybody but a superuser. The groups command would have printed a warning
nobody was reading.

So the rule here is: every permission a group hands out must be a real one, and
every permission the API checks must be handed out by some group. A permission
nobody can be granted is a locked door, and one nobody checks is a lie.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase

from cash_book import permissions as guards
from cash_book.management.commands.setup_cash_book_groups import CASH_BOOK_GROUPS


class CashBookGroupTests(TestCase):
    """What `setup_cash_book_groups` creates."""

    def setUp(self):
        call_command("setup_cash_book_groups", verbosity=0)

    def test_every_permission_a_group_grants_actually_exists(self):
        """The bug this file was written for: a group handing out a ghost."""
        wanted = {code for codes in CASH_BOOK_GROUPS.values() for code in codes}
        for code in wanted:
            app_label, codename = code.split(".", 1)
            with self.subTest(code=code):
                self.assertTrue(
                    Permission.objects.filter(
                        content_type__app_label=app_label, codename=codename
                    ).exists(),
                    f"{code} is handed out by a group but no model declares it, "
                    f"so nobody can ever hold it.",
                )

    def test_every_permission_the_api_checks_is_granted_by_some_group(self):
        """A permission no group grants is a door with no key."""
        checked = {
            guards.VIEW_PERMISSION,
            guards.MANAGE_PERMISSION,
            guards.APPROVE_PERMISSION,
            guards.BRANCHES_PERMISSION,
        }
        granted = {code for codes in CASH_BOOK_GROUPS.values() for code in codes}
        self.assertEqual(
            checked - granted,
            set(),
            "the API checks these, and no group hands them out",
        )

    def test_the_four_roles_are_created_with_their_permissions(self):
        for name, codes in CASH_BOOK_GROUPS.items():
            with self.subTest(group=name):
                group = Group.objects.get(name=name)
                held = {
                    f"cash_book.{codename}"
                    for codename in group.permissions.values_list(
                        "codename", flat=True
                    )
                }
                self.assertEqual(held, set(codes))

    def test_custodian_and_approver_stay_apart(self):
        """The one separation the module is built around."""
        custodian = set(CASH_BOOK_GROUPS["Cash Book Custodian"])
        approver = set(CASH_BOOK_GROUPS["Cash Book Approver"])
        self.assertNotIn(guards.APPROVE_PERMISSION, custodian)
        self.assertNotIn(guards.MANAGE_PERMISSION, approver)

    def test_every_role_can_read_the_book(self):
        """Approving or configuring without being able to read is useless."""
        for name, codes in CASH_BOOK_GROUPS.items():
            with self.subTest(group=name):
                self.assertIn(guards.VIEW_PERMISSION, codes)

    def test_running_it_again_changes_nothing(self):
        """It is run at every deploy, so it has to be safe to repeat."""
        before = {
            group.name: sorted(group.permissions.values_list("codename", flat=True))
            for group in Group.objects.filter(name__startswith="Cash Book")
        }
        call_command("setup_cash_book_groups", verbosity=0)
        after = {
            group.name: sorted(group.permissions.values_list("codename", flat=True))
            for group in Group.objects.filter(name__startswith="Cash Book")
        }
        self.assertEqual(before, after)
        self.assertEqual(Group.objects.filter(name__startswith="Cash Book").count(), 4)

    def test_it_does_not_hand_out_a_retired_permission(self):
        """Neither of the two 0007 buried."""
        granted = {code for codes in CASH_BOOK_GROUPS.values() for code in codes}
        for retired in (
            "cash_book.can_approve_cash_bunch",
            "cash_book.can_manage_cash_advances",
        ):
            self.assertNotIn(retired, granted)
