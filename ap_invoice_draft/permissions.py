from rest_framework.permissions import BasePermission


class CanViewAPInvoiceDraft(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('ap_invoice_draft.can_view_ap_invoice_draft')


class CanCreateAPInvoiceDraft(BasePermission):
    """Making an entry writes an A/P invoice draft into SAP. A draft books
    nothing, but it is what accounts will add, so it is a right of its own."""

    def has_permission(self, request, view):
        return request.user.has_perm('ap_invoice_draft.can_create_ap_invoice_draft')


class CanSeeGRPOAPStatus(BasePermission):
    """Whoever sees posted GRPOs, or this module, may see whether their A/P
    invoice is posted."""

    PERMISSIONS = (
        'grpo.can_view_grpo_history',
        'grpo.view_grpoposting',
        'ap_invoice_draft.can_view_ap_invoice_draft',
        'ap_invoice_draft.can_create_ap_invoice_draft',
    )

    def has_permission(self, request, view):
        return any(request.user.has_perm(perm) for perm in self.PERMISSIONS)


class CanReviewAPInvoiceDraft(BasePermission):
    """Marking a check OK or Not OK overrides what the app found."""

    def has_permission(self, request, view):
        return request.user.has_perm('ap_invoice_draft.can_review_ap_invoice_draft')
