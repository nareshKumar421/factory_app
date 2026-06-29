from django.contrib import admin

from .models import (
    Inventory, Location, Material, Movement, Pallet, Settings, Template,
    Warehouse, Zone,
)


class WmsRecordAdmin(admin.ModelAdmin):
    list_display = ('record_id', 'company', 'updated_at')
    list_filter = ('company',)
    search_fields = ('record_id',)
    readonly_fields = ('created_at', 'updated_at')


for _model in (
    Warehouse, Zone, Location, Material, Pallet, Inventory, Movement,
    Template, Settings,
):
    admin.site.register(_model, WmsRecordAdmin)
