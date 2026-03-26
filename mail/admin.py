from django.contrib import admin
from .models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ["subject", "recipient_email", "email_type", "status", "created_at"]
    list_filter = ["status", "email_type", "template_name", "created_at"]
    search_fields = ["recipient_email", "subject"]
    readonly_fields = [
        "recipient", "recipient_email", "company", "subject", "body",
        "email_type", "click_action_url", "reference_type", "reference_id",
        "template_name", "extra_data", "status", "error_message",
        "created_by", "created_at",
    ]
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
