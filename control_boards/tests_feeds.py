"""
control_boards/tests_feeds.py

The tests that matter here are not "a feed right opens a board". They are the
ones that pin what a feed right still CANNOT do, because that is the half that
silently rots when somebody widens a permission class six months from now. A
board-only login that quietly gains the ability to read the dispatch bill list
is the exact failure this whole app exists to prevent, and nothing about the
board's own behaviour would change to reveal it.

Written to the convention in ``admin_board/tests_carousel_permission.py``:
exercise the real migration rather than a copy, pin permission strings as
literals because they are strings on both sides of the stack, and assert the
negative space at least as hard as the positive.
"""

from importlib import import_module

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from control_boards import feeds as feeds_module
from control_boards.feeds import (
    APP_LABEL,
    CONTENT_TYPE_MODEL,
    FEEDS,
    all_rights,
    may_read,
    readable,
    right,
    rights_for,
)
from control_boards.permissions import CanReadBoard
from control_boards.sections import SectionBuilder
from sap_client.exceptions import SAPConnectionError, SAPDataError

User = get_user_model()

# A module whose name starts with a digit cannot be imported with `import`.
migration = import_module("control_boards.migrations.0001_feed_rights")


class _Apps:
    """The two historical models the migration asks for, served live.

    The migration only touches ``auth`` and ``contenttypes``, neither of which
    has changed shape, so handing it the real models exercises the real code
    rather than a re-implementation of it.
    """

    @staticmethod
    def get_model(app_label, model_name):
        return {"contenttype": ContentType, "permission": Permission}[
            model_name.lower()
        ]


def _user(name):
    """This project authenticates on email, not username."""
    return User.objects.create_user(
        email=f"{name}@example.test", password="x", full_name=name
    )


def _grant(user, dotted):
    app_label, codename = dotted.split(".", 1)
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label=app_label, codename=codename)
    )
    # has_perm caches per instance; re-fetch so the next check is honest.
    return User.objects.get(pk=user.pk)


