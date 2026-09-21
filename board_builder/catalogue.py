"""
board_builder/catalogue.py

The register of cards, and the contract a card signs.

ADDING A CARD IS ONE FILE AND NO FRONTEND WORK
-----------------------------------------------
Write a module in ``board_builder/cards/``, compute your figure, return one of
the shapes in ``board_builder/viz.py``, and call :func:`register`. It is in the
palette on the next restart, draggable onto any board, for every reader who
holds its feed. Nothing else in this app and nothing at all in the frontend
needs to know it exists.

That is the whole design goal, so the two rules that protect it are worth
stating plainly.

**A card names a feed, or it names why it does not.** ``feed`` is the key into
``control_boards.feeds`` that decides who may read this card's data. It is not
optional in the ordinary case: a card reading dispatch figures must be gated on
the dispatch feed, or the card becomes a way to read dispatch without the
dispatch right -- which is precisely the hole ``control_boards/feeds.py`` was
built to close, re-opened by a drag and drop. ``feed=None`` is allowed and
means "this card discloses nothing", which today is true of exactly one card:
the section label, which renders text the author typed.

**A card's footprint is fixed and declared here.** Not chosen by the author,
not stored on the placement. ``columns``/``rows`` are the card's own knowledge
of how much room its visualisation needs to stay honest.

WHAT A CARD MUST NOT DO
-----------------------
Raise. A card that throws is caught by ``control_boards.sections`` and reported
as degraded, which is the right outcome for a SAP timeout and the wrong one for
a typo -- the tile goes dark and the board says the source is down. Cards
should return :func:`board_builder.viz.missing` for every condition they can
foresee, and keep the exception path for the ones they cannot.

Be slow. A board is a dozen cards behind one request. There is no per-card
timeout, so a card that sequentially scans a two-million-row table takes the
whole board down with it -- and the Postgres box behind it serves fourteen
databases. A card doing anything heavier than an indexed aggregate over a few
thousand rows should read a rollup, not the raw table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from .constants import CARD_ACCENTS, DEFAULT_ACCENT, GRID_MAX_COLUMNS, WALL_MAX_ROWS


@dataclass(frozen=True)
class CardOption:
    """One setting an author may change per placement.

    Kept deliberately thin -- an integer, a choice or a switch. Anything richer
    is a second card. An option exists so the same card can be dropped twice on
    one board reading two windows; it is not a query builder.
    """

    key: str
    label: str
    kind: Literal["int", "choice", "bool"]
    default: Any
    #: ``int``: inclusive bounds. Ignored for the other kinds.
    minimum: int = 0
    maximum: int = 0
    #: ``choice``: the allowed ``(value, label)`` pairs.
    choices: tuple[tuple[str, str], ...] = ()
    help: str = ""

    def coerce(self, raw: Any) -> Any:
        """This option's value from whatever the author's browser sent.

        Falls back to the default rather than raising. An option is chrome: a
        board that will not open because somebody's saved dropdown value was
        retired in a later release is a worse outcome than a board that quietly
        reverts to the default and says so in its subtitle.
        """
        if raw is None:
            return self.default
        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return self.default
            if self.minimum or self.maximum:
                value = max(self.minimum, min(self.maximum, value))
            return value
        if self.kind == "bool":
            return bool(raw)
        allowed = {value for value, _ in self.choices}
        return raw if raw in allowed else self.default


@dataclass(frozen=True)
class CardContext:
    """Everything a card is given, and the whole of it.

    A card receives no request and no view. That is a boundary rather than an
    oversight: a card that could reach the request could read a header, branch
    on a query parameter, or -- the one that matters -- check permissions
    itself. Permissions are decided once, by the feed, before the card is ever
    called. A card that could re-decide them would be a second place for the
    answer to live.
    """

    company_code: str
    #: The reader. Present for cards that must name a person ("your approvals"),
    #: and for nothing else. ``None`` in a service built without a request.
    user: Any
    #: The placement's options, already coerced against the card's declarations.
    options: dict[str, Any]

    def option(self, key: str, fallback: Any = None) -> Any:
        return self.options.get(key, fallback)


#: What a card's build function looks like.
CardBuilder = Callable[[CardContext], dict]


@dataclass(frozen=True)
class CardSpec:
    """One entry in the palette."""

    key: str
    title: str
    #: One line, shown under the name in the palette. What the card answers,
    #: in the vocabulary of the person who would drag it -- not the table it
    #: reads.
    summary: str
    category: str
    build: CardBuilder

    #: The feed right that decides who may read this card. See the module
    #: docstring before setting it to ``None``.
    feed: str | None = None

    #: The fixed footprint, in grid cells.
    columns: int = 1
    rows: int = 1

    accent: str = DEFAULT_ACCENT
    options: tuple[CardOption, ...] = ()

    #: Whether this card talks to SAP. Cards that do are skipped wholesale once
    #: one of them reports the connection is gone -- see the latch in
    #: ``control_boards.sections``. Defaults to False because almost everything
    #: in this catalogue is Postgres, and a card wrongly marked as needing SAP
    #: disappears during an outage it was immune to.
    needs_sap: bool = False

    #: Longer prose for the palette's detail panel: what the figure means,
    #: which definition it follows, anything a reader would otherwise have to
    #: ask. Optional, but a card whose number could be computed two ways and
    #: does not say which is a card that will be misread.
    note: str = ""

    def __post_init__(self) -> None:
        # Validated at import, so a mistake fails the process rather than one
        # tile. Every one of these has a way of reading as "withheld" or
        # "empty" on a wall if it is allowed through.
        if self.accent not in CARD_ACCENTS:
            raise ValueError(
                f"Card {self.key!r} has accent {self.accent!r}; "
                f"choose one of {', '.join(CARD_ACCENTS)}."
            )
        if not 1 <= self.columns <= GRID_MAX_COLUMNS:
            raise ValueError(
                f"Card {self.key!r} is {self.columns} columns wide; "
                f"the widest board is {GRID_MAX_COLUMNS}."
            )
        if not 1 <= self.rows <= WALL_MAX_ROWS:
            raise ValueError(
                f"Card {self.key!r} is {self.rows} rows tall; a wall board is "
                f"at most {WALL_MAX_ROWS}, and a card that cannot fit on a wall "
                "cannot be placed on one."
            )

    def coerce_options(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """The options this card will actually run with.

        Every declared option gets a value and nothing undeclared survives: a
        stale key left behind by an earlier version of the card is dropped
        rather than passed through to confuse it.
        """
        raw = raw or {}
        return {option.key: option.coerce(raw.get(option.key)) for option in self.options}


#: The register. Keys are part of the stored contract -- a placement holds one
#: -- so renaming a card key orphans every board that used it. Retire a card by
#: leaving it registered and hidden rather than by deleting it.
_CARDS: dict[str, CardSpec] = {}


def register(spec: CardSpec) -> CardSpec:
    """Add one card to the palette. Returns it, so it can decorate a module."""
    if spec.key in _CARDS:
        raise ValueError(
            f"Two cards are registered as {spec.key!r}. A card key is stored on "
            "every placement that uses it, so it must be unique and stable."
        )
    _CARDS[spec.key] = spec
    return spec


def get(key: str) -> CardSpec | None:
    """One card, or ``None`` if a board holds a key no longer in the code."""
    _load()
    return _CARDS.get(key)


def require(key: str) -> CardSpec:
    """One card, loudly. For paths where an unknown key is a bug, not history."""
    spec = get(key)
    if spec is None:
        raise KeyError(
            f"Unknown card {key!r}. Registered: {', '.join(sorted(_CARDS)) or 'none'}."
        )
    return spec


def all_cards() -> tuple[CardSpec, ...]:
    """Every registered card, in palette order: category, then title."""
    _load()
    return tuple(sorted(_CARDS.values(), key=lambda spec: (spec.category, spec.title)))


def categories() -> tuple[str, ...]:
    """The palette's groups, in the order they are shown."""
    return tuple(dict.fromkeys(spec.category for spec in all_cards()))


_loaded = False


def _load() -> None:
    """Import the card modules once, on first use.

    Lazy rather than at app-ready, because a card module imports models from
    half the operational apps and doing that from ``AppConfig.ready`` invites
    the import cycle Django spends its startup avoiding.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import cards  # noqa: F401  (importing it is the registration)

    cards.load()
