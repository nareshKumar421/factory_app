"""Permission classes for the barcode app.

Barcode operations are performed by the ``barcode`` group, which holds the
``barcode.*`` permissions. These classes gate the API so it is not open to every
authenticated company user (it previously only required auth + company context).

Two pallet endpoints — the pallet **list** (search) and pallet **move** — are
also called by the Warehouse Ops (WMS) pallet-move sync
(``useSyncPalletToBarcode``), whose operators hold ``wms.*`` (not ``barcode.*``)
permissions, so those two additionally accept the WMS operator pallet perms.
"""
from rest_framework.permissions import BasePermission

# WMS operator permissions that legitimately drive the pallet-sync bridge.
_WMS_PALLET_SYNC_PERMS = {"wms.change_pallet", "wms.add_movement"}


class HasAnyBarcodePermission(BasePermission):
    """Grants access to a member of the barcode audience — any ``barcode.*``
    permission. (Superusers pass via Django's all-permissions behaviour.)"""

    message = "You do not have barcode module permissions."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return any(perm.startswith("barcode.") for perm in user.get_all_permissions())


class CanAccessBarcodePalletSync(BasePermission):
    """Barcode audience OR a WMS operator — for the pallet list + move endpoints
    that the Warehouse Ops sync calls without holding barcode permissions."""

    message = "You do not have barcode or WMS pallet permissions."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        perms = user.get_all_permissions()
        if any(perm.startswith("barcode.") for perm in perms):
            return True
        return bool(perms & _WMS_PALLET_SYNC_PERMS)


class CanRequestBarcodeActivation(BasePermission):
    """The label-printing side: asking for printed labels to be activated."""

    message = "You cannot request barcode activation."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user.has_perm("barcode.can_request_barcode_activation")


class CanApproveBarcodeActivation(BasePermission):
    """The decision side: activating stock with no physical scan behind it, and
    voiding labels that never arrived. Kept separate from requesting so the
    printer cannot approve their own request."""

    message = "You cannot approve barcode activation."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user.has_perm("barcode.can_approve_barcode_activation")


class CanViewBarcodeActivation(BasePermission):
    """Either side of the workflow may read it — a requester needs to see their
    own ticket, an approver needs the queue."""

    message = "You do not have barcode activation permissions."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return (
            user.has_perm("barcode.can_request_barcode_activation")
            or user.has_perm("barcode.can_approve_barcode_activation")
            or any(perm.startswith("barcode.") for perm in user.get_all_permissions())
        )
