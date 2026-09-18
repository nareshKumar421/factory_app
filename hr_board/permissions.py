"""
hr_board/permissions.py

Who may read the HR control board.

**It mints no right of its own.** The board is two existing registers on one
screen -- the employee directory and the labour gate -- so holding either
module's read right already means being allowed to read this. A dedicated
permission would have to be created as a row on the live database and added to
every relevant group before anybody could open the board, and it would buy
nothing.

That follows ``admin_board.permissions`` and ``plant_board.permissions``, and it
is why this app ships no model and no migration.

The board feed rights (``control_boards.can_read_workforce_feed`` and
``can_read_labour_feed``) are not listed here: they are applied by
``CanReadBoard`` on the view, which is the layer that knows about feeds. Both
feeds already exist and already mirror exactly the two rights below, so a
dashboard-only login reaches this board without anything new being minted
either.

WHAT THIS BOARD DISCLOSES
-------------------------
Head counts and nothing else. No salary figure is read anywhere in this app --
the salary rights are a separate, narrower family in ``employee_hierarchy`` and
are deliberately not consulted, so opening this board can never reveal pay. No
individual is named either: every tile is a count over a group. That is what
makes it safe to gate on the widely-held directory right.
"""

from rest_framework.permissions import BasePermission

HR_BOARD_VIEW_PERMISSIONS = (
    "employee_hierarchy.can_view_employees",
    "employee_hierarchy.can_view_workforce_reports",
    "labour_gate.view_labourgateentry",
    "labour_gate.can_record_labour_in",
    "labour_gate.can_allocate_labour_department",
)


class CanViewHrBoard(BasePermission):
    """Any one of the rights the board's own registers are gated on.

    One right opens the whole screen and the service then withholds the band
    whose feed the reader does not hold -- an HOD with only the labour rights
    gets the labour tile and a withheld headcount tile, rather than a 403 on a
    board they were granted.
    """

    message = (
        "You need either the employee directory right or a labour gate right "
        "to read this board."
    )

    def has_permission(self, request, view):
        user = request.user
        return any(user.has_perm(perm) for perm in HR_BOARD_VIEW_PERMISSIONS)
