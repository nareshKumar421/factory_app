from django.contrib import admin

from .models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest


class BOMChangeLineInline(admin.TabularInline):
    model = BOMChangeLine
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in BOMChangeLine._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


class BOMChangeApprovalInline(admin.TabularInline):
    model = BOMChangeApproval
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in BOMChangeApproval._meta.fields]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BOMChangeRequest)
class BOMChangeRequestAdmin(admin.ModelAdmin):
    """Read-only: a request moves only through the API, which checks the rights
    at each level and writes SAP. Editing a status here would skip both."""

    list_display = (
        "id", "company", "kind", "item_code", "status", "submitted_at", "created_by",
        "legacy_portal_id",
    )
    list_filter = ("company", "kind", "status")
    search_fields = ("item_code", "item_name", "legacy_submitted_by")
    readonly_fields = [f.name for f in BOMChangeRequest._meta.fields]
    inlines = [BOMChangeLineInline, BOMChangeApprovalInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
