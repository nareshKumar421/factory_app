from django.contrib import admin

from .models import SapPosting, SapPostingAttempt


class SapPostingAttemptInline(admin.TabularInline):
    model = SapPostingAttempt
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in SapPostingAttempt._meta.fields]


@admin.register(SapPosting)
class SapPostingAdmin(admin.ModelAdmin):
    """Read-only: the log is written by the app; send or cancel from the app's page."""

    list_display = ["id", "company", "title", "status", "attempts", "created_at", "posted_at"]
    list_filter = ["status", "kind", "company"]
    search_fields = ["title"]
    inlines = [SapPostingAttemptInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in SapPosting._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
