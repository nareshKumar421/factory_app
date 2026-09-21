"""
board_builder/tests.py

What is pinned here, and why these and not others.

This app's whole promise is that an author can arrange anything they like and
still not widen anybody's access. The tests that matter are therefore the
NEGATIVE ones: a card cannot be placed by somebody who may not read it, a
published board does not become a grant, a private board does not exist for
anybody else, and a card that fails does not take the board with it. Those are
the properties that rot silently when somebody adds a convenience six months
from now, and none of them would change the look of a working board if they
broke.

The geometry tests are here for a different reason. Overlap and overflow are
the two failures that render as a wrong board rather than an error -- two
cards on one square, or a tile hanging off a wall screen's edge with nothing
anywhere saying so -- so they are checked on the way in AND on the way out,
and both checks are pinned.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from control_boards.feeds import right as feed_right

from . import catalogue, viz
from .catalogue import CardOption, CardSpec
from .constants import CARD_ACCENTS, GRID_MAX_COLUMNS, PAGE_MAX_ROWS, WALL_MAX_ROWS
from .models import BoardMode, BoardPlacement, BoardVisibility, CustomBoard
from .permissions import BUILD_PERMISSION, may_open, readable_cards
from .services import CustomBoardService

User = get_user_model()


def _user(name: str) -> "User":
    """This project authenticates on email, not username."""
    return User.objects.create_user(
        email=f"{name}@example.test", password="x", full_name=name
    )


def _grant(user, dotted: str):
    app_label, codename = dotted.split(".", 1)
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label=app_label, codename=codename)
    )
    # has_perm caches per instance; re-fetch so the next check is honest.
    return User.objects.get(pk=user.pk)


class _Base(TestCase):
    """One company, one member, and a client that sends the company header."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.role = UserRole.objects.create(name="Admin")

    def member(self, name: str, *, company=None):
        user = _user(name)
        UserCompany.objects.create(
            user=user, company=company or self.company, role=self.role
        )
        return user

    def client_for(self, user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=(self.company.code))
        return client

    def board(self, owner, **kwargs) -> CustomBoard:
        defaults = {
            "company": self.company,
            "owner": owner,
            "name": "Gate wall",
            "slug": "gate-wall",
            "columns": 4,
            "rows": 3,
        }
        defaults.update(kwargs)
        return CustomBoard.objects.create(**defaults)


# ---------------------------------------------------------------------------
# The catalogue as a data structure
# ---------------------------------------------------------------------------


class CatalogueTests(TestCase):
    def test_every_card_fits_a_wall(self):
        """A card too tall for the smallest board can never be placed.

        ``CardSpec.__post_init__`` already refuses one at import, so this
        asserts the rule holds across the whole registered set rather than
        just at the moment somebody wrote a card.
        """
        for spec in catalogue.all_cards():
            with self.subTest(card=spec.key):
                self.assertLessEqual(spec.columns, GRID_MAX_COLUMNS)
                self.assertLessEqual(spec.rows, WALL_MAX_ROWS)

    def test_every_card_uses_a_known_accent(self):
        for spec in catalogue.all_cards():
            with self.subTest(card=spec.key):
                self.assertIn(spec.accent, CARD_ACCENTS)

    def test_only_layout_cards_may_hold_no_feed(self):
        """The rule from ``catalogue.py``, enforced rather than described.

        A card with ``feed=None`` is readable by anybody who can open the
        board it is on. That is correct for furniture and a disclosure for
        anything that touches a table, so the exemption is an allow-list of
        one category and adding to it has to be deliberate.
        """
        for spec in catalogue.all_cards():
            if spec.feed is None:
                with self.subTest(card=spec.key):
                    self.assertEqual(
                        spec.category,
                        "Layout",
                        f"{spec.key} reads data but names no feed",
                    )

    def test_every_feed_named_by_a_card_exists(self):
        """A typo in a feed name reads as 'withheld' for everybody, forever.

        Nothing else would report it: the card would simply never render, for
        any user, with a message saying they lack a right that does not exist.
        """
        for spec in catalogue.all_cards():
            if spec.feed:
                with self.subTest(card=spec.key):
                    # Raises KeyError on an unknown feed.
                    feed_right(spec.feed)

    def test_card_keys_are_stable_identifiers(self):
        """Keys are stored on every placement, so they may not carry spaces
        or capitals that a later refactor would be tempted to tidy."""
        for spec in catalogue.all_cards():
            with self.subTest(card=spec.key):
                self.assertRegex(spec.key, r"^[a-z][a-z0-9_]*$")


