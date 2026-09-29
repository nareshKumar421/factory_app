from rest_framework.permissions import BasePermission

# Django's own model permissions: superusers hold them without being given them,
# and anyone else is given them in the admin.
VIEW = "sap_postings.view_sapposting"
CHANGE = "sap_postings.change_sapposting"


class CanViewSapPostings(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(VIEW)


class CanActOnSapPostings(BasePermission):
    """Send again, or cancel: both change what the app will do in SAP."""

    def has_permission(self, request, view):
        return request.user.has_perm(CHANGE)