class CatalogueTests(TestCase):
    """Properties of the catalogue itself, as a pure data structure."""

    def test_every_right_lives_under_control_boards(self):
        """The app label IS the fix.

        A right minted inside an operational app would be prefix-matched by the
        frontend's ``hasModulePermission`` and put that module back in the
        sidebar -- the precise leak this app closes.
        """
        for name, feed in FEEDS.items():
            with self.subTest(feed=name):
                self.assertTrue(
                    feed.right.startswith(f"{APP_LABEL}."),
                    f"{name} mints {feed.right}, outside {APP_LABEL}",
                )

    def test_codenames_are_unique(self):
        codenames = [f.codename for f in FEEDS.values()]
        self.assertEqual(len(codenames), len(set(codenames)))

    def test_every_minted_right_is_a_read_right(self):
        """The rights this app mints are reads, and say so in their name.

        Groups are assembled out of these, so this is the rule that keeps
        "let them see the board" from becoming "let them move stock, link
        trucks and rewrite report SQL" -- the VIEW-RIGHTS-ONLY rule
        ``setup_dashboard_groups`` already follows, enforced at the source.
        """
        for name, feed in FEEDS.items():
            with self.subTest(feed=name):
                self.assertRegex(feed.codename, r"^can_read_[a-z_]+_feed$")

    def test_non_view_mirrors_are_an_explicit_allow_list(self):
        """Mirrors describe access as it IS, which is sometimes ugly.

        A mirror exists to stop anybody's access narrowing, so it has to name
        the right that really opens the endpoint today -- and six of those are
        not view rights. Each is listed here so adding a seventh is a decision
        somebody makes on purpose in review, rather than a regex quietly
        widening.

        Every entry is an ALTERNATIVE on an endpoint that also accepts a real
        view right, so none of them is the only way in.
        """
        licensed = {
            # GET on the expense board accepts either (CanReadOrConfigure).
            "factory_expense.can_configure_factory_expense",
            # Two of four alternatives on the employee meta read.
            "employee_hierarchy.can_manage_employees",
            "employee_hierarchy.can_manage_org_structure",
            # Two of three on the GRPO service-pending queue.
            "dispatch_plans.can_post_bilty_service_grpo",
            "grpo.add_grpoposting",
            # One of two on freight rate / transporter account.
            "dispatch_plans.can_post_transporter_ap_invoice",
        }
        for name, feed in FEEDS.items():
            for mirror in feed.mirrors:
                with self.subTest(feed=name, mirror=mirror):
                    if mirror in licensed:
                        continue
                    self.assertRegex(mirror, r"\.(can_view_|view_)")

    def test_write_rights_that_guard_reads_are_not_mirrored(self):
        """Three rights guard a read today despite being write rights.

        Mirroring them would carry that mistake forward into the new scheme and
        make a board viewer need a write right to see a number. Unlike the
        licensed list above, each of these is the ONLY way into its endpoint,
        so accepting it would mean granting the write.
        """
        never = {
            "goods_return.can_gate_in_goods_return",
            "labour_count.can_verify_labour_count",
            "sales_planning_requirement.can_refresh_sales_planning_requirement",
        }
        mirrored = {m for f in FEEDS.values() for m in f.mirrors}
        self.assertEqual(never & mirrored, set())

    def test_stock_feed_does_not_mirror_a_settings_write(self):
        """``WarehouseBoardSettingsAPI`` accepts PUT on the stock view right.

        So the ``stock`` feed right must never be honoured on that view -- a
        board service reads those settings through the model instead. The
        catalogue cannot enforce where a right is honoured; what it can pin is
        that the mirror set stayed exactly one read right.
        """
        self.assertEqual(
            FEEDS["stock"].mirrors, ("stock_dashboard.can_view_stock_dashboard",)
        )

    def test_wms_space_mirrors_nothing(self):
        """WMS reads are ungated today; this right is the first gate they get.

        If somebody later "fixes" this by pointing it at a wms permission, the
        gate becomes whatever that permission already means -- and today it
        means nothing, because ``WmsCollectionPermission`` allows every safe
        method. Pinned so the choice has to be made deliberately.
        """
        self.assertEqual(FEEDS["wms_space"].mirrors, ())

    def test_production_cost_is_separable_from_production_reports(self):
        """Output and what it cost are different rights upstream.

        Collapsing them would hand every shift supervisor the cost analysis.
        """
        self.assertNotEqual(
            FEEDS["production_cost"].mirrors, FEEDS["production_reports"].mirrors
        )
        self.assertIn(
            "production_execution.can_view_run_cost", FEEDS["production_cost"].mirrors
        )

    def test_helpers_agree_with_the_catalogue(self):
        self.assertEqual(right("stock"), "control_boards.can_read_stock_feed")
        self.assertEqual(len(all_rights()), len(FEEDS))
        self.assertEqual(all_rights(), tuple(sorted(all_rights())))
        self.assertEqual(
            rights_for("stock", "non_moving", "stock"),
            tuple(sorted({right("stock"), right("non_moving")})),
        )

    def test_unknown_feed_fails_loudly(self):
        """A typo must not read as "withheld" and show an empty tile."""
        with self.assertRaises(KeyError):
            right("stok")
        with self.assertRaises(KeyError):
            feeds_module.feed("no_such_feed")


class MigrationTests(TestCase):
    """The real migration functions, not a copy of them."""

    def _rows(self):
        return Permission.objects.filter(
            content_type__app_label=APP_LABEL,
            content_type__model=CONTENT_TYPE_MODEL,
        )

    def test_mints_every_feed_right(self):
        migration.add_permissions(_Apps, None)
        self.assertEqual(
            {p.codename for p in self._rows()}, {f.codename for f in FEEDS.values()}
        )

    def test_replay_is_harmless(self):
        migration.add_permissions(_Apps, None)
        before = self._rows().count()
        migration.add_permissions(_Apps, None)
        self.assertEqual(self._rows().count(), before)

    def test_reverses_cleanly(self):
        migration.add_permissions(_Apps, None)
        migration.remove_permissions(_Apps, None)
        self.assertEqual(self._rows().count(), 0)

    def test_reverse_before_forward_is_harmless(self):
        migration.remove_permissions(_Apps, None)
        self.assertEqual(self._rows().count(), 0)

    def test_creates_no_table(self):
        """The app owns no model, so nothing should have been created for it."""
        from django.apps import apps as django_apps

        self.assertEqual(
            list(django_apps.get_app_config("control_boards").get_models()), []
        )


