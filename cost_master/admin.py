from django.contrib import admin

from .models import CostType, CostRate


@admin.register(CostType)
class CostTypeAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'default_basis', 'is_credit', 'is_active')
    list_filter = ('default_basis', 'is_credit', 'is_active')
    search_fields = ('code', 'name')


@admin.register(CostRate)
class CostRateAdmin(admin.ModelAdmin):
    list_display = ('cost_type', 'scope', 'company', 'department', 'value_key',
                    'basis', 'rate', 'effective_from', 'is_active')
    list_filter = ('scope', 'basis', 'company', 'is_active')
    search_fields = ('cost_type__code', 'cost_type__name', 'value_key')
    list_select_related = ('cost_type', 'company', 'department')
