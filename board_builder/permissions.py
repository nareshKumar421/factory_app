"""
board_builder/permissions.py

Who may build a board, and who may open one somebody else built.

TWO SEPARATE QUESTIONS, AND KEEPING THEM SEPARATE IS THE POINT
---------------------------------------------------------------
**Building** is one right, ``board_builder.can_build_dashboards``, and it buys
the editor. It is a right over the PRODUCT, not over data: holding it lets
somebody arrange cards, and the palette they are offered is still only the
cards whose feeds they personally hold. An author cannot place a card they
cannot read, which means they cannot build a board that discloses something to
themselves, and they cannot build one that discloses something to a colleague
either -- see below.

**Opening** is decided per board and then again per card. A published board
opens for its audience; each card on it is then withheld individually by
``control_boards.sections`` unless the reader holds that card's feed. So
publishing a board is not a grant. The worst an author can do by publishing is
show somebody a grid of tiles that say "you may not see this", which is
annoying and not a disclosure.

THE RULE THIS FILE INHERITS
----------------------------
``control_boards/feeds.py``: a feed right is only ever honoured inside a
composed board service. Nothing here may be put on an operational view, and
``can_build_dashboards`` in particular grants no data at all -- it is checked
alongside the feed rights, never instead of them.

WHY PRIVATE IS PRIVATE, EVEN FOR STAFF
---------------------------------------
There is no staff override on a private board. A draft is somebody arranging
tiles, and a half-built wall is not a fact about the factory; an administrator
who needs to see one can be published to, or can look in the Django admin at
the rows. Silently readable drafts would make the private state a lie, and the
whole sharing model rests on people trusting it.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from control_boards.feeds import may_read

from . import catalogue
from .models import BoardVisibility, CustomBoard

#: The right that opens the editor. Minted by migration 0002; granted by an
#: administrator, never by a migration.
BUILD_PERMISSION = "board_builder.can_build_dashboards"


def may_build(user) -> bool:
    """Whether this login may create and edit boards."""
    return bool(user and user.is_authenticated and user.has_perm(BUILD_PERMISSION))


def readable_cards(user) -> tuple:
    """The catalogue entries this user may actually place.

    The palette is filtered rather than greyed out. A card somebody may not
    read is not a feature they are missing, it is data they have not been
    granted, and listing it by name tells them what exists behind a wall they
    cannot open -- a small disclosure, but a free one to avoid.
    """
    return tuple(
        spec
        for spec in catalogue.all_cards()
        if spec.feed is None or may_read(user, spec.feed)
    )


def _in_audience(user, board: CustomBoard) -> bool:
    """Whether a published board's audience covers this reader.

    An empty audience means the whole company, which is the common case: most
    boards are published because somebody wants colleagues to see them, not
    because they want a specific group to and everybody else not to.
    """
    if not board.audience.exists():
        return True
    return user.groups.filter(pk__in=board.audience.values("pk")).exists()


def _holds_any_feed(user, board: CustomBoard) -> bool:
    """Whether any card on this board would render for this reader.

    A board whose every card is withheld is a grid of refusals, and opening it
    teaches the reader nothing except which tiles exist. Refusing at the door
    with a message that names the problem is the kinder failure -- and it
    matches ``control_boards.permissions.CanReadBoard``, which opens a board
    on any ONE of its feeds.

    A board made entirely of cards that gate nothing (a page of section
    labels) has no feeds to hold, and opens.
    """
    feeds = set()
    for key in board.placements.values_list("card_key", flat=True):
        spec = catalogue.get(key)
        if spec is not None and spec.feed:
            feeds.add(spec.feed)
    if not feeds:
        return True
    return any(may_read(user, feed) for feed in feeds)


def may_open(user, board: CustomBoard) -> bool:
    """Whether this reader may open this board at all.

    Per-CARD suppression is not this function's job -- the service withholds
    each card on its own. This answers only "is there a board here for you".
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if board.owner_id == user.id:
        return True
    if board.visibility != BoardVisibility.PUBLISHED:
        return False
    return _in_audience(user, board) and _holds_any_feed(user, board)


class CanBuildDashboards(BasePermission):
    """The editor's gate. Grants no data -- see the module docstring."""

    message = (
        "You need the dashboard builder permission to create or change a "
        "board. Ask an administrator for the Dashboard Builders group."
    )

    def has_permission(self, request, view):
        return may_build(request.user)


# NOTE: there is deliberately no ``CanOpenCustomBoard`` permission class.
#
# Reading a board is an OBJECT-level decision, and ``APIView`` -- which every
# view in this app is -- never calls ``check_object_permissions`` on its own.
# A class listed in ``permission_classes`` would therefore read as protection
# while doing nothing at all, which is a worse failure than no class: the next
# person to touch those views would believe the gate was already there.
#
# :func:`may_open` is called explicitly instead, on every path that serves a
# board, and the views turn a refusal into a 404 rather than the 403 a
# permission class would raise -- so a private board's existence is not
# confirmed by a status code on a guessable slug.
