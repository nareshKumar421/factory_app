from django.contrib import admin

from .models import Issue, IssueAttachment, IssueComment, IssueEvent, IssueLabel


class IssueCommentInline(admin.TabularInline):
    model = IssueComment
    extra = 0
    fields = ("author", "body", "created_at", "edited_at")
    readonly_fields = ("created_at",)


class IssueEventInline(admin.TabularInline):
    model = IssueEvent
    extra = 0
    # The timeline is append-only, and the admin must not be the one exception.
    fields = ("created_at", "actor", "event", "detail")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj):
        return False


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "title",
        "state",
        "priority",
        "author",
        "comment_count",
        "last_activity_at",
    )
    list_filter = ("state", "priority", "labels", "company", "pinned")
    search_fields = ("number", "title", "body")
    list_select_related = ("author",)
    filter_horizontal = ("labels", "assignees")
    readonly_fields = ("number", "comment_count", "last_activity_at")
    inlines = [IssueCommentInline, IssueEventInline]


@admin.register(IssueLabel)
class IssueLabelAdmin(admin.ModelAdmin):
    list_display = ("name", "color", "description", "sequence", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(IssueAttachment)
class IssueAttachmentAdmin(admin.ModelAdmin):
    list_display = (
        "original_filename",
        "issue",
        "comment",
        "size_bytes",
        "uploaded_by",
        "uploaded_at",
    )
    list_select_related = ("issue", "uploaded_by")
    search_fields = ("original_filename",)
