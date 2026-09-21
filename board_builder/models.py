"""
board_builder/models.py

Boards people compose themselves, out of cards somebody else wrote.

WHAT THIS APP IS
----------------
The Admin, Plant and Logistics boards are each a hand-built page: a developer
chose the bands, the tiles, the order and the hues, and changing any of it is a
deploy. That is the right shape for three boards the whole factory reads, and
the wrong shape for the twentieth, which one desk wants, for six weeks, with
four tiles on it.

This app is the other half: a CATALOGUE of cards (``board_builder/cards``) and
a way for somebody to arrange the ones they are allowed to see onto a grid,
colour it, and open it at an address of its own -- or put it on the wall
rotation. No deploy, and, crucially, no new endpoint per board: every board
built here is read through the one composed endpoint in ``views.py``, which is
what keeps the feed-rights rule in ``control_boards/feeds.py`` intact.

THE GRID IS BOUNDED AND THE CARDS ARE NOT RESIZABLE
---------------------------------------------------
An author picks how many columns and rows their board has, inside the ceilings
in ``constants.py``. Every card declares its own fixed footprint in the
catalogue -- a figure tile is 1x1, a seven-day chart 2x1, an ageing matrix 2x2 --
and the author places it, but never stretches it. Two reasons, and the second
is the load-bearing one:

1. A tile that can be any size is a tile whose visualisation has to work at any
   size, and the ones in ``OpsViz`` do not. A seven-bar chart squeezed into a
   quarter-width column is seven grey slivers.

2. The footprint is a property of the CARD, so it lives with the card's code.
   An author dragging a matrix onto a board gets the two-by-two it needs
   without knowing that it needs one, and a card that later grows is caught by
   the overlap check on read instead of quietly covering its neighbour.

Which is why a placement below stores an ORIGIN and nothing else. The size is
looked up, never stored: a denormalised copy would let the catalogue and the
saved board disagree, and the disagreement would render as two cards on one
square with no error anywhere.

PRIVATE UNTIL PUBLISHED, AND PUBLISHING GRANTS NOTHING
-------------------------------------------------------
A board starts private to whoever made it. Publishing puts it in front of other
people, and it is worth being exact about what that does and does not do: it
makes the board OPENABLE, and it does not make any card on it READABLE.
Every card still declares a feed, every read still goes through
``control_boards.sections``, and a reader who may not hold that feed gets the
card withheld with a reason on its face. So an author cannot widen anybody's
access by dragging a tile onto a shared board -- the worst they can do is show
somebody a board of tiles they are told they may not see.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models.base import BaseModel

from .constants import (
    BOARD_DENSITIES,
    BOARD_SURFACES,
    CARD_ACCENTS,
    DEFAULT_ACCENT,
    DEFAULT_COLUMNS,
    DEFAULT_DENSITY,
    DEFAULT_ROWS,
    DEFAULT_SURFACE,
)


class BoardMode(models.TextChoices):
    """What screen this board was built for.

    The distinction is geometric and not cosmetic. A WALL board must fit its
    viewport exactly -- nobody is standing at it to scroll -- so its rows are
    sized to divide the screen and its row count is capped low. A PAGE board
    sits in the app shell where a person has a mouse, so it scrolls and may be
    taller. Only a WALL board may join the carousel rotation, for the obvious
    reason that a rotation cannot scroll either.
    """

    WALL = "WALL", "Wall screen"
    PAGE = "PAGE", "In-app page"


class BoardVisibility(models.TextChoices):
    """Who is allowed to look for this board.

    ``PRIVATE`` is the author and nobody else -- it does not appear in anyone
    else's list and its address 404s for them, because a board somebody is
    still arranging is a draft and a half-built wall is not a fact about the
    factory.

    ``PUBLISHED`` means it appears for the groups in :attr:`CustomBoard.audience`
    (or for the whole company when that is empty). It still says nothing about
    what the cards on it will show -- see the module docstring.
    """

    PRIVATE = "PRIVATE", "Private to its author"
    PUBLISHED = "PUBLISHED", "Published"


def _choices(values: tuple[str, ...]) -> list[tuple[str, str]]:
    """``("dark", "light")`` as Django choices, labelled for the admin."""
    return [(value, value.replace("_", " ").capitalize()) for value in values]


class CustomBoard(BaseModel):
    """One composed board: a grid, a palette, and the cards placed on it."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="custom_boards",
        help_text="The company whose figures this board reads.",
    )

    #: Explicit, and not ``BaseModel.created_by``.
    #:
    #: ``created_by`` is ``SET_NULL``: a deactivated account leaves it null, and
    #: a private board whose owner is null is a board nobody can open and
    #: nobody can delete. Ownership decides access here, so it gets a column
    #: that cannot become a hole -- ``CASCADE`` because a private board without
    #: its author is not worth keeping, and a published one is re-owned by the
    #: transfer path rather than orphaned.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="custom_boards",
    )

    name = models.CharField(max_length=120)
    #: Stable in the URL, so a board's address survives a rename.
    slug = models.SlugField(max_length=140)
    description = models.TextField(
        blank=True,
        help_text="What this board is for. Shown in the list and under the heading.",
    )

    mode = models.CharField(
        max_length=10,
        choices=BoardMode.choices,
        default=BoardMode.WALL,
    )

    # -- geometry -----------------------------------------------------------
    # Bounded in the serializer against constants.py rather than by a
    # validator, because the row ceiling depends on `mode` and a field
    # validator cannot see its own row.
    columns = models.PositiveSmallIntegerField(default=DEFAULT_COLUMNS)
    rows = models.PositiveSmallIntegerField(default=DEFAULT_ROWS)

    # -- finishing touches --------------------------------------------------
    surface = models.CharField(
        max_length=10,
        choices=_choices(BOARD_SURFACES),
        default=DEFAULT_SURFACE,
        help_text="Dark or light. A property of the room the screen is in.",
    )
    density = models.CharField(
        max_length=10,
        choices=_choices(BOARD_DENSITIES),
        default=DEFAULT_DENSITY,
    )
    accent = models.CharField(
        max_length=20,
        choices=_choices(CARD_ACCENTS),
        default=DEFAULT_ACCENT,
        help_text="The hue a newly dropped card takes unless it is given its own.",
    )
    show_heading = models.BooleanField(
        default=True,
        help_text="Print the board's name across the top. Off for a wall that "
        "is only ever seen by people who know what they are looking at.",
    )

    # -- sharing ------------------------------------------------------------
    visibility = models.CharField(
        max_length=12,
        choices=BoardVisibility.choices,
        default=BoardVisibility.PRIVATE,
    )
    audience = models.ManyToManyField(
        "auth.Group",
        blank=True,
        related_name="custom_boards",
        help_text="Groups this board is published to. Empty means the whole "
        "company -- still subject to each card's own feed right.",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="custom_boards_published",
    )

    #: Whether a WALL board joins the unattended rotation.
    #:
    #: Off by default and never implied by publishing: putting a board in front
    #: of colleagues and putting it on the factory wall are different decisions,
    #: and the second one is made by somebody looking at the wall.
    in_carousel = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                name="board_builder_unique_slug_per_company",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "visibility"]),
            models.Index(fields=["owner"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_published(self) -> bool:
        return self.visibility == BoardVisibility.PUBLISHED

    def publish(self, user) -> None:
        """Make the board openable by its audience. Idempotent.

        ``published_at`` is only stamped on the transition, so re-publishing an
        already-published board after an edit does not reset the date somebody
        is using to remember when it appeared.
        """
        if not self.is_published:
            self.published_at = timezone.now()
            self.published_by = user
        self.visibility = BoardVisibility.PUBLISHED
        self.save(
            update_fields=["visibility", "published_at", "published_by", "updated_at"]
        )

    def unpublish(self) -> None:
        """Take it back. Keeps ``published_at`` as the historical fact it is.

        Also drops it off the wall: a board its author has withdrawn must not
        keep rotating on a screen in the corridor because nobody remembered
        that the rotation is a separate flag.
        """
        self.visibility = BoardVisibility.PRIVATE
        self.in_carousel = False
        self.save(update_fields=["visibility", "in_carousel", "updated_at"])


class BoardPlacement(models.Model):
    """One card, at one spot on one board.

    Deliberately NOT a :class:`BaseModel`. A placement has no independent life:
    it is created and destroyed by dragging, it belongs to exactly one board,
    and stamping four audit columns on a row that changes every time somebody
    nudges a tile would make the audit trail noise. The board carries the
    authorship; this carries the geometry.
    """

    board = models.ForeignKey(
        CustomBoard,
        on_delete=models.CASCADE,
        related_name="placements",
    )

    #: The catalogue key. Validated against ``board_builder.cards`` on save --
    #: an unknown key here would render as a permanently withheld tile with no
    #: way for the author to find out why.
    card_key = models.CharField(max_length=80)

    #: Zero-based origin of the card's top-left cell. The card's WIDTH and
    #: HEIGHT are not here on purpose; see the module docstring.
    column = models.PositiveSmallIntegerField()
    row = models.PositiveSmallIntegerField()

    title = models.CharField(
        max_length=80,
        blank=True,
        help_text="Replaces the catalogue's name for this card on this board. "
        "For a board whose reader calls the same figure something else.",
    )
    accent = models.CharField(
        max_length=20,
        choices=_choices(CARD_ACCENTS),
        blank=True,
        help_text="Overrides the board's default hue for this card alone.",
    )
    options = models.JSONField(
        default=dict,
        blank=True,
        help_text="This card's own settings -- the window in days, which "
        "warehouse, and so on. Validated against the card's declared options.",
    )

    class Meta:
        ordering = ["row", "column"]
        constraints = [
            models.UniqueConstraint(
                fields=["board", "row", "column"],
                name="board_builder_one_card_per_cell",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.card_key} @ {self.board.slug} ({self.column},{self.row})"
