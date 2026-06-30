"""
Feature-rich Django admin for the Warehouse Ops (WMS) module.

Every WMS collection stores its record as a camelCase JSON document in ``data``
(see ``wms.models``), so a plain ``ModelAdmin`` would only show the opaque
``record_id``. This admin makes each collection fully manageable from the Django
admin:

* meaningful, colour-badged columns pulled out of the ``data`` document;
* filters on JSON fields (status / type / mode …) backed by Postgres key lookups;
* JSON-aware search across the relevant keys;
* export actions (JSON and CSV) for the selected rows;
* a clean detail layout — a formatted read-only view plus the raw, editable
  ``data`` (collapsed) so an admin can correct a record in place;
* Django's built-in bulk delete on every list.
"""
import csv
import json

from django.contrib import admin
from django.db.models import Count
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.utils.html import format_html

from .models import (
    Dashboard, Inventory, Location, Material, Movement, Pallet, Settings,
    Template, Warehouse, Zone,
)

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    # pallet
    'ACTIVE': '#16a34a', 'PARTIAL': '#d97706', 'EMPTY': '#6b7280',
    'PICKED': '#2563eb', 'SHIPPED': '#475569', 'ON_HOLD': '#dc2626',
    # location
    'BLOCKED': '#991b1b', 'DAMAGED': '#dc2626', 'MAINTENANCE': '#7c3aed',
    'RESERVED': '#2563eb',
}

MOVEMENT_COLORS = {
    'RECEIVE': '#16a34a', 'PUTAWAY': '#0d9488', 'TRANSFER': '#2563eb',
    'PICK': '#d97706', 'OUTBOUND': '#475569', 'ADJUSTMENT': '#dc2626',
    'AUDIT': '#7c3aed', 'CYCLE_COUNT': '#9333ea',
}


def data_column(key, label=None, transform=None):
    """A list_display callable that reads ``obj.data[key]``."""

    def accessor(obj):
        value = obj.data.get(key) if isinstance(obj.data, dict) else None
        if transform is not None:
            value = transform(value, obj)
        if value is None or value == '':
            return '—'
        return value

    accessor.short_description = label or key
    accessor.__name__ = f'data_{key}'
    return accessor


def badge_column(key, label, palette):
    """A list_display callable rendering ``obj.data[key]`` as a colour badge."""

    def accessor(obj):
        value = obj.data.get(key) if isinstance(obj.data, dict) else None
        if value in (None, ''):
            return '—'
        color = palette.get(str(value), '#6b7280')
        return format_html(
            '<span style="display:inline-block;padding:2px 8px;border-radius:9999px;'
            'background:{};color:#fff;font-size:11px;font-weight:600">{}</span>',
            color, value,
        )

    accessor.short_description = label
    accessor.__name__ = f'badge_{key}'
    return accessor


def _bool(value, _obj):
    if value is None:
        return None
    return '✓' if value else '✗'


def _item(value, obj):
    """Item name, falling back to item code."""
    return value or (obj.data.get('itemCode') if isinstance(obj.data, dict) else None)


def json_filter(key, title, choices=None):
    """Build a SimpleListFilter that filters on ``data[key]`` (Postgres lookup)."""

    class _Filter(admin.SimpleListFilter):
        pass

    _Filter.title = title
    _Filter.parameter_name = key

    def lookups(self, request, model_admin):
        if choices is not None:
            return [(c, c) for c in choices]
        values = (
            model_admin.model.objects
            .filter(**{f'data__{key}__isnull': False})
            .values_list(f'data__{key}', flat=True)
            .distinct()
        )
        seen = []
        for value in values:
            if value not in (None, '') and value not in seen:
                seen.append(value)
        return [(str(v), str(v)) for v in sorted(seen, key=str)]

    def queryset(self, request, queryset):
        value = self.value()
        if value:
            return queryset.filter(**{f'data__{key}': value})
        return queryset

    _Filter.lookups = lookups
    _Filter.queryset = queryset
    _Filter.__name__ = f'{key.capitalize()}Filter'
    return _Filter


