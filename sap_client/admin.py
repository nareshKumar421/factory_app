from django.contrib import admin

from .models import SapApproverIdentity


@admin.register(SapApproverIdentity)
class SapApproverIdentityAdmin(admin.ModelAdmin):
    """Fallback for the /admin/sap-identities page — same table, no SAP reads."""

    list_display = (
        "user",
        "company",
        "sap_user_code",
        "sap_user_name",
        "password_configured",
        "is_active",
    )
    list_filter = ("company", "is_active")
    search_fields = (
        "sap_user_code",
        "sap_user_name",
        "user__full_name",
        "user__email",
        "user__employee_code",
    )
    autocomplete_fields = ()
    raw_id_fields = ("user",)

    @admin.display(boolean=True, description="Password on file")
    def password_configured(self, obj):
        return obj.password_configured
