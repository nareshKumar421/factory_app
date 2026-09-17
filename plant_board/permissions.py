"""
plant_board/permissions.py

Who may read the plant control board.

**It mints no right of its own, deliberately.** The board is four existing
reports on one screen — the packing-material requirement sheet, SAP warehouse
stock, the non-moving report and the production plan — so holding any of those
rights already means being allowed to read this. A dedicated permission would
have to be created as a row on the live database and added to every relevant
group before anyone could open the board, and it would buy nothing.

That follows the precedent set by ``packing_material.permissions``, and it is
why this app ships no model and no migration.

The cost is real and worth stating: the board cannot be restricted
independently of the reports it summarises. Mint a right the day that matters.

A second consequence matters more on a wall board than on a page. This class
accepts *any* of the four rights, while the board itself is all-or-nothing —
there is nobody at a TV to be shown a partial screen. So a login holding only
one of these opens the board and sees the other bands' tiles empty with a
reason on their face, which the service reports as ``degraded`` rather than as
an error.
"""

from rest_framework.permissions import BasePermission

PLANT_BOARD_VIEW_PERMISSIONS = (
    "stock_dashboard.can_view_stock_dashboard",
    "non_moving_rm.can_view_non_moving_rm",
    "production_execution.can_view_reports",
    "planning_purchase.can_view_production_plan",
)


class CanViewPlantBoard(BasePermission):
    """Any one of the rights the board's own reports are gated on."""

    message = (
        "You need one of the stock, non-moving, production reports or "
        "production plan permissions to read this board."
    )

    def has_permission(self, request, view):
        user = request.user
        return any(user.has_perm(perm) for perm in PLANT_BOARD_VIEW_PERMISSIONS)
