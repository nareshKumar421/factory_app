from rest_framework.permissions import BasePermission


class CanViewDismantle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dismantle.can_view_dismantle")


class CanCreateDismantle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dismantle.can_create_dismantle")


class CanEditDismantle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dismantle.can_edit_dismantle")


class CanPostDismantle(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("dismantle.can_post_dismantle")
