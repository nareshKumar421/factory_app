"""
Persistence for the self-contained Warehouse Ops (WMS) module.

The WMS module is frontend-driven: every record (warehouse, zone, location,
material profile, pallet, inventory line, movement/audit entry, layout template,
and the settings singleton) is authored in the browser as a camelCase JSON
document with a client-generated UUID ``id``. The backend's job is simply to
store those documents durably and share them across devices/users — replacing
the browser-local IndexedDB/localStorage adapters with the API adapter.

Rather than mirror every nested field (capacity, dimensions, material rules,
naming scheme, zone cell lists, …) into columns — which would couple the schema
to the frontend types and risk lossy round-trips — each collection is stored as
a JSON document in ``data``. This guarantees the API returns the exact shape the
module sent, while still giving us a real, queryable, multi-tenant table per
collection (one row per record, scoped to a company).
"""
from django.db import models


class WmsRecord(models.Model):
    """Abstract base shared by every WMS collection.

    ``record_id`` is the client-generated UUID (or the fixed ``wms-settings`` id
    for the settings singleton). It is unique *per company*, so two companies
    keep fully isolated data sets and the settings singleton never collides.
    """

    company = models.ForeignKey(
        'company.Company', on_delete=models.CASCADE, related_name='+'
    )
    record_id = models.CharField(
        max_length=64, db_index=True,
        help_text="Client-generated record id (UUID, or 'wms-settings').",
    )
    data = models.JSONField(
        default=dict,
        help_text="The full camelCase record document as authored by the frontend.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['created_at']
        unique_together = ('company', 'record_id')

    def __str__(self):
        return f"{self.__class__.__name__}<{self.record_id}>"


class Warehouse(WmsRecord):
    pass


class Zone(WmsRecord):
    pass


class CellPurpose(WmsRecord):
    """User-defined cell purpose (walkable path, damaged goods, storage…)."""

    pass


class Location(WmsRecord):
    pass


class Material(WmsRecord):
    pass


class Pallet(WmsRecord):
    pass


class Inventory(WmsRecord):
    class Meta(WmsRecord.Meta):
        verbose_name_plural = 'Inventory'


class Movement(WmsRecord):
    pass


class Template(WmsRecord):
    pass


class Settings(WmsRecord):
    class Meta(WmsRecord.Meta):
        verbose_name_plural = 'Settings'


class Dashboard(Movement):
    """View-only proxy used purely to add a 'Dashboard' page to the WMS admin.

    It reuses the Movement table (no schema change) and is never edited; its
    admin renders an overview of the whole module instead of a record list.
    """

    class Meta:
        proxy = True
        verbose_name = 'Dashboard'
        verbose_name_plural = 'ⓘ Dashboard'  # sorts to the top of the WMS section
