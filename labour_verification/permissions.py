from rest_framework.permissions import BasePermission


class CanCreateVerificationRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("labour_verification.can_create_verification_request")


class CanViewVerificationRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("labour_verification.can_view_verification_request")


class CanCloseVerificationRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("labour_verification.can_close_verification_request")


class CanSubmitDepartmentLabour(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("labour_verification.can_submit_department_labour")