# ---------------------------------------------------------------------------
# Export actions
# ---------------------------------------------------------------------------

@admin.action(description='Export selected as JSON')
def export_as_json(modeladmin, request, queryset):
    payload = [
        {'record_id': obj.record_id, 'company': obj.company_id, 'data': obj.data}
        for obj in queryset
    ]
    response = HttpResponse(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        content_type='application/json',
    )
    response['Content-Disposition'] = (
        f'attachment; filename="{modeladmin.model.__name__.lower()}_export.json"'
    )
    return response


@admin.action(description='Export selected as CSV')
def export_as_csv(modeladmin, request, queryset):
    rows = list(queryset)
    keys = []
    for obj in rows:
        if isinstance(obj.data, dict):
            for key in obj.data:
                if key not in keys:
                    keys.append(key)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="{modeladmin.model.__name__.lower()}_export.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(['record_id', *keys, 'updated_at'])
    for obj in rows:
        data = obj.data if isinstance(obj.data, dict) else {}
        writer.writerow([
            obj.record_id,
            *[_csv_cell(data.get(key, '')) for key in keys],
            obj.updated_at.isoformat() if obj.updated_at else '',
        ])
    return response


def _csv_cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


# ---------------------------------------------------------------------------
# Admin classes
# ---------------------------------------------------------------------------

class WmsRecordAdmin(admin.ModelAdmin):
    """Base admin shared by every WMS collection."""

    list_filter = ('company', 'created_at')
    date_hierarchy = 'created_at'
    list_per_page = 50
    ordering = ('-updated_at',)
    actions = (export_as_json, export_as_csv)
    search_fields = ('record_id',)
    empty_value_display = '—'
    data_search_keys = ()
    change_list_template = 'admin/wms/change_list.html'
    # When set, the changelist shows colour-coded count chips for this data key.
    summary_key = None
    summary_palette = {}

    class Media:
        css = {'all': ('admin/wms/wms.css',)}

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        chips = [{'label': 'Total', 'n': self.model.objects.count(), 'total': True}]
        if self.summary_key:
            rows = (
                self.model.objects
                .filter(**{f'data__{self.summary_key}__isnull': False})
                .values(f'data__{self.summary_key}')
                .annotate(n=Count('id'))
                .order_by('-n')
            )
            for row in rows[:10]:
                label = row[f'data__{self.summary_key}'] or '—'
                chips.append({
                    'label': label,
                    'n': row['n'],
                    'color': self.summary_palette.get(str(label), '#94a3b8'),
                })
        extra_context['wms_summary'] = chips
        return super().changelist_view(request, extra_context)

    fieldsets = (
        (None, {'fields': ('company', 'record_id')}),
        ('Record', {'fields': ('formatted_data',)}),
        ('Raw data (editable)', {
            'classes': ('collapse',),
            'description': 'The exact JSON document the frontend stores. Edit with care.',
            'fields': ('data',),
        }),
        ('Timestamps', {'classes': ('collapse',), 'fields': ('created_at', 'updated_at')}),
    )

    def get_readonly_fields(self, request, obj=None):
        base = ['created_at', 'updated_at', 'formatted_data']
        if obj is not None:  # editing an existing record — don't let the id drift
            base.append('record_id')
        return base

    @admin.display(description='Data (formatted)')
    def formatted_data(self, obj):
        return format_html(
            '<pre style="white-space:pre-wrap;max-width:60rem;background:#f8fafc;'
            'border:1px solid #e2e8f0;border-radius:6px;padding:10px">{}</pre>',
            json.dumps(obj.data, indent=2, ensure_ascii=False),
        )

    def get_search_results(self, request, queryset, search_term):
        results, may_have_duplicates = super().get_search_results(
            request, queryset, search_term,
        )
        term = (search_term or '').strip().lower()
        if term and self.data_search_keys:
            matched_ids = [
                obj.pk for obj in queryset
                if isinstance(obj.data, dict)
                and any(term in str(obj.data.get(k, '')).lower() for k in self.data_search_keys)
            ]
            if matched_ids:
                results = (results | self.model.objects.filter(pk__in=matched_ids)).distinct()
        return results, may_have_duplicates


