from django.contrib import admin

from .models import MirrorDataset


@admin.register(MirrorDataset)
class MirrorDatasetAdmin(admin.ModelAdmin):
    list_display = ("company", "name", "synced_at", "row_count", "last_attempt_at", "last_error")
    list_filter = ("name", "company")
    readonly_fields = list_display

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
