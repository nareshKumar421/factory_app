from rest_framework.permissions import BasePermission


class CanViewSupplyChain(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("supply_chain.can_view_supply_chain")


class CanManageSupplyChainReference(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("supply_chain.can_manage_supply_chain_reference")
