"""
amounts_board/permissions.py

Who may read the Amounts board, and who may name its owners.

UNLIKE THE OTHER BOARDS, THIS ONE MINTS ITS OWN RIGHT
-----------------------------------------------------
Admin, Plant and Logistics Control are existing reports on one screen, so
holding a report's right already means being allowed to read the board. This
board shows what customers owe and what the stock is worth in rupees, which no
stock or warehouse right has ever disclosed -- a warehouse login holding the
stock right must not find the company's debtors behind it. So it has a right of
its own, created by this app's migration.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "amounts_board.can_view_amounts_board"
MANAGE_OWNERS_PERMISSION = "amounts_board.can_manage_stock_owners"


class CanViewAmountsBoard(BasePermission):
    message = "You need the Amounts board permission to read this board."

    def has_permission(self, request, view):
        return request.user.has_perm(VIEW_PERMISSION)


class CanManageStockOwners(BasePermission):
    message = "You need the permission to set the Amounts board's stock owners."

    def has_permission(self, request, view):
        return request.user.has_perm(MANAGE_OWNERS_PERMISSION)
