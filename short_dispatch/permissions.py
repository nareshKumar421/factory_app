from rest_framework.permissions import BasePermission


class CanViewShortDispatch(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('short_dispatch.can_view_short_dispatch')


class CanCreateShortDispatch(BasePermission):
    """Creating *is* posting: the form writes an A/R Return into SAP that nobody
    here can withdraw, so this permission is the whole gate on the module."""

    def has_permission(self, request, view):
        return request.user.has_perm('short_dispatch.can_create_short_dispatch')
