"""
Permission classes for the Warehouse Ops (WMS) module.

Follows the house pattern (cf. ``maintenance/permissions.py``): a small
``DjangoPermission`` base that simply checks ``request.user.has_perm(...)``, plus
a collection-aware class for the WMS module's single dynamic CRUD endpoint.

Access model:
  * Reads (GET) — open to any authenticated user with company context.
  * Writes — require the matching ``wms.<add|change|delete>_<model>`` permission,
    granted through the ``WMS Admin`` / ``WMS Operator`` groups (see migration
    ``0003_wms_access_groups``). Superusers bypass via ``has_perm``.
  * The ``movements`` audit log is append-only — no edits (PUT/PATCH), and only a
    user with ``delete_movement`` may delete.
  * The ``settings`` singleton may be *seeded* once by anyone (first run) but only
    a user with ``add_settings``/``change_settings`` may overwrite/patch it.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import (
    Inventory, Location, Material, Movement, Pallet, Settings, Template,
    Warehouse, Zone,
)

# URL collection segment -> model. Single source of truth, imported by the views.
COLLECTION_MODELS = {
    'warehouses': Warehouse,
    'zones': Zone,
    'locations': Location,
    'materials': Material,
    'pallets': Pallet,
    'inventory': Inventory,
    'movements': Movement,
    'templates': Template,
    'settings': Settings,
}

APPEND_ONLY_COLLECTIONS = {'movements'}
SETTINGS_COLLECTION = 'settings'

_ACTION_BY_METHOD = {'POST': 'add', 'PUT': 'change', 'PATCH': 'change', 'DELETE': 'delete'}


class DjangoPermission(BasePermission):
    """Base: pass when the user holds ``self.permission`` (house convention)."""

    permission = ''

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


def _settings_already_seeded(request):
    company = getattr(getattr(request, 'company', None), 'company', None)
    if company is None:
        return True  # no company context -> never allow the seed bypass
    return Settings.objects.filter(company=company).exists()


class WmsCollectionPermission(BasePermission):
    """Model-permission gate for the dynamic ``/wms/<collection>/`` endpoints."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True  # reads are open to any authenticated company user

        collection = view.kwargs.get('collection')
        model = COLLECTION_MODELS.get(collection)
        if model is None:
            return True  # unknown collection -> let the view return 404

        # Audit log is append-only: never editable.
        if collection in APPEND_ONLY_COLLECTIONS and request.method in ('PUT', 'PATCH'):
            return False

        action = _ACTION_BY_METHOD.get(request.method)
        if action is None:
            return True

        if request.user and request.user.has_perm(f'wms.{action}_{model._meta.model_name}'):
            return True

        # First-run seed of the settings singleton is allowed for anyone; an
        # existing settings record can only be changed by someone with the perm.
        if (
            collection == SETTINGS_COLLECTION
            and request.method == 'POST'
            and not _settings_already_seeded(request)
        ):
            return True

        return False
