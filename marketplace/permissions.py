"""Permission classes for the marketplace app.

Custom permission codenames are declared in the model ``Meta.permissions`` blocks
(see ``models.py``) and bundled into a ``marketplace`` group by the data migration.
"""
from rest_framework.permissions import BasePermission


def _perm(codename):
    class _HasPerm(BasePermission):
        message = f"You do not have the '{codename}' permission."

        def has_permission(self, request, view):
            return bool(request.user and request.user.has_perm(codename))

    _HasPerm.__name__ = f"Has_{codename.replace('.', '_')}"
    return _HasPerm


CanViewDispatch = _perm("marketplace.view_dispatch")
CanAddDispatch = _perm("marketplace.add_dispatch")
CanScanDispatch = _perm("marketplace.scan_dispatch")
CanConfirmDispatch = _perm("marketplace.confirm_dispatch")
CanCancelDispatch = _perm("marketplace.cancel_dispatch")
CanViewReturn = _perm("marketplace.view_return")
CanAddReturn = _perm("marketplace.add_return")
CanSubmitReturn = _perm("marketplace.submit_return")
CanViewMaster = _perm("marketplace.view_master")
CanChangeMaster = _perm("marketplace.change_master")
CanViewReconciliation = _perm("marketplace.view_reconciliation")