class CardOptionTests(TestCase):
    def test_an_unknown_choice_falls_back_rather_than_raising(self):
        """A retired dropdown value must not stop a board opening.

        The board is the thing people rely on; the option is chrome. Reverting
        to the default and rendering is strictly better than 500-ing because
        somebody's saved window size was removed in a later release.
        """
        option = CardOption(
            key="window_days",
            label="Window",
            kind="choice",
            default="30",
            choices=(("7", "7"), ("30", "30")),
        )
        self.assertEqual(option.coerce("365"), "30")
        self.assertEqual(option.coerce(None), "30")
        self.assertEqual(option.coerce("7"), "7")

    def test_integers_are_clamped_to_their_bounds(self):
        option = CardOption(
            key="lines", label="Lines", kind="int", default=4, minimum=2, maximum=8
        )
        self.assertEqual(option.coerce(99), 8)
        self.assertEqual(option.coerce(0), 2)
        self.assertEqual(option.coerce("not a number"), 4)

    def test_undeclared_options_are_dropped(self):
        spec = catalogue.require("gate_truck_turnaround")
        coerced = spec.coerce_options({"window_days": "7", "left_over": "junk"})
        self.assertEqual(coerced, {"window_days": "7"})


class VizTests(TestCase):
    def test_a_card_must_return_a_value_or_a_reason(self):
        """The boards' one hard rule: an empty warehouse and an unreadable one
        must not look the same."""
        with self.assertRaises(ValueError):
            viz.validate(viz.figure(value=""))

    def test_an_unknown_shape_is_refused(self):
        with self.assertRaises(ValueError):
            viz.validate(viz.figure(value="1", viz={"kind": "sunburst"}))

    def test_missing_is_not_a_zero(self):
        payload = viz.missing("Nobody has configured a rate.")
        self.assertEqual(payload["value"], "")
        self.assertIsNotNone(payload["missing"])


# ---------------------------------------------------------------------------
# Who may place what
# ---------------------------------------------------------------------------


