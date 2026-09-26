from django.contrib import admin

from .models import SapBudgetChange


@admin.register(SapBudgetChange)
class SapBudgetChangeAdmin(admin.ModelAdmin):
    """Read-only: each row records a change SAP already accepted."""

    list_display = ("created_at", "company", "action", "doc_entry", "budget_code", "line_count", "created_by")
    list_filter = ("company", "action")
    search_fields = ("budget_code", "sub_budget_code", "doc_entry")
    readonly_fields = [f.name for f in SapBudgetChange._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
