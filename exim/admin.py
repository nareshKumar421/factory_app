from django.contrib import admin

from .models import EximUser


@admin.register(EximUser)
class EximUserAdmin(admin.ModelAdmin):
    """Read-only: the link is written by ``import_exim_users`` and nothing else."""

    list_display = ("exim_id", "exim_email", "user", "created_user", "last_synced_at")
    list_filter = ("created_user",)
    search_fields = ("exim_email", "user__email", "user__full_name")
    readonly_fields = ("exim_id", "exim_email", "user", "created_user", "first_imported_at", "last_synced_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