class PaletteTests(_Base):
    def test_the_palette_hides_cards_the_author_may_not_read(self):
        """Filtered, not greyed out.

        Listing a card by name tells somebody what exists behind a wall they
        cannot open. Small, but free to avoid.
        """
        author = self.member("author")
        author = _grant(author, BUILD_PERMISSION)

        offered = {spec.key for spec in readable_cards(author)}
        self.assertIn("section_label", offered)  # gates nothing
        self.assertNotIn("gate_truck_turnaround", offered)

        author = _grant(author, feed_right("gate"))
        offered = {spec.key for spec in readable_cards(author)}
        self.assertIn("gate_truck_turnaround", offered)

    def test_the_catalogue_endpoint_needs_the_build_right(self):
        reader = self.member("reader")
        response = self.client_for(reader).get(reverse("board_builder:catalogue"))
        self.assertEqual(response.status_code, 403)

    def test_a_card_cannot_be_placed_by_somebody_who_may_not_read_it(self):
        """The palette is a convenience; the serializer is the boundary.

        Without this an author could post a key they were never offered. The
        service would withhold it at read time, so nothing would leak -- but
        the attempt should fail where it can be explained rather than produce
        a board of permanently dark tiles.
        """
        author = _grant(self.member("author"), BUILD_PERMISSION)
        response = self.client_for(author).post(
            reverse("board_builder:boards"),
            {
                "name": "Sneaky",
                "columns": 4,
                "rows": 2,
                "placements": [
                    {"card_key": "gate_truck_turnaround", "column": 0, "row": 0}
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("data right", str(response.data))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class LayoutValidationTests(_Base):
    def setUp(self):
        self.author = _grant(
            _grant(self.member("author"), BUILD_PERMISSION), feed_right("gate")
        )
        self.client_ = self.client_for(self.author)

    def _post(self, **overrides):
        body = {"name": "Gate wall", "columns": 4, "rows": 2, "placements": []}
        body.update(overrides)
        return self.client_.post(reverse("board_builder:boards"), body, format="json")

    def test_a_card_may_not_hang_off_the_edge(self):
        """A 2x1 card in the last column of a 4-wide board.

        Refused rather than clipped: a tile half off a wall screen is a tile
        whose figure is half missing, and nothing on the screen says so.
        """
        response = self._post(
            placements=[{"card_key": "gate_truck_turnaround", "column": 3, "row": 0}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not fit", str(response.data))

    def test_two_cards_may_not_share_a_cell(self):
        response = self._post(
            placements=[
                {"card_key": "gate_truck_turnaround", "column": 0, "row": 0},
                {"card_key": "gate_trucks_on_site", "column": 1, "row": 0},
            ]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("overlaps", str(response.data))

    def test_a_valid_layout_saves(self):
        response = self._post(
            placements=[
                {"card_key": "gate_truck_turnaround", "column": 0, "row": 0},
                {"card_key": "gate_trucks_on_site", "column": 2, "row": 0},
                {
                    "card_key": "section_label",
                    "column": 3,
                    "row": 0,
                    "title": "Gate",
                    "accent": "transport",
                },
            ]
        )
        self.assertEqual(response.status_code, 201, response.data)
        board = CustomBoard.objects.get(slug=response.data["slug"])
        self.assertEqual(board.placements.count(), 3)
        self.assertEqual(board.visibility, BoardVisibility.PRIVATE)

    def test_a_wall_board_is_capped_shorter_than_a_page_board(self):
        """A wall does not scroll, so its rows have to fit the screen."""
        too_tall = self._post(mode=BoardMode.WALL, rows=WALL_MAX_ROWS + 1)
        self.assertEqual(too_tall.status_code, 400)
        self.assertIn("does not scroll", str(too_tall.data))

        same_height_as_page = self._post(
            name="Desk board", mode=BoardMode.PAGE, rows=WALL_MAX_ROWS + 1
        )
        self.assertEqual(same_height_as_page.status_code, 201)

    def test_a_page_board_still_has_a_ceiling(self):
        response = self._post(mode=BoardMode.PAGE, rows=PAGE_MAX_ROWS + 1)
        self.assertEqual(response.status_code, 400)

    def test_the_layout_is_replaced_whole(self):
        """A save is the finished arrangement, not a patch.

        Pinned because the alternative -- per-placement updates -- would walk
        a board through states that violate the overlap rule mid-drag.
        """
        created = self._post(
            placements=[{"card_key": "gate_trucks_on_site", "column": 0, "row": 0}]
        )
        slug = created.data["slug"]
        self.client_.patch(
            reverse("board_builder:board", args=[slug]),
            {
                "placements": [
                    {"card_key": "gate_trucks_on_site", "column": 2, "row": 1}
                ]
            },
            format="json",
        )
        placements = list(BoardPlacement.objects.filter(board__slug=slug))
        self.assertEqual(len(placements), 1)
        self.assertEqual((placements[0].column, placements[0].row), (2, 1))

    def test_two_boards_may_share_a_name(self):
        """Two desks naming a board 'Dispatch' is not a conflict to report."""
        first = self._post(name="Dispatch")
        second = self._post(name="Dispatch")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["slug"], second.data["slug"])


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------


class SharingTests(_Base):
    def setUp(self):
        self.author = _grant(
            _grant(self.member("author"), BUILD_PERMISSION), feed_right("gate")
        )
        self.colleague = _grant(self.member("colleague"), feed_right("gate"))
        self.board_ = self.board(self.author)
        BoardPlacement.objects.create(
            board=self.board_, card_key="gate_trucks_on_site", column=0, row=0
        )

    def test_a_private_board_does_not_exist_for_anybody_else(self):
        """404 and not 403.

        A draft's existence is not somebody else's business, and a 403 on a
        guessable slug confirms it.
        """
        response = self.client_for(self.colleague).get(
            reverse("board_builder:board", args=[self.board_.slug])
        )
        self.assertEqual(response.status_code, 404)

    def test_publishing_opens_the_board_for_the_company(self):
        self.board_.publish(self.author)
        response = self.client_for(self.colleague).get(
            reverse("board_builder:board", args=[self.board_.slug])
        )
        self.assertEqual(response.status_code, 200)

    def test_publishing_grants_no_data(self):
        """The property the whole sharing model rests on.

        An author who can read the gate publishes a gate card. A colleague who
        cannot read the gate opens the board and is told so, per card -- the
        figure never reaches them.
        """
        self.board_.publish(self.author)
        stranger = self.member("stranger")  # holds no feed at all

        self.assertFalse(may_open(stranger, self.board_))

        response = self.client_for(stranger).get(
            reverse("board_builder:board-data", args=[self.board_.slug])
        )
        self.assertEqual(response.status_code, 403)

    def test_a_card_a_reader_may_not_see_is_withheld_and_not_degraded(self):
        """The distinction that decides who somebody goes and talks to.

        One sends an operator to the server room, the other to an
        administrator. They must never share a list.
        """
        self.board_.publish(self.author)
        BoardPlacement.objects.create(
            board=self.board_, card_key="section_label", column=1, row=0
        )
        # Holds the layout card's (absent) feed only -- opens the board, sees
        # the label, is refused the gate figure.
        reader = self.member("reader")

        payload = CustomBoardService(
            self.board_, company_code=self.company.code, user=reader
        ).build()

        self.assertIn("gate_trucks_on_site", payload["meta"]["withheld"])
        self.assertEqual(payload["meta"]["degraded"], [])
        withheld_card = next(
            card for card in payload["cards"] if card["card_key"] == "gate_trucks_on_site"
        )
        self.assertIsNone(withheld_card["payload"])

    def test_an_audience_narrows_a_published_board(self):
        group = Group.objects.create(name="Dispatch office")
        self.board_.publish(self.author)
        self.board_.audience.set([group])

        self.assertFalse(may_open(self.colleague, self.board_))
        self.colleague.groups.add(group)
        self.assertTrue(may_open(User.objects.get(pk=self.colleague.pk), self.board_))

    def test_unpublishing_takes_it_off_the_wall_too(self):
        """Withdrawing a board must not leave it rotating in a corridor."""
        self.board_.publish(self.author)
        self.board_.in_carousel = True
        self.board_.save(update_fields=["in_carousel"])

        self.board_.unpublish()
        self.board_.refresh_from_db()
        self.assertFalse(self.board_.in_carousel)

    def test_somebody_elses_board_cannot_be_edited(self):
        self.board_.publish(self.author)
        colleague = _grant(self.colleague, BUILD_PERMISSION)
        response = self.client_for(colleague).patch(
            reverse("board_builder:board", args=[self.board_.slug]),
            {"name": "Mine now"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Duplicate it", str(response.data))

    def test_a_duplicate_is_always_a_private_copy(self):
        """Duplicating somebody's published wall board must not publish yours."""
        self.board_.publish(self.author)
        self.board_.in_carousel = True
        self.board_.save(update_fields=["in_carousel"])
        colleague = _grant(self.colleague, BUILD_PERMISSION)

        response = self.client_for(colleague).post(
            reverse("board_builder:board-duplicate", args=[self.board_.slug])
        )
        self.assertEqual(response.status_code, 201, response.data)
        copy = CustomBoard.objects.get(slug=response.data["slug"])
        self.assertEqual(copy.owner_id, colleague.id)
        self.assertEqual(copy.visibility, BoardVisibility.PRIVATE)
        self.assertFalse(copy.in_carousel)
        self.assertEqual(copy.placements.count(), self.board_.placements.count())

    def test_an_empty_board_cannot_be_published(self):
        empty = self.board(self.author, name="Empty", slug="empty")
        response = self.client_for(self.author).post(
            reverse("board_builder:board-publish", args=[empty.slug])
        )
        self.assertEqual(response.status_code, 400)

    def test_another_companys_board_is_not_reachable(self):
        theirs = CustomBoard.objects.create(
            company=self.other_company,
            owner=self.author,
            name="Mart wall",
            slug="mart-wall",
            visibility=BoardVisibility.PUBLISHED,
        )
        response = self.client_for(self.author).get(
            reverse("board_builder:board", args=[theirs.slug])
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Reading a board
# ---------------------------------------------------------------------------


class _Failing:
    """A card that raises, registered only for the duration of one test."""

    key = "test_exploding_card"


class ServiceTests(_Base):
    def setUp(self):
        self.author = _grant(
            _grant(self.member("author"), BUILD_PERMISSION), feed_right("gate")
        )
        self.board_ = self.board(self.author)

    def test_a_card_that_raises_costs_its_own_tile_and_not_the_board(self):
        """A wall with eleven good tiles and one that says why it is dark is
        worth far more than a 503 nobody is standing there to read."""

        def explode(context):
            raise RuntimeError("the query is wrong")

        spec = CardSpec(
            key=_Failing.key,
            title="Exploding card",
            summary="Raises, on purpose.",
            category="Layout",
            feed=None,
            build=explode,
        )
        catalogue.register(spec)
        self.addCleanup(catalogue._CARDS.pop, _Failing.key, None)

        BoardPlacement.objects.create(
            board=self.board_, card_key=_Failing.key, column=0, row=0
        )
        BoardPlacement.objects.create(
            board=self.board_, card_key="section_label", column=1, row=0
        )

        payload = CustomBoardService(
            self.board_, company_code=self.company.code, user=self.author
        ).build()

        self.assertIn(_Failing.key, payload["meta"]["degraded"])
        self.assertEqual(payload["meta"]["withheld"], [])
        self.assertEqual(len(payload["cards"]), 2)

    def test_a_retired_card_is_named_rather_than_drawn(self):
        """A board saved before a card was removed from the code."""
        BoardPlacement.objects.create(
            board=self.board_, card_key="card_that_was_deleted", column=0, row=0
        )
        payload = CustomBoardService(
            self.board_, company_code=self.company.code, user=self.author
        ).build()

        self.assertEqual(payload["meta"]["retired"], ["card_that_was_deleted"])
        self.assertEqual(payload["cards"], [])
        self.assertTrue(payload["meta"]["warnings"])

    def test_a_card_that_no_longer_fits_is_dropped_rather_than_overlapped(self):
        """A card's footprint is catalogue data and can grow under a saved
        board. Two cards on one square is the failure with no error."""
        small = self.board(self.author, name="Tiny", slug="tiny", columns=1, rows=1)
        BoardPlacement.objects.create(
            board=small, card_key="gate_truck_turnaround", column=0, row=0
        )  # 2x1 on a 1x1 board

        payload = CustomBoardService(
            small, company_code=self.company.code, user=self.author
        ).build()

        self.assertEqual(payload["meta"]["misplaced"], ["gate_truck_turnaround"])
        self.assertEqual(payload["cards"], [])

    def test_a_service_without_a_user_withholds_nothing(self):
        """The injectable-collaborator behaviour every board service has."""
        BoardPlacement.objects.create(
            board=self.board_, card_key="gate_trucks_on_site", column=0, row=0
        )
        payload = CustomBoardService(
            self.board_, company_code=self.company.code, user=None
        ).build()
        self.assertEqual(payload["meta"]["withheld"], [])

    def test_the_payload_carries_the_boards_finishing_touches(self):
        board = self.board(
            self.author,
            name="Night wall",
            slug="night-wall",
            surface="light",
            density="roomy",
            accent="production",
            show_heading=False,
        )
        payload = CustomBoardService(
            board, company_code=self.company.code, user=self.author
        ).build()

        self.assertEqual(payload["board"]["surface"], "light")
        self.assertEqual(payload["board"]["density"], "roomy")
        self.assertEqual(payload["board"]["accent"], "production")
        self.assertFalse(payload["board"]["show_heading"])

    def test_a_card_without_its_own_accent_takes_the_boards(self):
        board = self.board(self.author, name="Hued", slug="hued", accent="store")
        BoardPlacement.objects.create(
            board=board, card_key="section_label", column=0, row=0
        )
        BoardPlacement.objects.create(
            board=board, card_key="section_label", column=1, row=0, accent="dispatch"
        )
        payload = CustomBoardService(
            board, company_code=self.company.code, user=self.author
        ).build()

        accents = {card["column"]: card["accent"] for card in payload["cards"]}
        self.assertEqual(accents[0], "store")
        self.assertEqual(accents[1], "dispatch")


class CarouselTests(_Base):
    def test_only_wall_boards_on_the_rotation_are_listed(self):
        """A rotation cannot scroll, so a page board has no business in one."""
        author = _grant(self.member("author"), BUILD_PERMISSION)
        wall = self.board(
            author,
            name="Wall",
            slug="wall",
            mode=BoardMode.WALL,
            in_carousel=True,
            visibility=BoardVisibility.PUBLISHED,
        )
        self.board(
            author,
            name="Page",
            slug="page",
            mode=BoardMode.PAGE,
            in_carousel=True,
            visibility=BoardVisibility.PUBLISHED,
        )
        self.board(author, name="Off", slug="off", mode=BoardMode.WALL)

        response = self.client_for(author).get(reverse("board_builder:carousel"))
        self.assertEqual(
            [board["slug"] for board in response.data["boards"]], [wall.slug]
        )

    def test_a_board_the_screen_cannot_read_is_not_offered_to_it(self):
        """Unlike the general list, this endpoint checks feeds per board.

        The caller is an unattended screen. A slide it may not read would
        rotate into view and sit there for the whole dwell as a grid of
        refusals, with nobody in front of it to move on. A shorter rotation is
        the better answer, and the set is small enough to afford the check.
        """
        author = _grant(
            _grant(self.member("author"), BUILD_PERMISSION), feed_right("gate")
        )
        board = self.board(
            author,
            name="Gate wall",
            slug="gate-wall-rotating",
            mode=BoardMode.WALL,
            in_carousel=True,
            visibility=BoardVisibility.PUBLISHED,
        )
        BoardPlacement.objects.create(
            board=board, card_key="gate_trucks_on_site", column=0, row=0
        )

        # Its author, who holds the gate feed, gets it.
        mine = self.client_for(author).get(reverse("board_builder:carousel"))
        self.assertEqual([b["slug"] for b in mine.data["boards"]], [board.slug])

        # A display login holding no feed does not, even though the board is
        # published to the whole company.
        screen = self.member("wall-screen")
        theirs = self.client_for(screen).get(reverse("board_builder:carousel"))
        self.assertEqual(theirs.data["boards"], [])

    def test_the_carousel_right_alone_does_not_open_a_built_board(self):
        """The rule from admin_board/carousel.py, arrived at by another road.

        ``can_view_board_carousel`` is honoured on the Admin and Plant boards'
        composed reads because those two are FIXED: a developer chose every
        tile, so the right buys a known, audited set of figures. A built board
        is whatever its author dragged onto it this morning, so honouring one
        right across all of them would make that right a key into every feed
        in the product.
        """
        author = _grant(
            _grant(self.member("author"), BUILD_PERMISSION), feed_right("gate")
        )
        board = self.board(
            author,
            name="Gate wall",
            slug="gate-wall-carousel",
            mode=BoardMode.WALL,
            in_carousel=True,
            visibility=BoardVisibility.PUBLISHED,
        )
        BoardPlacement.objects.create(
            board=board, card_key="gate_trucks_on_site", column=0, row=0
        )

        display = _grant(
            self.member("display"), "admin_board.can_view_board_carousel"
        )
        self.assertFalse(may_open(display, board))

        response = self.client_for(display).get(
            reverse("board_builder:board-data", args=[board.slug])
        )
        self.assertEqual(response.status_code, 403)
