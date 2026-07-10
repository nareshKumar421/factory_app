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
