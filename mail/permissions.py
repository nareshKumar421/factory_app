from rest_framework.permissions import BasePermission


class CanSendEmail(BasePermission):
    """Permission to send manual emails."""

    def has_permission(self, request, view):
        return request.user.has_perm("mail.can_send_email")


class CanSendBulkEmail(BasePermission):
    """Permission to send bulk/broadcast emails."""

    def has_permission(self, request, view):
        return request.user.has_perm("mail.can_send_bulk_email")