class MayReadTests(TestCase):
    """The either/or that keeps today's users unchanged."""

    @classmethod
    def setUpTestData(cls):
        migration.add_permissions(_Apps, None)

    def test_nobody_reads_without_a_right(self):
        user = _user("nobody")
        self.assertFalse(may_read(user, "stock"))

    def test_the_board_right_reads(self):
        user = _user("board")
        user = _grant(user, right("stock"))
        self.assertTrue(may_read(user, "stock"))

    def test_the_mirrored_operational_right_still_reads(self):
        """The regression half: nobody's access narrows the day this ships."""
        user = _user("ops")
        user = _grant(user, "stock_dashboard.can_view_stock_dashboard")
        self.assertTrue(may_read(user, "stock"))

    def test_one_feed_right_does_not_open_another(self):
        user = _user("narrow")
        user = _grant(user, right("stock"))
        self.assertFalse(may_read(user, "dispatch_plans"))
        self.assertFalse(may_read(user, "factory_expense"))

    def test_anonymous_never_reaches_has_perm(self):
        class Anonymous:
            is_authenticated = False

            def has_perm(self, perm):  # pragma: no cover - must not be called
                raise AssertionError("has_perm reached for an anonymous user")

        self.assertFalse(may_read(Anonymous(), "stock"))
        self.assertFalse(may_read(None, "stock"))

    def test_readable_returns_only_the_held_feeds(self):
        user = _user("some")
        user = _grant(user, right("stock"))
        user = _grant(user, "non_moving_rm.can_view_non_moving_rm")
        self.assertEqual(
            readable(user, "stock", "non_moving", "freight"), {"stock", "non_moving"}
        )


class BoardGateTests(TestCase):
    """``CanReadBoard`` answers "may you open it", never "what may you see"."""

    @classmethod
    def setUpTestData(cls):
        migration.add_permissions(_Apps, None)

    def _check(self, klass, user):
        return klass().has_permission(type("R", (), {"user": user})(), None)

    def test_any_one_feed_opens_the_board(self):
        klass = CanReadBoard("stock", "non_moving", board="Warehouse Control")
        user = _user("one")
        user = _grant(user, right("non_moving"))
        self.assertTrue(self._check(klass, user))

    def test_none_of_them_is_refused(self):
        klass = CanReadBoard("stock", "non_moving", board="Warehouse Control")
        user = _user("none")
        self.assertFalse(self._check(klass, user))

    def test_an_unrelated_feed_does_not_open_it(self):
        klass = CanReadBoard("stock", board="Warehouse Control")
        user = _user("other")
        user = _grant(user, right("blowing"))
        self.assertFalse(self._check(klass, user))

    def test_feed_names_are_resolved_at_build_time(self):
        """A typo must fail on import, not read as "nobody may see this"."""
        with self.assertRaises(KeyError):
            CanReadBoard("stok", board="Typo")

    def test_a_board_must_name_a_feed(self):
        with self.assertRaises(ValueError):
            CanReadBoard(board="Empty")

    def test_the_gate_is_introspectable(self):
        """Tests and the groups command assert a gate matches its service."""
        klass = CanReadBoard("stock", "labour", board="Production Control")
        self.assertEqual(klass.board_feeds, ("stock", "labour"))
        self.assertEqual(klass.board_name, "Production Control")


