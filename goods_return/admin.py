from django.contrib import admin

from .models import (
    GoodsReturn,
    GoodsReturnAttachment,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
)


class GoodsReturnInvoiceRefInline(admin.TabularInline):
    model = GoodsReturnInvoiceRef
    extra = 0


class GoodsReturnItemInline(admin.TabularInline):
    model = GoodsReturnItem
    extra = 0


class GoodsReturnAttachmentInline(admin.TabularInline):
    model = GoodsReturnAttachment
    extra = 0


@admin.register(GoodsReturn)
class GoodsReturnAdmin(admin.ModelAdmin):
    list_display = ("entry_no", "company", "basis", "status", "customer_name", "created_at")
    list_filter = ("company", "basis", "status")
    search_fields = ("entry_no", "customer_code", "customer_name")
    inlines = [GoodsReturnInvoiceRefInline, GoodsReturnItemInline, GoodsReturnAttachmentInline]
