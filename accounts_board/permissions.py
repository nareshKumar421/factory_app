"""
accounts_board/permissions.py

Who may open the accounts control board, and who may see a name on it.

TWO GATES, BECAUSE THE BOARD HAS TWO KINDS OF CONTENT
------------------------------------------------------
Every other tile on this board is a total over the whole register: what came
in, what went out, what is in the box. Two of them are not -- the cash-issue
list and the salary-advance list name individual people and say what each is
holding. That is a different disclosure, and a wall screen in a corridor is
exactly where it should not appear.

So:

``CanViewAccountsBoard``   opens the screen. It mints NO right of its own --
                           holding the cash book's own read right is already
                           permission to read every figure here, because this
                           board is that register summarised and nothing more.

``may_name_people``        decides whether the per-person rows carry names. It
                           asks for the SAME cash-book right, so the rule is
                           simply: if you could open the register and read the
                           names there, you may read them here; if you reached
                           this board through a board-feed right instead -- the
                           way the carousel's display login does -- you get the
                           totals and the counts with the names masked.

That is why the view composes ``CanViewAccountsBoard | CanReadBoard(...)``: the
feed right widens who may open the board, and ``may_name_people`` then narrows
what they see once inside. A feed right can never become a way to read the
staff list off a television.

WHY NO NEW PERMISSION ROW
--------------------------
Following ``hr_board.permissions`` and ``admin_board.permissions``: a dedicated
right would have to be created on the live database and added to every relevant
group before anybody at all could open the board, and it would buy nothing that
``cash_book.can_view_cash_book`` does not already express. This app therefore
ships no model and no migration of its own.
"""

from rest_framework.permissions import BasePermission

#: The register's own rights. Any one of them means this reader is already
#: trusted with the cash book, which is everything this board summarises.
ACCOUNTS_BOARD_VIEW_PERMISSIONS = (
    "cash_book.can_view_cash_book",
    "cash_book.can_manage_cash_book",
    "cash_book.can_approve_cash_entries",
)

#: The right that lets a row carry somebody's name. Deliberately the plain read
#: right and not the manage one: seeing who holds a float is reading, not
#: administering.
NAME_PEOPLE_PERMISSION = "cash_book.can_view_cash_book"


class CanViewAccountsBoard(BasePermission):
    """Any one of the cash book's own read rights opens the board."""

    message = (
        "You need a cash book right to read the accounts board. "
        "Ask an administrator."
    )

    def has_permission(self, request, view):
        user = request.user
        return any(user.has_perm(perm) for perm in ACCOUNTS_BOARD_VIEW_PERMISSIONS)


def may_name_people(user) -> bool:
    """Whether this reader may see who is holding the factory's cash.

    False for a board-feed reader -- the display login on the wall carousel --
    which is the case this function exists for. It still gets every total and
    every count; only the names go.
    """
    return bool(user and user.has_perm(NAME_PEOPLE_PERMISSION))
