from django.contrib import admin

from .models import ExpenseClaim, ExpenseClaimAttachment


class ExpenseClaimAttachmentInline(admin.TabularInline):
    model = ExpenseClaimAttachment
    extra = 0
    fields = ("file", "original_filename", "size_bytes", "created_by", "created_at")
    readonly_fields = ("created_at",)
    raw_id_fields = ("created_by",)


@admin.register(ExpenseClaim)
class ExpenseClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "company", "budget_name", "amount", "status", "created_by", "created_at")
    list_filter = ("company", "status")
    search_fields = ("comment", "gl_account_code", "gl_account_name", "gl_description", "budget_name")
    raw_id_fields = ("created_by", "updated_by", "decided_by")
    inlines = [ExpenseClaimAttachmentInline]
