from django.contrib import admin

from .models import ApiCall


@admin.register(ApiCall)
class ApiCallAdmin(admin.ModelAdmin):
    """Read-only: the middleware writes the log, and prune_api_log empties it."""

    list_display = [
        "started_at", "user", "company_code", "method", "path", "status_code", "duration_ms",
    ]
    list_filter = ["method", "app_label", "company_code"]
    search_fields = ["path", "user__email"]
    list_select_related = ["user"]
    # The table grows by every call; a COUNT(*) of it on each page would not.
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
