"""
Read-mostly admin for the cash book.

The balance is maintained by :mod:`cash_book.services`, so editing an amount
here would leave the column wrong from that row down. Amount and direction are
therefore read-only in the admin: corrections go through the page, which
rewrites the tail.
"""

from django.contrib import admin

from .models import CashBunch, CashEntry


@admin.register(CashEntry)
class CashEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company",
        "entry_date",
        "direction",
        "amount",
        "balance_after",
        "department",
        "gl_account_code",
        "bunch",
        "is_active",
    )
    list_filter = ("company", "direction", "is_active", "entry_date")
    search_fields = ("detail", "item", "gl_account_code", "gl_account_name")
    autocomplete_fields = ()
    readonly_fields = (
        "direction",
        "amount",
        "balance_after",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    )
    date_hierarchy = "entry_date"


@admin.register(CashBunch)
class CashBunchAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "company",
        "status",
        "sent_at",
        "sent_by",
        "decided_at",
        "decided_by",
    )
    list_filter = ("company", "status")
    search_fields = ("number", "remarks", "decision_note")
    readonly_fields = (
        "number",
        "sent_at",
        "sent_by",
        "decided_at",
        "decided_by",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    )
