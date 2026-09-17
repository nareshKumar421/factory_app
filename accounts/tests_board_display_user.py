"""
The display login a wall screen runs the board carousel under.

What is worth testing here is not that Django can create a user. It is the four
promises the command makes, every one of which is a promise about LIVE data:

  - it refuses to run before the group exists, rather than creating a login that
    can see nothing;
  - it never leaves a display account holding staff, superuser, a second group
    or a directly-held right — a wall screen's access has exactly one source;
  - re-running it is safe, and does not silently change a password;
  - ``--dry-run`` writes nothing at all.
"""

from io import StringIO

from django.contrib.auth.models import Group, Permission
from django.core.management import CommandError, call_command
from django.test import TestCase

from company.models import Company, UserCompany

from .management.commands.setup_dashboard_groups import build_groups
from .models import User

CAROUSEL_GROUP = "Dashboards — Board Carousel (display)"
# The same one right under the name the business asked for. Two names, one
# permission, on purpose — see the note in ``setup_dashboard_groups``.
BOARD_ONLY_GROUP = "Dashboards — Carousel Board Only"
FULL_GROUP = "Dashboards — Control Carousel"


class CarouselGroupTests(TestCase):
    """The group itself: derived from the three boards, never typed out."""

    def test_the_display_group_holds_exactly_one_right(self):
        """The whole point of it. A screen holds one permission, not ten."""
        self.assertEqual(
            build_groups()[CAROUSEL_GROUP],
            ["admin_board.can_view_board_carousel"],
        )

    def test_the_board_only_group_is_the_same_single_right(self):
        """Two names, one permission — and they must never drift apart.

        If this fails, somebody widened one of the two carousel-only groups
        without widening the other, and an administrator picking by name is now
        handing out something different from what the tooling hands out.
        """
        groups = build_groups()
        self.assertEqual(
            groups[BOARD_ONLY_GROUP],
            ["admin_board.can_view_board_carousel"],
        )
        self.assertEqual(groups[BOARD_ONLY_GROUP], groups[CAROUSEL_GROUP])

    def test_carousel_is_exactly_the_union_of_its_three_boards(self):
        groups = build_groups()
        carousel = set(groups[FULL_GROUP])
        union = (
            set(groups["Dashboards — Admin Control"])
            | set(groups["Dashboards — Plant Control"])
            | set(groups["Dashboards — Logistics Control"])
        )
        # Equality both ways on purpose: a carousel MISSING a right shows an
        # empty band on a wall, and a carousel with an EXTRA one is a disclosure
        # nobody reviewed.
        self.assertEqual(carousel, union)

    def test_carousel_grants_no_right_that_does_something(self):
        """View rights only — the header rule of the groups command.

        A wall screen that could move stock or link a truck would be one stray
        click from an operation nobody authorised, and there is nobody standing
        at it to notice.

        ``can_read_..._feed`` is the third accepted shape, and only the third
        shape: it is a ``control_boards`` board READ right, which by
        construction is honoured only inside a composed board endpoint and
        cannot reach an operational view at all. See control_boards/feeds.py.
        Anything that is not one of these three prefixes is a right that DOES
        something and has no business on a wall screen.
        """
        groups = build_groups()
        for code in groups[FULL_GROUP] + groups[CAROUSEL_GROUP] + groups[BOARD_ONLY_GROUP]:
            self.assertRegex(
                code,
                r"\.(can_view_|view_|can_read_)",
                msg=f"{code} is not a view right; it must not be on a wall screen",
            )

    def test_every_board_read_right_really_is_one(self):
        """The widened regex above must not let a stray codename through.

        ``can_read_`` is accepted because of what those rights ARE, not because
        of how they are spelled -- so pin that every code matching it is a real
        entry in the catalogue rather than something that merely looks like one.
        """
        from control_boards.feeds import all_rights

        catalogue = set(all_rights())
        groups = build_groups()
        for code in groups[FULL_GROUP] + groups[CAROUSEL_GROUP] + groups[BOARD_ONLY_GROUP]:
            if ".can_read_" in code:
                self.assertIn(code, catalogue, msg=f"{code} is not a known board feed")


