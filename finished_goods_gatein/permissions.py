from rest_framework.permissions import BasePermission


class CanReceiveFGPO(BasePermission):
    message = "You do not have permission to receive finished goods POs."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("finished_goods_gatein.can_receive_fg_po")
        )


class CanViewFGReceipt(BasePermission):
    message = "You do not have permission to view finished goods receipts."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("finished_goods_gatein.view_fgreceipt")
        )


class CanCompleteFGEntry(BasePermission):
    message = "You do not have permission to complete finished goods gate entries."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("finished_goods_gatein.can_complete_fg_entry")
        )


class CanDeleteFGEntry(BasePermission):
    message = "You do not have permission to delete finished goods gate entries."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("finished_goods_gatein.delete_fgreceipt")
        )
