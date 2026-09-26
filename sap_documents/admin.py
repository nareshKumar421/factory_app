from django.contrib import admin

from .models import SapAttachmentDownload


@admin.register(SapAttachmentDownload)
class SapAttachmentDownloadAdmin(admin.ModelAdmin):
    """Read-only: each row records a file already served."""

    list_display = ("created_at", "company", "abs_entry", "line", "file_name", "size_bytes", "created_by")
    list_filter = ("company",)
    search_fields = ("file_name", "abs_entry", "created_by__email")
    readonly_fields = [f.name for f in SapAttachmentDownload._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
