"""
The board carousel's own right, and the limits on it.

The point of this right is that a wall screen holds ONE permission and reaches
nothing else. So the tests that matter are not "the display login can read the
board" — they are the ones that pin what it still cannot do, because that is the
half that silently rots when somebody widens a permission class later.
"""

from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from admin_board.carousel import BOARD_CAROUSEL_PERMISSION, holds_carousel_right
from company.models import Company, UserCompany, UserRole

User = get_user_model()

# A module whose name starts with a digit cannot be reached by `import`, and
# every migration's is. Imported by string so these tests run the real thing.
carousel_migration = import_module("admin_board.migrations.0001_board_carousel_permission")


def mint_carousel_permission():
    """Create the right by running the migration's own code.

    This suite runs under settings that build the schema from models rather than
    replaying migrations (see ``config/test_settings.py``), so the data migration
    never fires and the permission row would not exist. Calling the migration's
    function directly is better than hand-creating the row anyway: it means these
    tests exercise the code that will actually run against live, rather than a
    copy of it that can drift.

    ``apps.get_model`` works the same on the real registry as on a migration's
    historical one for these two built-in models.
    """
    carousel_migration.add_permission(django_apps, None)
    app_label, codename = BOARD_CAROUSEL_PERMISSION.split(".", 1)
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


class CarouselPermissionMigrationTests(TestCase):
    """The migration is the only thing that creates this row on live."""

    def test_it_mints_the_right(self):
        permission = mint_carousel_permission()
        self.assertEqual(permission.codename, "can_view_board_carousel")
        self.assertEqual(permission.content_type.app_label, "admin_board")

    def test_running_it_twice_is_harmless(self):
        # Migrations get replayed — on a rebuilt environment, or by a colleague
        # who is not sure whether it ran. A second row would be a duplicate right
        # that half the groups point at.
        mint_carousel_permission()
        mint_carousel_permission()
        self.assertEqual(
            Permission.objects.filter(
                content_type__app_label="admin_board", codename="can_view_board_carousel"
            ).count(),
            1,
        )

    def test_it_reverses_cleanly(self):
        mint_carousel_permission()
        carousel_migration.remove_permission(django_apps, None)
        self.assertFalse(
            Permission.objects.filter(
                content_type__app_label="admin_board", codename="can_view_board_carousel"
            ).exists()
        )

    def test_it_hangs_off_admin_board_and_nothing_else(self):
        # If it ever moves, every permission string in the frontend and in
        # setup_dashboard_groups goes stale silently — they are strings.
        self.assertEqual(BOARD_CAROUSEL_PERMISSION, "admin_board.can_view_board_carousel")


class HoldsCarouselRightTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="screen@example.com", full_name="Wall", password="x"
        )

    def test_false_for_a_login_without_it(self):
        self.assertFalse(holds_carousel_right(self.user))

    def test_true_once_granted(self):
        self.user.user_permissions.add(mint_carousel_permission())
        self.assertTrue(holds_carousel_right(User.objects.get(pk=self.user.pk)))

    def test_false_for_nobody(self):
        """An unauthenticated request must not slip through the helper."""

        class Anonymous:
            is_authenticated = False

            def has_perm(self, _perm):  # pragma: no cover - must never be reached
                raise AssertionError("permission checked on an anonymous user")

        self.assertFalse(holds_carousel_right(Anonymous()))
        self.assertFalse(holds_carousel_right(None))


class DisplayLoginReachTests(TestCase):
    """What a login holding ONLY the carousel right can and cannot reach.

    The board reads themselves need SAP and HANA, so they are not called here —
    what is asserted is the permission decision, which is the part this change
    owns. A 403 is the failure this must never regress into; anything else means
    the gate let the request through to the service.
    """

    def setUp(self):
        self.client = APIClient()
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role, _ = UserRole.objects.get_or_create(name="Display")
        self.user = User.objects.create_user(
            email="screen@example.com", full_name="Wall", password="x"
        )
        UserCompany.objects.create(
            user=self.user, company=company, role=role, is_default=True, is_active=True
        )
        self.user.user_permissions.add(mint_carousel_permission())
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def test_it_is_not_refused_the_admin_board(self):
        response = self.client.get(reverse("admin_board:admin-board"))
        self.assertNotEqual(response.status_code, 403, response.content[:400])

    def test_it_is_not_refused_the_plant_board(self):
        response = self.client.get(reverse("plant_board:plant-board"))
        self.assertNotEqual(response.status_code, 403, response.content[:400])

    def test_it_is_still_refused_the_reports_behind_those_boards(self):
        """The whole point: one right, and it opens one thing.

        These are the operational endpoints the old ten-right display group
        opened. If any of them stops returning 403, somebody has widened a
        permission class that guards a whole module and the "one permission" has
        quietly become a key into it.
        """
        for name in (
            "stock-dashboard",
            "non-moving-rm-report",
            "warehouse-occupancy",
            "owned-vehicle-status",
            # Plant's settings views, which the carousel deliberately did NOT
            # widen: reading the board must not carry editing what it reports.
            "plant_board:plant-board-workforce",
            "plant_board:plant-board-space",
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.client.get(reverse(name)).status_code,
                    403,
                    f"{name} no longer refuses a carousel-only login",
                )


class BoardRightsAreUnchangedTests(TestCase):
    """Widening with `|` must take nothing away from the people already there."""

    def setUp(self):
        self.client = APIClient()
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role, _ = UserRole.objects.get_or_create(name="Admin")
        self.user = User.objects.create_user(
            email="person@example.com", full_name="A Person", password="x"
        )
        UserCompany.objects.create(
            user=self.user, company=company, role=role, is_default=True, is_active=True
        )
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def test_a_board_right_still_opens_its_board_without_the_carousel_right(self):
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="stock_dashboard", codename="can_view_stock_dashboard"
            )
        )
        response = self.client.get(reverse("admin_board:admin-board"))
        self.assertNotEqual(response.status_code, 403, response.content[:400])

    def test_holding_neither_is_still_refused(self):
        self.assertEqual(self.client.get(reverse("admin_board:admin-board")).status_code, 403)
        self.assertEqual(self.client.get(reverse("plant_board:plant-board")).status_code, 403)
