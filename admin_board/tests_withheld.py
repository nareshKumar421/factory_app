"""
admin_board/tests_withheld.py

What a reader who holds only SOME of this board's feeds sees, and -- much more
importantly -- what the action centre says about the bands they do not.

WHY THIS FILE EXISTS SEPARATELY
``alerts.py`` needed no change to handle withholding: every rule already opens
with a truthiness guard, and a withheld section is ``None`` exactly like a
degraded one. That is a happy accident of good existing design, and an accident
is precisely the kind of thing that stops being true when somebody rewrites a
rule to read ``section.get(...)`` directly. The alternative is the failure
``alerts.py`` names in its own docstring: a silent all-clear derived from a read
that never happened, reporting a healthy plant on the strength of having learned
nothing about it.

"Nobody showed me the dispatch tile" is not "dispatch is fine".
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from admin_board.alerts import build_alerts
from admin_board.services import AdminBoardService
from control_boards.feeds import FEEDS, right

User = get_user_model()


def _mint_feed_rights():
    ct, _ = ContentType.objects.get_or_create(
        app_label="control_boards", model="boardfeed"
    )
    for feed in FEEDS.values():
        Permission.objects.get_or_create(
            codename=feed.codename, content_type=ct, defaults={"name": feed.label}
        )


def _user_with(*dotted):
    user = User.objects.create_user(
        email=f"{'_'.join(d.split('.')[-1] for d in dotted) or 'none'}@example.test",
        password="x",
        full_name="Board reader",
    )
    for d in dotted:
        app_label, codename = d.split(".", 1)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )
    return User.objects.get(pk=user.pk)


class AlertsIgnoreWithheldSectionsTests(TestCase):
    """A band nobody was allowed to read must generate no finding at all."""

    def test_a_withheld_production_band_raises_no_alert(self):
        board = {
            "output": {"production": None, "dispatch": None},
            "storage": {"fg": None, "pm": None, "oil": None},
            "cost": None,
            "meta": {"withheld": ["production", "dispatch"], "degraded": []},
        }
        self.assertEqual(build_alerts(board, today=date(2026, 9, 17)), [])

    def test_a_withheld_cost_band_does_not_read_as_nothing_to_price(self):
        """The cost rule fires on slices with no source.

        A withheld cost band has no slices at all, and must not be mistaken for
        a priced board that happens to have found nothing.
        """
        board = {
            "output": {"production": None, "dispatch": None},
            "storage": {"fg": None, "pm": None, "oil": None},
            "cost": None,
            "meta": {"withheld": ["cost"], "degraded": []},
        }
        keys = {a["key"] for a in build_alerts(board, today=date(2026, 9, 17))}
        self.assertNotIn("cost.unsourced", keys)

    def test_a_withheld_storage_band_is_not_reported_as_unrated(self):
        """"No rated capacity" is a thing to go and fix.

        Telling somebody to go and rate a warehouse they were never shown is a
        instruction they cannot act on and a defect they did not have.
        """
        board = {
            "output": {"production": None, "dispatch": None},
            "storage": {"fg": None, "pm": None, "oil": None},
            "cost": None,
            "meta": {"withheld": ["fg_storage", "pm_storage", "oil_storage"]},
        }
        keys = {a["key"] for a in build_alerts(board, today=date(2026, 9, 17))}
        self.assertNotIn("storage.unrated", keys)


class ServiceWithholdsPerFeedTests(TestCase):
    """The service decides per band, from the rights the reader actually holds."""

    @classmethod
    def setUpTestData(cls):
        _mint_feed_rights()

    def _build(self, user):
        """Compose with every reader stubbed, so nothing reaches SAP.

        Bands the reader IS allowed to see will fail and land in ``degraded``;
        that is fine and beside the point. What is being asserted is which bands
        never ran at all.
        """
        service = AdminBoardService(company_code="JIVO", user=user)
        return service.build()

    def test_a_stock_only_reader_is_withheld_the_cost_band(self):
        """The wage and power bill is the one disclosure worth pinning.

        ``admin_board/permissions.py`` records that a warehouse login holding
        only the stock right sees the factory's wage and power bill, and that
        the business accepted it knowingly. With feed rights that is no longer
        forced: a reader granted only the stock feed does not get cost.
        """
        user = _user_with(right("stock"))
        board = self._build(user)
        self.assertIsNone(board["cost"])
        self.assertIn("cost", board["meta"]["withheld"])

    def test_a_cost_reader_is_withheld_the_storage_bands(self):
        user = _user_with(right("factory_expense"))
        board = self._build(user)
        withheld = board["meta"]["withheld"]
        for band in ("fg_storage", "pm_storage", "oil_storage"):
            self.assertIn(band, withheld)

    def test_withheld_and_degraded_never_share_a_band(self):
        """The two lists answer different questions and must stay disjoint."""
        user = _user_with(right("stock"))
        meta = self._build(user)["meta"]
        self.assertEqual(set(meta["withheld"]) & set(meta["degraded"]), set())

    def test_the_mirrored_operational_right_is_not_withheld(self):
        """The regression half: today's users see exactly what they saw."""
        user = _user_with("factory_expense.can_view_factory_expense")
        board = self._build(user)
        self.assertNotIn("cost", board["meta"]["withheld"])

    def test_no_user_withholds_nothing(self):
        """Every existing caller builds this service without a request."""
        board = AdminBoardService(company_code="JIVO").build()
        self.assertEqual(board["meta"]["withheld"], [])
