"""
control_boards/tests_reach.py

What a DASHBOARD-ONLY login can and cannot reach.

This is the file that decides whether the whole change worked. The positive half
-- "the boards open" -- is the easy half and would be noticed within a day if it
broke. The negative half is the one that matters: a login holding nothing but
board feed rights must be refused every operational endpoint, and if one of them
ever stops returning 403 nothing visible changes. The boards keep working, the
sidebar keeps looking right, and a read right has quietly become a key into a
module.

So: every assertion below that expects 403 is load-bearing. Adding an endpoint to
that list when you add a feed is part of adding the feed.

The boards themselves need SAP and HANA, so what is asserted for them is only
that the request was not REFUSED -- the permission decision is the part this app
owns. That is the same compromise ``admin_board/tests_carousel_permission.py``
makes, for the same reason.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from control_boards.feeds import FEEDS, right

User = get_user_model()


def _mint_feed_rights():
    """Run what the migration runs, so these tests exercise real rows."""
    ct, _ = ContentType.objects.get_or_create(
        app_label="control_boards", model="boardfeed"
    )
    for feed in FEEDS.values():
        Permission.objects.get_or_create(
            codename=feed.codename, content_type=ct, defaults={"name": feed.label}
        )


class _SignedIn(TestCase):
    """One company, one signed-in user, no permissions yet."""

    def setUp(self):
        _mint_feed_rights()
        self.client = APIClient()
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role, _ = UserRole.objects.get_or_create(name="Display")
        self.user = User.objects.create_user(
            email="board@example.com", full_name="Board Reader", password="x"
        )
        UserCompany.objects.create(
            user=self.user, company=company, role=role, is_default=True, is_active=True
        )
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def grant(self, *dotted):
        for d in dotted:
            app_label, codename = d.split(".", 1)
            self.user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)


class DashboardOnlyLoginTests(_SignedIn):
    """A login holding EVERY feed right and no operational right at all."""

    def setUp(self):
        super().setUp()
        self.grant(*(f.right for f in FEEDS.values()))

    def test_it_opens_the_composed_boards(self):
        for name in (
            "admin_board:admin-board",
            "plant_board:plant-board",
        ):
            with self.subTest(board=name):
                response = self.client.get(reverse(name))
                self.assertNotEqual(
                    response.status_code, 403, response.content[:400]
                )

    def test_it_is_refused_every_operational_report(self):
        """The list that must never shrink.

        These are the endpoints a "Dashboards" group used to have to grant, and
        each one also opens a module in the sidebar. A feed right buying any of
        them would put the original bug straight back.
        """
        for name in (
            "stock-dashboard",
            "non-moving-rm-report",
            "warehouse-occupancy",
            "owned-vehicle-status",
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.client.get(reverse(name)).status_code,
                    403,
                    f"{name} no longer refuses a dashboard-only login",
                )

    def test_it_is_refused_the_settings_views_behind_the_boards(self):
        """Reading a board must never carry editing what it reports.

        The board-settings endpoints are the sharp case: they accept PUT on the
        same right that opens the stock report, so honouring the ``stock`` feed
        right there would hand a wall screen a write.
        """
        for name in (
            "plant_board:plant-board-workforce",
            "plant_board:plant-board-space",
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.client.get(reverse(name)).status_code,
                    403,
                    f"{name} no longer refuses a dashboard-only login",
                )


class OneFeedIsNotEveryFeedTests(_SignedIn):
    """Holding one board's feeds must not open another board's endpoints."""

    def test_the_stock_feed_alone_does_not_open_the_returns_dashboard(self):
        self.grant(right("stock"))
        self.assertEqual(
            self.client.get(reverse("goods-return-dashboard")).status_code, 403
        )

    def test_the_returns_feed_alone_does_not_open_the_returns_list(self):
        """The dashboard is composed; the list is the module.

        This is the distinction the whole design rests on -- a board right buys
        the composed figures and never the rows behind them.
        """
        self.grant(right("goods_return"))
        self.assertNotEqual(
            self.client.get(reverse("goods-return-dashboard")).status_code, 403
        )
        self.assertEqual(
            self.client.get(reverse("goods-return-list-create")).status_code, 403
        )


class ExistingAccessIsUnchangedTests(_SignedIn):
    """The regression half: `|` widens and must never narrow.

    If any of these breaks, somebody who could read a board yesterday cannot
    read it today, which is a worse outcome than the bug being fixed.
    """

    def test_an_operational_right_still_opens_the_admin_board(self):
        self.grant("stock_dashboard.can_view_stock_dashboard")
        self.assertNotEqual(
            self.client.get(reverse("admin_board:admin-board")).status_code, 403
        )

    def test_an_operational_right_still_opens_the_plant_board(self):
        self.grant("production_execution.can_view_reports")
        self.assertNotEqual(
            self.client.get(reverse("plant_board:plant-board")).status_code, 403
        )

    def test_an_operational_right_still_opens_the_returns_dashboard(self):
        self.grant("goods_return.can_view_goods_return")
        self.assertNotEqual(
            self.client.get(reverse("goods-return-dashboard")).status_code, 403
        )

    def test_holding_nothing_is_still_refused_everywhere(self):
        for name in (
            "admin_board:admin-board",
            "plant_board:plant-board",
            "goods-return-dashboard",
        ):
            with self.subTest(board=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
