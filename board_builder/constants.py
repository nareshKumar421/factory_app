"""
board_builder/constants.py

The limits a composed board is built inside, and why each one is where it is.

EVERY NUMBER HERE IS A LEGIBILITY LIMIT, NOT A STORAGE ONE
-----------------------------------------------------------
Nothing in the database cares whether a board is four columns or forty. These
ceilings exist because the boards are read from across a room -- the existing
three are 1920x1080 wall screens with nobody standing at them -- and a grid
past a certain density stops being a board and becomes a spreadsheet nobody
can read at four metres. They are deliberately low, and a request to raise one
should come with the screen it is being raised for.
"""

from __future__ import annotations

#: The widest a board may be.
#:
#: ``OpsBand`` lays four equal tiles today and that is the shape the plant is
#: used to reading. Six is the point at which a tile on a 1920-wide wall falls
#: under about 300px, which is where the four-row tile (name, subtitle, figure,
#: visualisation) starts eliding its own figure. Past that the author is not
#: designing a board, they are shrinking one.
GRID_MAX_COLUMNS = 6

#: The floor. A one-column board is a legitimate thing -- a single big number
#: on a screen by a machine -- so this is 1 rather than 2.
GRID_MIN_COLUMNS = 1

#: How tall a WALL board may be.
#:
#: A wall board does not scroll: every row has to fit in the viewport at once,
#: and 1080 minus the topbar divided by five is about 190px a row, which is the
#: measured floor for a tile that still shows a visualisation under its figure.
#: A sixth row is what clipped the plant board's fourth band, and the fix then
#: was to stop growing rather than to scale down.
WALL_MAX_ROWS = 5

#: How tall a PAGE board may be.
#:
#: A page board scrolls inside the app shell, so height costs nothing but
#: patience. Twelve is a scroll of roughly three screens, past which a reader
#: has lost the top of the board before reaching the bottom and would be better
#: served by two boards.
PAGE_MAX_ROWS = 12

GRID_MIN_ROWS = 1

#: What a new board starts as: the shape of the boards that already exist.
DEFAULT_COLUMNS = 4
DEFAULT_ROWS = 3


def max_rows_for(mode: str) -> int:
    """The row ceiling for one board mode.

    Looked up rather than branched at each call site, because the wall/page
    split appears in the serializer, the service and the tests, and three
    copies of ``5 if wall else 12`` is three places to forget one.
    """
    from .models import BoardMode

    return PAGE_MAX_ROWS if mode == BoardMode.PAGE else WALL_MAX_ROWS


#: The hues a board or a card may wear.
#:
#: A CLOSED LIST, AND THAT IS THE POINT. The existing boards state the rule
#: plainly -- "colour carries meaning and nothing else": each band owns one hue,
#: every bar inside it is a tint of that same hue, and green/amber/red are
#: reserved for CONDITION and never used as a domain colour. A free colour
#: picker would let an author paint a healthy tile red on a wall where red means
#: "this is wrong", and nobody would be able to tell the two apart from across
#: the floor.
#:
#: So the author picks an identity, not a colour. These seven are the domain
#: hues ``OpsBand`` already paints, and the frontend resolves each to the same
#: CSS custom properties the three wall boards use, which is what keeps a card
#: built here and the same card on the Plant board from being two teals.
CARD_ACCENTS: tuple[str, ...] = (
    "warehouse",
    "dispatch",
    "transport",
    "purchase",
    "store",
    "production",
    "shifting",
)

DEFAULT_ACCENT = "warehouse"

#: The page's own surface. Wall screens live in rooms with very different
#: light -- the dispatch office is bright, the floor by the lines is not -- and
#: this is the only setting on a board that is a property of the ROOM rather
#: than of the data.
#:
#: LIGHT IS THE DEFAULT because the three existing wall boards are light, and a
#: board built here appearing beside them in the same rotation should not be
#: the odd one out unless somebody chose that. Dark is for the screens by the
#: lines, where the room is not.
BOARD_SURFACES: tuple[str, ...] = ("light", "dark")

DEFAULT_SURFACE = "light"

#: How much air a tile gets. ``compact`` is for a board an author has filled to
#: the edges; ``roomy`` for a four-tile board on a large screen.
BOARD_DENSITIES: tuple[str, ...] = ("compact", "normal", "roomy")

DEFAULT_DENSITY = "normal"
