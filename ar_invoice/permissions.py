"""Permission classes for the A/R invoice module.

Codenames are declared on ``ARInvoicePosting.Meta.permissions`` and bundled into
the "AR Invoices" group by the data migration. Mirrors ``ap_invoice``.
"""
from rest_framework.permissions import BasePermission


def _perm(codename):
    class _HasPerm(BasePermission):
        message = f"You do not have the '{codename}' permission."

        def has_permission(self, request, view):
            return bool(request.user and request.user.has_perm(codename))

    _HasPerm.__name__ = f"Has_{codename.replace('.', '_')}"
    return _HasPerm


CanViewARInvoice = _perm("ar_invoice.view_ar_invoice_posting")
CanCreateARInvoice = _perm("ar_invoice.create_ar_invoice_posting")
