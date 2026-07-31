from rest_framework.permissions import BasePermission


class CanViewGoodsReturn(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('goods_return.can_view_goods_return')


class CanCreateGoodsReturn(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('goods_return.can_create_goods_return')


class CanEditGoodsReturn(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('goods_return.can_edit_goods_return')


class CanSubmitGoodsReturn(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('goods_return.can_submit_goods_return')


class CanGateInGoodsReturn(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('goods_return.can_gate_in_goods_return')