@admin.register(Warehouse)
class WarehouseAdmin(WmsRecordAdmin):
    list_display = (
        data_column('code', 'Code'),
        data_column('name', 'Name'),
        data_column('type', 'Type'),
        data_column('columns', 'Cols'),
        data_column('rows', 'Rows'),
        data_column('levels', 'Levels'),
        data_column('enabled', 'Enabled', transform=_bool),
        'company', 'updated_at',
    )
    list_filter = (json_filter('type', 'Type', choices=('OWN', 'SAP')), 'company', 'created_at')
    data_search_keys = ('code', 'name', 'type')
    summary_key = 'type'


@admin.register(Zone)
class ZoneAdmin(WmsRecordAdmin):
    list_display = (
        data_column('code', 'Code'),
        data_column('name', 'Name'),
        data_column('type', 'Type'),
        data_column('temperatureClass', 'Temp'),
        'company', 'updated_at',
    )
    list_filter = (json_filter('type', 'Type'), json_filter('temperatureClass', 'Temperature'), 'company')
    data_search_keys = ('code', 'name', 'type')
    summary_key = 'type'


@admin.register(Location)
class LocationAdmin(WmsRecordAdmin):
    list_display = (
        data_column('code', 'Code'),
        data_column('type', 'Type'),
        badge_column('status', 'Status', STATUS_COLORS),
        data_column('barcode', 'Barcode'),
        data_column('enabled', 'Enabled', transform=_bool),
        'company', 'updated_at',
    )
    list_filter = (
        json_filter('status', 'Status', choices=('ACTIVE', 'BLOCKED', 'DAMAGED', 'MAINTENANCE', 'RESERVED')),
        json_filter('type', 'Type'),
        'company',
    )
    data_search_keys = ('code', 'barcode', 'type', 'status')
    summary_key = 'status'
    summary_palette = STATUS_COLORS


@admin.register(Material)
class MaterialAdmin(WmsRecordAdmin):
    list_display = (
        data_column('itemCode', 'Item code'),
        data_column('itemName', 'Item name'),
        data_column('materialType', 'Type'),
        data_column('defaultUom', 'UoM'),
        data_column('temperatureClass', 'Temp'),
        'company', 'updated_at',
    )
    list_filter = (json_filter('materialType', 'Material type'), json_filter('temperatureClass', 'Temperature'), 'company')
    data_search_keys = ('itemCode', 'itemName', 'materialType')
    summary_key = 'materialType'


@admin.register(Pallet)
class PalletAdmin(WmsRecordAdmin):
    list_display = (
        data_column('licensePlate', 'License plate'),
        data_column('itemName', 'Item', transform=_item),
        data_column('boxCount', 'Boxes'),
        badge_column('status', 'Status', STATUS_COLORS),
        data_column('lotNumber', 'Lot'),
        'company', 'updated_at',
    )
    list_filter = (
        json_filter('status', 'Status', choices=('ACTIVE', 'PARTIAL', 'EMPTY', 'PICKED', 'SHIPPED', 'ON_HOLD')),
        'company', 'created_at',
    )
    data_search_keys = ('licensePlate', 'itemCode', 'itemName', 'lotNumber', 'status')
    summary_key = 'status'
    summary_palette = STATUS_COLORS


@admin.register(Inventory)
class InventoryAdmin(WmsRecordAdmin):
    list_display = (
        data_column('itemName', 'Item', transform=_item),
        data_column('quantity', 'Qty'),
        data_column('uom', 'UoM'),
        data_column('lotNumber', 'Lot'),
        data_column('palletId', 'On pallet?', transform=lambda v, o: '✓' if v else '—'),
        'company', 'updated_at',
    )
    data_search_keys = ('itemCode', 'itemName', 'lotNumber', 'serialNumber')


