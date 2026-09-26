"""Admin: for looking, not for working the queue.

The workflow (verify, reject, create in SAP) runs through the API, which keeps
the events and the SAP guards; the admin shows the rows read-only. Documents
are listed without their file links, which would point at ``/media/`` — the
permission-checked download is the only way to open one.
"""

from django.contrib import admin

from .models import (
    CustomerRegistration,
    PartnerAddress,
    RegistrationAttachment,
    RegistrationEvent,
    VendorBankAccount,
    VendorRegistration,
)


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AddressInline(_ReadOnlyInline):
    model = PartnerAddress
    fields = ("address_type", "address_name", "street", "city", "state", "zip_code", "country", "gstin")


class AttachmentInline(_ReadOnlyInline):
    model = RegistrationAttachment
    fields = ("kind", "original_name", "content_type", "size", "uploaded_at", "sent_to_sap_at")


class EventInline(_ReadOnlyInline):
    model = RegistrationEvent
    fields = ("at", "kind", "actor_name", "note")


class BankAccountInline(_ReadOnlyInline):
    model = VendorBankAccount
    fields = ("bank_name", "branch", "account_number", "ifsc", "account_type", "sap_bank_code")


class _RegistrationAdmin(admin.ModelAdmin):
    list_display = ("id", "card_name", "company", "status", "submitted_at", "sap_card_code")
    list_filter = ("company", "status")
    search_fields = ("card_name", "gstin", "pan", "email", "mobile", "card_code", "sap_card_code")
    date_hierarchy = "submitted_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(CustomerRegistration)
class CustomerRegistrationAdmin(_RegistrationAdmin):
    inlines = [AddressInline, AttachmentInline, EventInline]


@admin.register(VendorRegistration)
class VendorRegistrationAdmin(_RegistrationAdmin):
    inlines = [AddressInline, BankAccountInline, AttachmentInline, EventInline]
