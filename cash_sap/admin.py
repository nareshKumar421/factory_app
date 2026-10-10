"""Django admin for cash book -> SAP. Every SAP field is read-only here.

The admin is for looking, and for fixing settings in an emergency: a document
number typed by hand would bypass the read-back that keeps SAP and JI in step.
"""

from django.contrib import admin

from .models import (
    BunchSapState,
    CashBranchSapRouting,
    GLItemMap,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    VoucherSapInfo,
)

BATCH_SAP_FIELDS = (
    "po_status", "po_doc_entry", "po_doc_num", "po_draft_entry", "po_doc_total", "po_payload",
    "po_error", "po_posted_at", "po_round", "po_close_status", "po_close_error", "simulated",
    "requested_by", "requested_at", "last_sent_by", "last_sent_at", "completion_notified_at",
)
GRPO_SAP_FIELDS = (
    "status", "grpo_doc_entry", "grpo_doc_num", "grpo_draft_entry", "grpo_doc_total", "payload",
    "error", "posted_at", "sap_attachment_entry", "uploaded_files", "line_map", "num_at_card",
    "round", "simulated", "last_sent_by", "last_sent_at",
)
DRAFT_SAP_FIELDS = (
    "status", "sap_draft_entry", "sap_doc_entry", "sap_doc_num", "payload", "error", "posted_at",
    "num_at_card", "round", "simulated", "last_sent_by", "last_sent_at",
)


@admin.register(CashBranchSapRouting)
class CashBranchSapRoutingAdmin(admin.ModelAdmin):
    list_display = ("branch", "enabled", "sap_company_code", "mode", "vendor_code", "bpl_id",
                    "warehouse_code", "ap_drafts_enabled", "grpo_grouping", "cutover_date")
    list_filter = ("enabled", "sap_company_code", "mode")
    raw_id_fields = ("branch",)
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")


@admin.register(GLItemMap)
class GLItemMapAdmin(admin.ModelAdmin):
    list_display = ("sap_company_code", "gl_account_code", "gl_account_name", "treatment",
                    "item_code", "target_account_code", "needs_review", "is_active")
    list_filter = ("sap_company_code", "treatment", "needs_review", "is_active")
    search_fields = ("gl_account_code", "gl_account_name", "item_code")
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ImprestGRPOInline(_ReadOnlyInline):
    model = ImprestGRPO
    fields = ("group_key", "status", "num_at_card", "amount", "bill_date", "posting_date",
              "grpo_doc_num", "error", "simulated")
    readonly_fields = fields


class ImprestVoucherInline(_ReadOnlyInline):
    model = ImprestVoucher
    fk_name = "batch"
    fields = ("entry", "treatment", "item_code", "account_code", "amount", "po_line_num", "grpo",
              "left_out_after_po")
    readonly_fields = fields


class ImprestAPDraftInline(_ReadOnlyInline):
    model = ImprestAPDraft
    fields = ("kind", "status", "num_at_card", "amount", "doc_date", "sap_draft_entry", "sap_doc_num",
              "error", "simulated")
    readonly_fields = fields


@admin.register(ImprestBatch)
class ImprestBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "bunch", "sap_company_code", "mode", "po_status", "po_doc_num",
                    "po_close_status", "simulated", "requested_at")
    list_filter = ("sap_company_code", "po_status", "simulated")
    raw_id_fields = ("bunch",)
    readonly_fields = BATCH_SAP_FIELDS + ("created_by", "updated_by", "created_at", "updated_at")
    inlines = [ImprestGRPOInline, ImprestVoucherInline, ImprestAPDraftInline]


@admin.register(ImprestGRPO)
class ImprestGRPOAdmin(admin.ModelAdmin):
    list_display = ("id", "batch", "group_key", "status", "num_at_card", "amount", "grpo_doc_num", "simulated")
    list_filter = ("status", "simulated", "sap_company_code")
    search_fields = ("num_at_card", "group_key")
    raw_id_fields = ("batch",)
    readonly_fields = GRPO_SAP_FIELDS + ("created_by", "updated_by", "created_at", "updated_at")


@admin.register(ImprestAPDraft)
class ImprestAPDraftAdmin(admin.ModelAdmin):
    list_display = ("id", "batch", "kind", "status", "num_at_card", "amount", "sap_draft_entry", "simulated")
    list_filter = ("kind", "status", "simulated")
    raw_id_fields = ("batch",)
    readonly_fields = DRAFT_SAP_FIELDS + ("created_by", "updated_by", "created_at", "updated_at")


@admin.register(VoucherSapInfo)
class VoucherSapInfoAdmin(admin.ModelAdmin):
    list_display = ("entry", "bill_date", "gate_entry_no", "supplier_bill_no", "updated_by", "updated_at")
    raw_id_fields = ("entry",)
    readonly_fields = ("updated_by", "updated_at")


@admin.register(BunchSapState)
class BunchSapStateAdmin(admin.ModelAdmin):
    list_display = ("bunch", "status", "frozen_at", "frozen_by", "checked_at")
    list_filter = ("status",)
    raw_id_fields = ("bunch",)
    readonly_fields = ("frozen_at", "frozen_by", "member_snapshot", "drift", "checked_at",
                       "changes_accepted_by", "changes_accepted_at", "changes_accepted_note", "updated_at")