class CreateBoardDisplayUserTests(TestCase):
    """The command that makes the screen's login."""

    EMAIL = "wall.board@example.com"

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        # A right that really exists in any install, so the group is not empty
        # and the command's "can see something" promise is actually exercised.
        self.perm = Permission.objects.first()
        self.group = Group.objects.create(name=CAROUSEL_GROUP)
        self.group.permissions.add(self.perm)

    def _run(self, **kwargs):
        out = StringIO()
        call_command("create_board_display_user", stdout=out, **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------------ gate #
    def test_refuses_when_the_group_does_not_exist(self):
        self.group.delete()
        with self.assertRaises(CommandError) as raised:
            self._run(email=self.EMAIL, company="JIVO_OIL")
        self.assertIn("setup_dashboard_groups", str(raised.exception))
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_refuses_an_unknown_company(self):
        with self.assertRaises(CommandError):
            self._run(email=self.EMAIL, company="NOPE")
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_requires_a_company_when_creating(self):
        with self.assertRaises(CommandError):
            self._run(email=self.EMAIL)

    # ----------------------------------------------------------- what it makes #
    def test_creates_an_unprivileged_login_in_one_group(self):
        self._run(email=self.EMAIL, company="JIVO_OIL", name="Canteen Wall")

        user = User.objects.get(email=self.EMAIL)
        self.assertEqual(user.full_name, "Canteen Wall")
        self.assertFalse(user.is_staff)
        # A superuser bypasses every permission check in Django, which would
        # make the group decorative and the wall able to read the whole product.
        self.assertFalse(user.is_superuser)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), [CAROUSEL_GROUP])
        self.assertEqual(user.user_permissions.count(), 0)

        link = UserCompany.objects.get(user=user)
        self.assertEqual(link.company, self.company)
        self.assertTrue(link.is_default)
        self.assertTrue(link.is_active)

    def test_prints_the_generated_password_once_and_it_works(self):
        output = self._run(email=self.EMAIL, company="JIVO_OIL")
        # The password is on its own line after "password"; pull it back out and
        # prove the screen could actually sign in with what was printed.
        printed = [line for line in output.splitlines() if line.strip().startswith("password ")]
        self.assertTrue(printed, msg=f"no password line in:\n{output}")
        password = printed[-1].split("password", 1)[1].strip()
        self.assertTrue(User.objects.get(email=self.EMAIL).check_password(password))

    def test_strips_a_group_and_a_direct_right_that_arrived_some_other_way(self):
        """This account's access has one source, and re-running restores that."""
        other = Group.objects.create(name="Some Other Group")
        self._run(email=self.EMAIL, company="JIVO_OIL")
        user = User.objects.get(email=self.EMAIL)
        user.groups.add(other)
        user.user_permissions.add(self.perm)

        self._run(email=self.EMAIL, company="JIVO_OIL")

        user.refresh_from_db()
        self.assertEqual(list(user.groups.values_list("name", flat=True)), [CAROUSEL_GROUP])
        self.assertEqual(user.user_permissions.count(), 0)

    # ------------------------------------------------------------ re-running #
    def test_rerunning_leaves_the_password_alone(self):
        self._run(email=self.EMAIL, company="JIVO_OIL", password="first-password")
        self._run(email=self.EMAIL, company="JIVO_OIL")

        user = User.objects.get(email=self.EMAIL)
        self.assertTrue(user.check_password("first-password"))

    def test_reset_password_changes_it_when_asked(self):
        self._run(email=self.EMAIL, company="JIVO_OIL", password="first-password")
        self._run(
            email=self.EMAIL,
            company="JIVO_OIL",
            password="second-password",
            reset_password=True,
        )

        user = User.objects.get(email=self.EMAIL)
        self.assertTrue(user.check_password("second-password"))

    def test_refuses_to_turn_a_privileged_account_into_a_screen(self):
        User.objects.create_user(
            email=self.EMAIL, full_name="A Real Person", password="x", is_staff=True
        )
        with self.assertRaises(CommandError):
            self._run(email=self.EMAIL, company="JIVO_OIL")

    def test_one_company_only(self):
        """A wall shows one plant, and the Logistics board follows the switcher."""
        second = Company.objects.create(name="Jivo Beverages", code="JIVO_BEV")
        self._run(email=self.EMAIL, company="JIVO_OIL")
        user = User.objects.get(email=self.EMAIL)

        self._run(email=self.EMAIL, company="JIVO_BEV")

        links = UserCompany.objects.filter(user=user)
        self.assertEqual(links.count(), 1)
        self.assertEqual(links.first().company, second)

    # -------------------------------------------------------------- dry runs #
    def test_dry_run_writes_nothing(self):
        self._run(email=self.EMAIL, company="JIVO_OIL", dry_run=True)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())

    def test_show_reports_without_writing(self):
        output = self._run(email=self.EMAIL, show=True)
        self.assertIn("no such account", output)
        self.assertFalse(User.objects.filter(email=self.EMAIL).exists())


