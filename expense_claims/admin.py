from django.contrib import admin

from .models import ExpenseClaim


@admin.register(ExpenseClaim)
class ExpenseClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "company", "budget_name", "amount", "status", "created_by", "created_at")
    list_filter = ("company", "status")
    search_fields = ("comment", "gl_account_code", "gl_account_name", "gl_description", "budget_name")
    raw_id_fields = ("created_by", "updated_by", "decided_by")
