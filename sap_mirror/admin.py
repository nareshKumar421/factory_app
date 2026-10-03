from django.contrib import admin

from .models import MirrorDataset, ServedBill


@admin.register(MirrorDataset)
class MirrorDatasetAdmin(admin.ModelAdmin):
    list_display = ("company", "name", "synced_at", "row_count", "last_attempt_at", "last_error")
    list_filter = ("name", "company")
    readonly_fields = list_display

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ServedBill)
class ServedBillAdmin(admin.ModelAdmin):
    """Bills handed out from the copy while HANA was down, and what the re-check found."""

    list_display = ("company", "doc_num", "outcome", "served_at", "checked_at", "notified")
    list_filter = ("outcome", "company")
    search_fields = ("doc_num",)
    readonly_fields = (
        "company", "doc_entry", "doc_num", "served_at", "last_served_at", "copy_as_of",
        "outcome", "checked_at", "differences", "linked", "notified",
    )
    exclude = ("served",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
