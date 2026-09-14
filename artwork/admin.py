from django.contrib import admin

from .models import ArtworkRecord, ArtworkRevision


class ArtworkRevisionInline(admin.TabularInline):
    model = ArtworkRevision
    extra = 0
    can_delete = False
    readonly_fields = [
        "document_number",
        "revision_number",
        "revision_date",
        "barcode",
        "pdf_original_name",
        "cdr_original_name",
        "superseded_at",
        "superseded_by",
    ]

    def has_add_permission(self, request, obj=None):
        # History is written by the service, never by hand.
        return False


@admin.register(ArtworkRecord)
class ArtworkRecordAdmin(admin.ModelAdmin):
    list_display = [
        "item_code",
        "item_name",
        "sub_group",
        "document_number",
        "revision_number",
        "revision_date",
        "barcode",
        "company",
        "is_active",
    ]
    list_filter = ["company", "sub_group", "is_active"]
    search_fields = ["item_code", "item_name", "document_number", "barcode"]
    readonly_fields = ["created_at", "updated_at", "created_by", "updated_by"]
    inlines = [ArtworkRevisionInline]


@admin.register(ArtworkRevision)
class ArtworkRevisionAdmin(admin.ModelAdmin):
    list_display = [
        "record",
        "document_number",
        "revision_number",
        "revision_date",
        "superseded_at",
        "superseded_by",
    ]
    list_filter = ["record__company", "record__sub_group"]
    search_fields = ["record__item_code", "document_number", "barcode"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
