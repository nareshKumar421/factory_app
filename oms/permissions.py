"""Permission classes for the OMS invoice-approval proxy.

Codenames are declared on ``InvoiceApprovalAudit.Meta.permissions`` and bundled into
the "Invoice Approval" group by the data migration. Mirrors ``marketplace/permissions.py``.
"""
from rest_framework.permissions import BasePermission


def _perm(codename):
    class _HasPerm(BasePermission):
        message = f"You do not have the '{codename}' permission."

        def has_permission(self, request, view):
            return bool(request.user and request.user.has_perm(codename))

    _HasPerm.__name__ = f"Has_{codename.replace('.', '_')}"
    return _HasPerm


CanViewInvoice = _perm("oms.view_invoice")
CanApproveInvoice = _perm("oms.approve_invoice")
