from django.contrib import admin

from .models import SapApprovalDecision


@admin.register(SapApprovalDecision)
class SapApprovalDecisionAdmin(admin.ModelAdmin):
    """Read-only: each row records something SAP already accepted."""

    list_display = (
        "created_at", "company", "wdd_code", "object_type", "action",
        "changed_from", "signed_as", "typed_password", "confirmed_duplicate", "created_by",
    )
    list_filter = ("company", "action", "changed_from", "typed_password", "confirmed_duplicate")
    search_fields = ("wdd_code", "signed_as", "draft_entry")
    readonly_fields = [f.name for f in SapApprovalDecision._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
