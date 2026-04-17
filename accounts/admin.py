# accounts/admin.py
"""
Enhanced User Admin Configuration for Factory Jivo Wellness
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from django.utils import timezone
from .models import User
from .models import Department

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Enhanced User Admin with comprehensive features
    """
    model = User

    # List display configuration
    list_display = (
        "email",
        "full_name",
        "employee_code",
        "department",
        "is_active",
        "is_staff_display",
        "is_superuser_display",
        "date_joined",
        "last_login_display",
    )

    list_display_links = ("email", "full_name")

    list_filter = (
        "is_active",
        "is_staff",
        "is_superuser",
        "department",
        "date_joined",
        "last_login",
    )

    ordering = ("-date_joined",)
    search_fields = ("email", "full_name", "employee_code")
    date_hierarchy = "date_joined"

    # List editable for quick updates
    list_editable = ("is_active",)

    # List per page
    list_per_page = 25
    list_max_show_all = 200

    # Fieldsets for detail view
    fieldsets = (
        (None, {
            "fields": ("email", "password"),
            "description": "Primary login credentials"
        }),
        ("Personal Information", {
            "fields": ("full_name", "employee_code", "department"),
            "classes": ("wide",),
        }),
        ("Permissions", {
            "fields": (
                "is_active",
                "is_staff",
                "is_superuser",
                "groups",
                "user_permissions",
            ),
            "classes": ("collapse",),
            "description": "Control user access levels and permissions"
        }),
        ("Important Dates", {
            "fields": ("last_login", "date_joined"),
            "classes": ("collapse",),
        }),
    )

    # Add user fieldsets
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "email",
                "full_name",
                "employee_code",
                "password1",
                "password2",
            ),
            "description": "Create a new user account"
        }),
        ("Permissions", {
            "classes": ("wide",),
            "fields": (
                "is_active",
                "is_staff",
                "is_superuser",
            ),
        }),
    )

    # Read-only fields
    readonly_fields = ("date_joined", "last_login")

    # Filter horizontal for better UX
    filter_horizontal = ("groups", "user_permissions")

    # Custom display methods with colored badges
    @admin.display(description="Staff", boolean=True, ordering="is_staff")
    def is_staff_display(self, obj):
        return obj.is_staff

    @admin.display(description="Superuser", boolean=True, ordering="is_superuser")
    def is_superuser_display(self, obj):
        return obj.is_superuser

    @admin.display(description="Last Login", ordering="last_login")
    def last_login_display(self, obj):
        if obj.last_login:
            return obj.last_login.strftime("%Y-%m-%d %H:%M")
        return format_html('<span style="color: #999;">{}</span>', "Never")

    # Admin actions
    @admin.action(description="Activate selected users")
    def activate_users(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} user(s) activated successfully.")

    @admin.action(description="Deactivate selected users")
    def deactivate_users(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} user(s) deactivated successfully.")

    @admin.action(description="Grant staff status to selected users")
    def make_staff(self, request, queryset):
        updated = queryset.update(is_staff=True)
        self.message_user(request, f"{updated} user(s) granted staff status.")

    @admin.action(description="Remove staff status from selected users")
    def remove_staff(self, request, queryset):
        updated = queryset.update(is_staff=False)
        self.message_user(request, f"{updated} user(s) removed from staff.")

    actions = ["activate_users", "deactivate_users", "make_staff", "remove_staff"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related()

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)

    

admin.site.register(Department)