class SectionBuilderTests(TestCase):
    """Withheld and degraded are different facts and must stay different lists.

    Reporting "you may not read this" as "the source is down" sends somebody to
    the server room over a permissions problem; reporting it the other way sends
    them to an administrator over a HANA outage.
    """

    @classmethod
    def setUpTestData(cls):
        migration.add_permissions(_Apps, None)

    def _board(self, user):
        class Board(SectionBuilder):
            def __init__(self, who):
                self.user = who
                self._init_sections()

        return Board(user)

    def test_a_held_feed_builds(self):
        user = _grant(_user("held"),
                      right("stock"))
        board = self._board(user)
        self.assertEqual(board.section("fg", lambda: 42, feed="stock"), 42)
        self.assertEqual(board.section_meta()["withheld"], [])

    def test_an_unheld_feed_is_withheld_not_degraded(self):
        user = _user("unheld")
        board = self._board(user)
        self.assertIsNone(board.section("fg", lambda: 42, feed="stock"))
        meta = board.section_meta()
        self.assertEqual(meta["withheld"], ["fg"])
        self.assertEqual(meta["degraded"], [])

    def test_a_withheld_section_never_runs_its_build(self):
        """Not just hidden -- not fetched. A wall board must not pay for a tile
        nobody is allowed to see, and the query must not touch SAP at all."""
        user = _user("nofetch")
        board = self._board(user)

        def build():  # pragma: no cover - must not be called
            raise AssertionError("build ran for a withheld section")

        self.assertIsNone(board.section("fg", build, feed="stock"))

    def test_a_failure_is_degraded_not_withheld(self):
        user = _grant(_user("fails"),
                      right("stock"))
        board = self._board(user)

        def build():
            raise SAPDataError("HANA said no")

        self.assertIsNone(board.section("fg", build, feed="stock"))
        meta = board.section_meta()
        self.assertEqual(meta["degraded"], ["fg"])
        self.assertEqual(meta["withheld"], [])

    def test_permission_is_decided_before_the_sap_latch(self):
        """A withheld tile reads as withheld even during an outage.

        Whether somebody is allowed to see a tile has nothing to do with
        whether SAP is answering, and a board that conflated the two would
        change its explanation depending on the weather.
        """
        user = _grant(_user("latch"),
                      right("non_moving"))
        board = self._board(user)

        def down():
            raise SAPConnectionError("no route to host")

        board.section("first", down, feed="non_moving")
        self.assertIsNone(board.section("second", lambda: 1, feed="stock"))
        meta = board.section_meta()
        self.assertEqual(meta["degraded"], ["first"])
        self.assertEqual(meta["withheld"], ["second"])

    def test_the_latch_stops_asking_after_one_outage(self):
        """One outage costs one timeout, not one per section."""
        user = _grant(_user("once"),
                      right("stock"))
        board = self._board(user)
        calls = []

        def down():
            calls.append(1)
            raise SAPConnectionError("no route to host")

        board.section("a", down, feed="stock")
        board.section("b", down, feed="stock")
        board.section("c", down, feed="stock")
        self.assertEqual(len(calls), 1)
        self.assertEqual(board.section_meta()["degraded"], ["a", "b", "c"])

    def test_needs_sap_false_survives_the_latch(self):
        user = _grant(_user("pg"),
                      right("stock"))
        board = self._board(user)
        board.section("a", lambda: (_ for _ in ()).throw(SAPConnectionError("x")),
                      feed="stock")
        self.assertEqual(
            board.section("b", lambda: 7, needs_sap=False, feed="stock"), 7
        )

    def test_no_user_withholds_nothing(self):
        """``user=None`` is how every existing service and test keeps working."""
        board = self._board(None)
        self.assertEqual(board.section("fg", lambda: 42, feed="stock"), 42)
        self.assertEqual(board.section_meta()["withheld"], [])

    def test_an_outage_says_so_in_prose(self):
        user = _grant(_user("prose"),
                      right("stock"))
        board = self._board(user)
        board.section("a", lambda: (_ for _ in ()).throw(SAPConnectionError("x")),
                      feed="stock")
        self.assertTrue(
            any("SAP did not answer" in w for w in board.section_meta()["warnings"])
        )

    def test_a_withheld_section_adds_no_warning(self):
        """An absent section is named in ``withheld``; it needs no prose, and
        prose about it would read to an operator as something being wrong."""
        user = _user("quiet")
        board = self._board(user)
        board.section("fg", lambda: 1, feed="stock")
        self.assertEqual(board.section_meta()["warnings"], [])
