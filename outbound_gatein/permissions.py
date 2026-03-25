from rest_framework.permissions import BasePermission


class CanCreateOutboundEntry(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.add_outboundgateentry")


class CanViewOutboundEntry(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.view_outboundgateentry")


class CanEditOutboundEntry(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.change_outboundgateentry")


class CanCompleteOutboundEntry(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.can_complete_outbound_entry")


class CanReleaseForLoading(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.can_release_for_loading")


class CanViewOutboundPurpose(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.view_outboundpurpose")


class CanManageOutboundPurpose(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_gatein.can_manage_outbound_purpose")
