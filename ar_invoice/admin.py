from django import forms
from django.contrib import admin

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.customer_reader import HanaCustomerReader

from .models import (
    ARInvoiceAttachment,
    ARInvoiceLine,
    ARInvoicePayment,
    ARInvoicePosting,
    UserCustomer,
)


class ARInvoiceLineInline(admin.TabularInline):
    model = ARInvoiceLine
    extra = 0


class ARInvoiceAttachmentInline(admin.TabularInline):
    model = ARInvoiceAttachment
    extra = 0


@admin.register(ARInvoicePosting)
class ARInvoicePostingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "company", "customer_code", "customer_name", "status",
        "sap_draft_entry", "sap_doc_num", "created_at",
    )
    list_filter = ("company", "status")
    search_fields = ("customer_code", "customer_name", "customer_ref")
    inlines = [ARInvoiceLineInline, ARInvoiceAttachmentInline]


@admin.register(ARInvoicePayment)
class ARInvoicePaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "company", "sap_doc_num", "sap_doc_entry", "status",
        "received_on", "amount", "mode", "updated_at",
    )
    list_filter = ("company", "status", "mode")
    search_fields = ("sap_doc_num", "sap_doc_entry", "reference", "remarks")
    raw_id_fields = ("ar_invoice",)


class UserCustomerForm(forms.ModelForm):
    """Checks the code against the company's SAP before it is saved.

    A mistyped code would link the user to nobody (or, worse, to somebody
    else), and the ledger would then show "not linked" with no hint why — so
    the code must name a customer in that company's SAP. SAP's own spelling of
    the code is stored, and its name is copied for the lists.
    """

    class Meta:
        model = UserCustomer
        fields = ("user", "company", "customer_code", "is_active")

    def clean(self):
        cleaned = super().clean()
        company = cleaned.get("company")
        code = (cleaned.get("customer_code") or "").strip()
        if not company or not code:
            return cleaned
        try:
            reader = HanaCustomerReader(CompanyContext(company.code))
            customer = reader.get_customer(code)
            if customer is None and code != code.upper():
                # SAP matches codes case-sensitively, and ours are upper case.
                customer = reader.get_customer(code.upper())
        except (SAPConnectionError, SAPDataError) as exc:
            raise forms.ValidationError(f"SAP could not be read to check {code}: {exc}")
        except ValueError as exc:  # no SAP configured for this company
            raise forms.ValidationError(str(exc))
        if customer is None:
            self.add_error("customer_code", f"{code} is not a customer in {company.name}'s SAP.")
            return cleaned
        cleaned["customer_code"] = customer["customer_code"]
        self.instance.customer_name = customer["customer_name"]
        return cleaned


@admin.register(UserCustomer)
class UserCustomerAdmin(admin.ModelAdmin):
    """Which SAP customer each user is — what their Ledger tab may show."""

    form = UserCustomerForm
    list_display = (
        "user", "company", "customer_code", "customer_name", "is_active", "updated_at",
    )
    list_filter = ("company", "is_active")
    search_fields = (
        "user__email", "user__full_name", "user__employee_code",
        "customer_code", "customer_name",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("customer_name", "created_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