@admin.register(Movement)
class MovementAdmin(WmsRecordAdmin):
    list_display = (
        badge_column('type', 'Type', MOVEMENT_COLORS),
        data_column('itemName', 'Item', transform=_item),
        data_column('quantity', 'Qty'),
        data_column('boxCount', 'Boxes'),
        data_column('userName', 'By'),
        'created_at', 'company',
    )
    list_filter = (
        json_filter('type', 'Type', choices=tuple(MOVEMENT_COLORS)),
        'company', 'created_at',
    )
    ordering = ('-created_at',)
    data_search_keys = ('type', 'itemCode', 'itemName', 'lotNumber', 'note')
    summary_key = 'type'
    summary_palette = MOVEMENT_COLORS


@admin.register(Template)
class TemplateAdmin(WmsRecordAdmin):
    list_display = (
        data_column('name', 'Name'),
        data_column('columns', 'Cols'),
        data_column('rows', 'Rows'),
        data_column('levels', 'Levels'),
        'company', 'updated_at',
    )
    data_search_keys = ('name',)


@admin.register(Settings)
class SettingsAdmin(WmsRecordAdmin):
    list_display = (
        'record_id',
        data_column('masterEnabled', 'Enabled', transform=_bool),
        data_column('putawayMode', 'Putaway'),
        data_column('pickStrategy', 'Pick'),
        data_column('role', 'Role'),
        'company', 'updated_at',
    )


def _ranked(rows, key, palette):
    """Attach a colour + bar percentage (relative to the largest count)."""
    items = [{'label': r[f'data__{key}'] or '—', 'n': r['n']} for r in rows]
    top = max((i['n'] for i in items), default=0) or 1
    for item in items:
        item['color'] = palette.get(str(item['label']), '#94a3b8')
        item['pct'] = round(item['n'] * 100 / top)
    return items


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    """A view-only overview page for the whole WMS module."""

    class Media:
        css = {'all': ('admin/wms/wms.css',)}

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        models = {
            'Warehouse': Warehouse, 'Zone': Zone, 'Location': Location,
            'Material': Material, 'Pallet': Pallet, 'Inventory': Inventory,
            'Movement': Movement, 'Template': Template,
        }
        counts = {name: model.objects.count() for name, model in models.items()}

        pallet_status = _ranked(
            Pallet.objects.filter(data__status__isnull=False)
            .values('data__status').annotate(n=Count('id')).order_by('-n'),
            'status', STATUS_COLORS,
        )
        location_status = _ranked(
            Location.objects.filter(data__status__isnull=False)
            .values('data__status').annotate(n=Count('id')).order_by('-n'),
            'status', STATUS_COLORS,
        )
        movement_type = _ranked(
            Movement.objects.filter(data__type__isnull=False)
            .values('data__type').annotate(n=Count('id')).order_by('-n')[:10],
            'type', MOVEMENT_COLORS,
        )

        occupied = (
            Inventory.objects.filter(data__locationId__isnull=False)
            .values('data__locationId').distinct().count()
        )
        location_total = counts['Location'] or 1
        recent = list(Movement.objects.order_by('-created_at')[:10])
        for movement in recent:
            data = movement.data if isinstance(movement.data, dict) else {}
            movement.color = MOVEMENT_COLORS.get(str(data.get('type')), '#94a3b8')

        context = {
            **self.admin_site.each_context(request),
            'title': 'Warehouse Ops — Dashboard',
            'counts': counts,
            'pallet_status': pallet_status,
            'location_status': location_status,
            'movement_type': movement_type,
            'occupied': occupied,
            'occupied_pct': round(occupied * 100 / location_total),
            'recent': recent,
        }
        return TemplateResponse(request, 'admin/wms/dashboard.html', context)
