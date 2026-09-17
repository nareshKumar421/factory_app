"""
admin_board/carousel.py

The board carousel's own right, and the one rule every board shares about it.

WHAT THIS IS FOR
``/dashboards/carousel`` rotates the Admin, Plant and Logistics control boards
on an unattended wall screen. A screen is not a person: it should hold one
permission, open one page and reach nothing else. Before this right existed a
display login had to be given the UNION of the three boards' gates — ten rights,
each of which also opens the operational report behind it.

``admin_board.can_view_board_carousel`` (minted by migration 0001) is that one
permission. A board's permission class accepts it IN ADDITION to its own rights,
never instead of them, so nothing any existing user can read changes.

THE RULE, AND WHY IT MATTERS MORE THAN IT LOOKS
This right must only ever be honoured by an endpoint the carousel actually
reads, and only for reading. It is a key cut for one wall screen; the moment it
is added to a permission class that guards a whole module, "one permission"
becomes ten wearing one name — worse than the group it replaced, because the
breadth is then hidden inside permission classes instead of being visible as
group membership that an administrator can audit.

Admin and Plant are safe places to honour it because each composes its whole
board server-side and exposes ONE read endpoint (plus Plant's two settings
views). Widening those widens nothing else.
"""

from rest_framework.permissions import BasePermission

BOARD_CAROUSEL_PERMISSION = "admin_board.can_view_board_carousel"


def holds_carousel_right(user) -> bool:
    """Whether this request comes from a login that exists to show the wall."""
    return bool(user and user.is_authenticated and user.has_perm(BOARD_CAROUSEL_PERMISSION))


class CanViewBoardCarousel(BasePermission):
    """The display login's single right.

    Written to be composed with a board's own class rather than used alone --
    ``CanViewAdminBoard | CanViewBoardCarousel`` reads as "the people who could
    always see this, plus the wall screen", and DRF's ``|`` keeps each class
    responsible for its own message.
    """

    message = (
        "You need the board carousel permission, or one of this board's own "
        "rights, to read it."
    )

    def has_permission(self, request, view):
        return holds_carousel_right(request.user)
