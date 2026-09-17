"""
Read-mostly admin for the cash book.

The balance is maintained by :mod:`cash_book.services`, so editing an amount
here would leave the column wrong from that row down. Amount and direction are
therefore read-only in the admin: corrections go through the page, which
rewrites the tail.
"""

from django.contrib import admin

from .models import CashBranch, CashBunch, CashEntry


@admin.register(CashEntry)
class CashEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company",
        "entry_date",
        "direction",
        "amount",
        "balance_after",
        "branch",
        "gl_account_code",
        "bunch",
        "is_active",
    )
    list_filter = ("company", "branch", "direction", "is_active", "entry_date")
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


@admin.register(CashBranch)
class CashBranchAdmin(admin.ModelAdmin):
    """The four branches a payment can be filed under, per company.

    Editable here as well as from the settings page -- the page is the one
    people use; this is for putting a list right when nobody has the right yet.
    """

    list_display = ("name", "company", "sort_order", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
