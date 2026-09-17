"""
admin_board/permissions.py

Who may read the admin control board.

**It mints no right of its own, deliberately.** The board is four existing
reports on one screen — SAP warehouse stock, the production plan, dispatch and
the factory expense wall — so holding any of those rights already means being
allowed to read this. A dedicated permission would have to be created as a row
on the live database and added to every relevant group before anyone could open
the board, and it would buy nothing.

That follows the precedent set by ``plant_board.permissions`` and
``packing_material.permissions``, and it is why this app ships no model and no
migration.

THE COST TILE IS THE EXCEPTION WORTH STATING
--------------------------------------------
This board shows the factory's wage and power bill to anyone holding *any* of
the rights below, including a warehouse login that holds only the stock right.
That is the same disclosure the Logistics board already makes and the business
accepted it there knowingly — the figures are factory totals, not payslips, and
there is no per-employee figure anywhere on this screen.

It is recorded here rather than left implicit because it is the one thing on the
board that a reader might not expect their stock permission to buy them. Narrow
this class the day that changes, and note that doing so needs a real permission
row on the live database.
"""

from rest_framework.permissions import BasePermission

ADMIN_BOARD_VIEW_PERMISSIONS = (
    "stock_dashboard.can_view_stock_dashboard",
    "planning_purchase.can_view_production_plan",
    "dispatch_plans.can_view_dispatch_plans",
    "factory_expense.can_view_factory_expense",
)


class CanViewAdminBoard(BasePermission):
    """Any one of the rights the board's own reports are gated on."""

    message = (
        "You need one of the stock, production plan, dispatch or factory "
        "expense permissions to read this board."
    )

    def has_permission(self, request, view):
        user = request.user
        return any(user.has_perm(perm) for perm in ADMIN_BOARD_VIEW_PERMISSIONS)
