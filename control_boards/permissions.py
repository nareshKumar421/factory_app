"""
control_boards/permissions.py

The route-level gate for a composed board.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
``CanReadBoard("stock", "non_moving", ...)`` builds a DRF permission class that
opens a board to anybody who may read AT LEAST ONE of its feeds. It answers
"may this person open the board at all", and nothing finer.

Per-SECTION suppression is the board service's job, not this class's -- see
``control_boards.sections.SectionBuilder``. Keeping the two apart matters: a
board is opened by holding one feed, and then shows only the sections whose
feeds the reader actually holds. Doing both here would either lock out a viewer
who holds one feed, or hand them every section.

That "any one right opens it" shape is the existing convention, taken from
``plant_board/permissions.py``, which spells out the consequence: a login
holding one right opens the board and finds the other bands' tiles empty with a
reason on their face, rather than a 403 on the whole screen. There is nobody at
a wall screen to be shown an error page.

THE RULE THIS FILE MUST NOT BREAK
---------------------------------
These classes belong on a COMPOSED board view -- one endpoint that assembles the
whole board server-side -- and nowhere else. Putting one on an operational view
would make a feed right into that module's view right under another name. See
the rule in ``control_boards/feeds.py``, and ``admin_board/carousel.py`` before
it.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from .feeds import feed, may_read


def CanReadBoard(*feed_names: str, board: str = "this board"):  # noqa: N802
    """Build the permission class for one board.

    Named like a class because it is used like one::

        permission_classes = [
            IsAuthenticated,
            HasCompanyContext,
            CanReadBoard("stock", "non_moving", board="Warehouse Control"),
        ]

    Every name is resolved at import time rather than per request, so a typo in
    a board's feed list fails when the module loads instead of reading as
    "nobody may see this tile" in production.
    """
    if not feed_names:
        raise ValueError("A board must name at least one feed.")
    resolved = tuple(feed(name) for name in feed_names)
    names = tuple(feed_names)

    class _CanReadBoard(BasePermission):
        __doc__ = f"Any one of {board}'s feed rights, or the rights they mirror."

        #: Introspectable so tests and the groups command can assert a board's
        #: gate matches the feeds its service actually reads.
        board_name = board
        board_feeds = names

        message = (
            f"You need one of {board}'s feed permissions to read it. "
            "Ask an administrator for the matching Dashboards group."
        )

        def has_permission(self, request, view):
            return any(may_read(request.user, name) for name in names)

    _CanReadBoard.__name__ = "CanRead" + "".join(
        part.capitalize() for part in board.replace("-", " ").split()
    )
    _CanReadBoard.__qualname__ = _CanReadBoard.__name__
    # Kept for the error message and for tests that print a failure.
    _CanReadBoard.resolved_feeds = resolved
    return _CanReadBoard