class FeedRightsMustExistFirstTests(TestCase):
    """The command must refuse to run before `control_boards` 0001 is applied.

    WHY THIS IS THE MOST IMPORTANT TEST IN THIS FILE
    The composed boards' groups are built from feed rights. `_resolve` skips a
    code it cannot find, and a group is REPLACED rather than merged -- so
    running this command one step too early does not fail, it quietly empties
    Admin Control, Plant Control, Company Expense and Customer Returns and
    revokes those boards from everybody in them.

    The failure mode is silent, permanent until noticed, and looks exactly like
    a successful run. It was caught on a real dry-run against live; this is what
    stops it happening to somebody who does not read the output as carefully.
    """

    def _mint_feed_rights(self):
        from django.contrib.contenttypes.models import ContentType

        from control_boards.feeds import FEEDS

        ct, _ = ContentType.objects.get_or_create(
            app_label="control_boards", model="boardfeed"
        )
        for feed in FEEDS.values():
            Permission.objects.get_or_create(
                codename=feed.codename, content_type=ct, defaults={"name": feed.label}
            )

    def test_it_refuses_when_the_rights_are_not_minted(self):
        Permission.objects.filter(content_type__app_label="control_boards").delete()
        with self.assertRaises(CommandError) as caught:
            call_command("setup_dashboard_groups", stdout=StringIO())
        self.assertIn("migrate control_boards", str(caught.exception))

    def test_a_dry_run_is_refused_too(self):
        """A dry run that printed the emptying as if it were the plan would be
        worse than useless -- it would read as confirmation."""
        Permission.objects.filter(content_type__app_label="control_boards").delete()
        with self.assertRaises(CommandError):
            call_command("setup_dashboard_groups", "--dry-run", stdout=StringIO())

    def test_it_refuses_when_even_one_right_is_missing(self):
        """Partial is the dangerous case: most groups look right, one is empty."""
        self._mint_feed_rights()
        Permission.objects.filter(
            content_type__app_label="control_boards", codename="can_read_stock_feed"
        ).delete()
        with self.assertRaises(CommandError) as caught:
            call_command("setup_dashboard_groups", "--dry-run", stdout=StringIO())
        self.assertIn("can_read_stock_feed", str(caught.exception))

    def test_it_runs_once_the_rights_exist(self):
        self._mint_feed_rights()
        out = StringIO()
        call_command("setup_dashboard_groups", "--dry-run", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())

    def test_no_composed_board_group_ends_up_empty(self):
        """The property the guard exists to protect, asserted directly."""
        self._mint_feed_rights()
        call_command("setup_dashboard_groups", stdout=StringIO())
        for board in ("Admin Control", "Plant Control", "Company Expense", "Customer Returns"):
            with self.subTest(board=board):
                group = Group.objects.get(name=f"Dashboards — {board}")
                self.assertGreater(
                    group.permissions.count(), 0, f"{board} was emptied, not converted"
                )
