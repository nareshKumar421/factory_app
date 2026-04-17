from django.contrib import admin
from .models import LabourVerificationRequest, DepartmentLabourResponse


class DepartmentLabourResponseInline(admin.TabularInline):
    model = DepartmentLabourResponse
    extra = 0
    readonly_fields = ("department", "submitted_by", "submitted_at", "created_at", "updated_at")


@admin.register(LabourVerificationRequest)
class LabourVerificationRequestAdmin(admin.ModelAdmin):
    list_display = ("date", "status", "created_by", "created_at", "closed_at")
    list_filter = ("status", "date")
    search_fields = ("created_by__email", "created_by__full_name")
    readonly_fields = ("created_at",)
    inlines = [DepartmentLabourResponseInline]


@admin.register(DepartmentLabourResponse)
class DepartmentLabourResponseAdmin(admin.ModelAdmin):
    list_display = ("verification_request", "department", "labour_count", "status", "submitted_by", "submitted_at")
    list_filter = ("status", "department")
    search_fields = ("department__name", "submitted_by__email")
    readonly_fields = ("created_at", "updated_at")
