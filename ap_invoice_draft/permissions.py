from rest_framework.permissions import BasePermission


class CanViewAPInvoiceDraft(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('ap_invoice_draft.can_view_ap_invoice_draft')


class CanCreateAPInvoiceDraft(BasePermission):
    """Making an entry writes an A/P invoice draft into SAP. A draft books
    nothing, but it is what accounts will add, so it is a right of its own."""

    def has_permission(self, request, view):
        return request.user.has_perm('ap_invoice_draft.can_create_ap_invoice_draft')
