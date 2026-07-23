from django.contrib import admin

from .models import DocumentCode


@admin.register(DocumentCode)
class DocumentCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "section",
        "doctype",
        "clause",
        "nn",
        "revision_label",
        "issue_date",
        "total_pages",
        "module",
        "created_at",
    )
    list_filter = ("section", "doctype", "module")
    search_fields = ("code", "source_reference")
    readonly_fields = (
        "code",
        "section",
        "doctype",
        "cc",
        "ss",
        "gg",
        "nn",
        "created_at",
        "updated_at",
    )
    ordering = ("section", "doctype", "cc", "ss", "gg", "nn")

    @admin.display(description="Clause")
    def clause(self, obj):
        return obj.clause

    @admin.display(description="Rev")
    def revision_label(self, obj):
        return obj.revision_label
