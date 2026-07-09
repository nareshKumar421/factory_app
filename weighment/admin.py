# weighment/admin.py
"""
Enhanced Weighment Admin Configuration for Factory Jivo Wellness
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import Weighment


@admin.register(Weighment)
class WeighmentAdmin(admin.ModelAdmin):
    """
    Comprehensive Weighment Admin with all features
    """
    list_display = (
        "vehicle_entry_link",
        "gross_weight_display",
        "tare_weight_display",
        "net_weight_display",
        "weighbridge_slip_no",
        "first_weighment_time",
        "second_weighment_time",
        "weighment_status",
        "is_active_display",
        "created_at",
    )
    list_display_links = ("vehicle_entry_link",)
    list_filter = (
        "is_active",
        "first_weighment_time",
        "second_weighment_time",
        "created_at",
    )
    search_fields = (
        "vehicle_entry__entry_no",
        "vehicle_entry__vehicle__vehicle_number",
        "vehicle_entry__driver__name",
        "weighbridge_slip_no",
        "remarks",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25

    autocomplete_fields = ["vehicle_entry"]

    fieldsets = (
        ("Entry Information", {
            "fields": ("vehicle_entry",),
            "description": "Associated gate entry"
        }),
        ("Weight Measurements", {
            "fields": (
                ("gross_weight", "tare_weight"),
                "net_weight",
                "challan_weight",
            ),
            "classes": ("wide",),
            "description": "Vehicle weight details"
        }),
        ("Weighbridge Details", {
            "fields": ("weighbridge_slip_no",),
        }),
        ("Timestamps", {
            "fields": ("first_weighment_time", "second_weighment_time"),
            "classes": ("wide",),
        }),
        ("Additional Information", {
            "fields": ("remarks",),
        }),
        ("Status", {
            "fields": ("is_active",),
        }),
        ("Audit Information", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    readonly_fields = (
        "net_weight", "created_by", "created_at",
        "updated_by", "updated_at"
    )

    @admin.display(description="Vehicle Entry", ordering="vehicle_entry__entry_no")
    def vehicle_entry_link(self, obj):
        url = reverse('admin:driver_management_vehicleentry_change', args=[obj.vehicle_entry.id])
        return format_html(
            '<a href="{}" style="font-weight: bold;">{}</a>',
            url, obj.vehicle_entry.entry_no
        )

    @admin.display(description="Gross (Kg)", ordering="gross_weight")
    def gross_weight_display(self, obj):
        if obj.gross_weight:
            return format_html(
                '<span style="color: #3498db; font-weight: bold;">{}</span>',
                f"{obj.gross_weight:,.2f}"
            )
        return format_html('<span style="color: #999;">{}</span>', "-")

    @admin.display(description="Tare (Kg)", ordering="tare_weight")
    def tare_weight_display(self, obj):
        if obj.tare_weight:
            return format_html(
                '<span style="color: #9b59b6; font-weight: bold;">{}</span>',
                f"{obj.tare_weight:,.2f}"
            )
        return format_html('<span style="color: #999;">{}</span>', "-")

    @admin.display(description="Net (Kg)", ordering="net_weight")
    def net_weight_display(self, obj):
        if obj.net_weight and obj.net_weight > 0:
            return format_html(
                '<span style="color: #27ae60; font-weight: bold;">{}</span>',
                f"{obj.net_weight:,.2f}"
            )
        return format_html('<span style="color: #999;">{}</span>', "0.00")

    @admin.display(description="Status")
    def weighment_status(self, obj):
        if obj.gross_weight and obj.tare_weight:
            return format_html(
                '<span style="background-color: #27ae60; color: white; padding: 2px 6px; '
                'border-radius: 4px; font-size: 11px;">{}</span>',
                "Complete"
            )
        elif obj.gross_weight:
            return format_html(
                '<span style="background-color: #f39c12; color: white; padding: 2px 6px; '
                'border-radius: 4px; font-size: 11px;">{}</span>',
                "First Done"
            )
        return format_html(
            '<span style="background-color: #e74c3c; color: white; padding: 2px 6px; '
            'border-radius: 4px; font-size: 11px;">{}</span>',
            "Pending"
        )

    @admin.display(description="Active", boolean=True, ordering="is_active")
    def is_active_display(self, obj):
        return obj.is_active

    # Admin actions
    @admin.action(description="Activate selected weighments")
    def activate_weighments(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} weighment(s) activated.")

    @admin.action(description="Deactivate selected weighments")
    def deactivate_weighments(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} weighment(s) deactivated.")

    @admin.action(description="Reset tare weight to zero")
    def reset_tare_weight(self, request, queryset):
        for w in queryset:
            w.tare_weight = None
            w.second_weighment_time = None
            w.save()
        self.message_user(request, f"{queryset.count()} weighment(s) tare weight reset.")

    @admin.action(description="Clear all weights")
    def clear_weights(self, request, queryset):
        for w in queryset:
            w.gross_weight = None
            w.tare_weight = None
            w.first_weighment_time = None
            w.second_weighment_time = None
            w.save()
        self.message_user(request, f"{queryset.count()} weighment(s) weights cleared.")

    actions = [
        "activate_weighments", "deactivate_weighments",
        "reset_tare_weight", "clear_weights"
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'vehicle_entry', 'vehicle_entry__vehicle', 'vehicle_entry__driver',
            'created_by', 'updated_by'
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
