from django.contrib import admin

from .models import (
    DailyLog,
    EstimateLine,
    ExpenseBatch,
    DailyLogStopReason,
    Expense,
    Project,
    ProjectAttachment,
    ProjectRevision,
)


class ProjectRevisionInline(admin.TabularInline):
    model = ProjectRevision
    extra = 0
    fields = (
        "revision_no",
        "additional_amount",
        "new_end_date",
        "status",
        "reason",
        "decided_by",
    )
    readonly_fields = ("revision_no",)


class EstimateLineInline(admin.TabularInline):
    model = EstimateLine
    extra = 0
    fields = ("line_no", "material", "quantity", "unit", "rate", "amount")
    readonly_fields = ("amount",)


class ProjectAttachmentInline(admin.TabularInline):
    model = ProjectAttachment
    extra = 0
    fields = ("file", "title", "is_active")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "company",
        "status",
        "sanctioned_budget",
        "spent_amount",
        "expected_end_date",
        "progress_percent",
    )
    list_filter = ("company", "status")
    search_fields = ("code", "name", "location")
    readonly_fields = (
        "code",
        "sanctioned_budget",
        "spent_amount",
        "progress_percent",
        "created_at",
        "updated_at",
    )
    inlines = [EstimateLineInline, ProjectRevisionInline, ProjectAttachmentInline]


class DailyLogStopReasonInline(admin.TabularInline):
    model = DailyLogStopReason
    extra = 0


@admin.register(DailyLog)
class DailyLogAdmin(admin.ModelAdmin):
    list_display = ("project", "log_date", "workers_count", "work_stopped", "progress_percent")
    list_filter = ("work_stopped", "stop_reasons__reason")
    search_fields = ("project__code", "project__name", "work_done")
    date_hierarchy = "log_date"
    inlines = [DailyLogStopReasonInline]


@admin.register(ExpenseBatch)
class ExpenseBatchAdmin(admin.ModelAdmin):
    list_display = ("project", "batch_no", "status", "line_count", "total", "submitted_at")
    list_filter = ("status",)
    search_fields = ("project__code", "project__name")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "project", "spend_date", "category", "description", "amount", "paid_to", "batch",
    )
    list_filter = ("category", "payment_mode", "batch__status")
    search_fields = ("project__code", "description", "paid_to", "reference_no")
    date_hierarchy = "spend_date"
