import csv
from datetime import datetime, time
from decimal import Decimal

from django.contrib import admin, messages
from django.db.models import ProtectedError
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    BarcodeMaster,
    BarcodeSequence,
    Box,
    BoxMovement,
    BoxStatus,
    DispatchSapSyncLog,
    DispatchScanLog,
    DispatchScanResult,
    DispatchScannedUnit,
    DispatchScannedUnitStatus,
    DispatchSession,
    DispatchSessionLine,
    DispatchSessionStatus,
    DispatchSettings,
    BarcodeAuditLog,
    IntercompanyTransfer,
    IntercompanyTransferLine,
    LabelPrintLog,
    LooseStock,
    LooseStockConsumption,
    Pallet,
    PalletBoxHistory,
    PalletMovement,
    PalletStatus,
    ScanLog,
    ScanResult,
)


def _user_label(user):
    if not user:
        return "-"
    full_name = getattr(user, "full_name", "") or user.get_full_name()
    return full_name or user.get_username()


def _decimal_to_float(value):
    if isinstance(value, Decimal):
        return float(value)
    return value or 0


def export_as_csv(modeladmin, request, queryset):
    """Generic CSV export for the visible admin queryset."""
    meta = modeladmin.model._meta
    field_names = [
        field.name
        for field in meta.fields
        if field.get_internal_type() not in {"BinaryField"}
    ]
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="{meta.app_label}_{meta.model_name}_export.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(field_names)
    for obj in queryset:
        writer.writerow([getattr(obj, field_name) for field_name in field_names])
    return response


export_as_csv.short_description = "Export selected rows to CSV"


class WarehouseListFilter(admin.SimpleListFilter):
    title = "warehouse"
    parameter_name = "warehouse"

    def lookups(self, request, model_admin):
        values = set()
        model = model_admin.model
        for field_name in ("current_warehouse", "warehouse_code"):
            if hasattr(model, field_name):
                values.update(
                    model.objects.exclude(**{field_name: ""})
                    .values_list(field_name, flat=True)
                    .distinct()
                )
        return [(value, value) for value in sorted(values)]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        model = queryset.model
        if hasattr(model, "current_warehouse"):
            return queryset.filter(current_warehouse=value)
        if hasattr(model, "warehouse_code"):
            return queryset.filter(warehouse_code=value)
        return queryset


class BarcodeSystemFilter(admin.SimpleListFilter):
    title = "barcode system"
    parameter_name = "barcode_system"

    def lookups(self, request, model_admin):
        return (
            ("new", "New lightweight format"),
            ("legacy", "Legacy JSON payload"),
            ("unknown", "Unknown / manual"),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        if value == "new":
            return queryset.filter(barcode_data__has_key="barcode")
        if value == "legacy":
            return queryset.filter(
                Q(barcode_data__has_key="box_barcode")
                | Q(barcode_data__has_key="pallet_id")
                | Q(barcode_data__has_key="type")
            )
        return queryset.filter(Q(barcode_data={}) | Q(barcode_data__isnull=True))


class DispatchConflictFilter(admin.SimpleListFilter):
    title = "conflict type"
    parameter_name = "conflict"

    def lookups(self, request, model_admin):
        return (
            ("duplicate", "Duplicate / already used"),
            ("wrong_item", "Wrong item or batch"),
            ("wrong_warehouse", "Wrong warehouse"),
            ("invalid", "Invalid barcode"),
            ("not_found", "No master data"),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "duplicate":
            return queryset.filter(
                Q(reject_code__icontains="DUPLICATE")
                | Q(reject_code__icontains="ALREADY")
                | Q(reject_message__icontains="already")
            )
        if value == "wrong_item":
            return queryset.filter(
                Q(reject_code__icontains="ITEM")
                | Q(reject_code__icontains="BATCH")
                | Q(reject_message__icontains="item")
                | Q(reject_message__icontains="batch")
            )
        if value == "wrong_warehouse":
            return queryset.filter(
                Q(reject_code__icontains="WAREHOUSE")
                | Q(reject_message__icontains="warehouse")
            )
        if value == "invalid":
            return queryset.filter(
                Q(reject_code__icontains="INVALID")
                | Q(reject_message__icontains="invalid")
            )
        if value == "not_found":
            return queryset.filter(
                Q(reject_code__icontains="NOT_FOUND")
                | Q(reject_message__icontains="not found")
                | Q(reject_message__icontains="master")
            )
        return queryset


class BoxInline(admin.TabularInline):
    model = Box
    extra = 0
    fields = ["box_barcode", "item_code", "qty", "status", "current_warehouse"]
    readonly_fields = ["box_barcode", "item_code", "qty", "status", "current_warehouse"]
    can_delete = False
    show_change_link = True


class DispatchSessionLineInline(admin.TabularInline):
    model = DispatchSessionLine
    extra = 0
    fields = [
        "sequence_no",
        "material_code",
        "batch_number",
        "warehouse_code",
        "bill_qty",
        "scanned_qty",
        "status",
    ]
    readonly_fields = fields
    can_delete = False
    show_change_link = True


class DispatchScannedUnitInline(admin.TabularInline):
    model = DispatchScannedUnit
    extra = 0
    fields = [
        "barcode_value",
        "entity_type",
        "material_code",
        "batch_number",
        "dispatch_qty",
        "remaining_qty",
        "scan_status",
        "created_at",
    ]
    readonly_fields = fields
    can_delete = False
    show_change_link = True


class IntercompanyTransferLineInline(admin.TabularInline):
    model = IntercompanyTransferLine
    extra = 0
    fields = ["barcode", "item_code", "batch_number", "qty", "uom", "from_company", "to_company"]
    readonly_fields = fields
    can_delete = False
    show_change_link = True


@admin.register(IntercompanyTransfer)
class IntercompanyTransferAdmin(admin.ModelAdmin):
    list_display = [
        "transfer_number",
        "entity_type",
        "source_company",
        "destination_company",
        "status",
        "total_barcodes",
        "total_qty",
        "sap_status",
        "created_by",
        "created_at",
    ]
    list_filter = ["entity_type", "status", "source_company", "destination_company", "sap_enabled", "created_at"]
    search_fields = [
        "transfer_number",
        "lines__barcode",
        "lines__item_code",
        "lines__batch_number",
        "source_company__code",
        "destination_company__code",
    ]
    readonly_fields = ["created_at", "updated_at", "reversed_at"]
    inlines = [IntercompanyTransferLineInline]
    actions = [export_as_csv]


@admin.register(BarcodeAuditLog)
class BarcodeAuditLogAdmin(admin.ModelAdmin):
    list_display = [
        "barcode",
        "transaction_type",
        "from_company",
        "to_company",
        "transfer",
        "user",
        "created_at",
    ]
    list_filter = ["transaction_type", "from_company", "to_company", "created_at"]
    search_fields = ["barcode", "transfer__transfer_number", "box__box_barcode", "user__email"]
    readonly_fields = ["created_at"]
    actions = [export_as_csv]


@admin.register(BarcodeMaster)
class BarcodeMasterAdmin(admin.ModelAdmin):
    change_list_template = "admin/barcode/barcodemaster/change_list.html"
    list_display = [
        "barcode",
        "barcode_type",
        "material_code",
        "quantity",
        "uom",
        "linked_entity",
        "is_active",
        "updated_at",
    ]
    list_filter = ["barcode_type", "is_active", "created_at", "updated_at"]
    search_fields = [
        "barcode",
        "material_code",
        "box__box_barcode",
        "pallet__pallet_id",
        "company__code",
        "company__name",
    ]
    autocomplete_fields = ["company", "pallet", "box"]
    readonly_fields = ["created_at", "updated_at", "linked_entity"]
    actions = [export_as_csv, "activate_records", "deactivate_records"]
    list_per_page = 50

    fieldsets = (
        ("Barcode", {"fields": ("company", "barcode", "barcode_type", "is_active")}),
        ("Material", {"fields": ("material_code", "quantity", "uom")}),
        ("Linked Records", {"fields": ("pallet", "box", "linked_entity")}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "dashboard/",
                self.admin_site.admin_view(self.dashboard_view),
                name="barcode_barcodemaster_dashboard",
            ),
        ]
        return custom_urls + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["dashboard_url"] = reverse(
            "admin:barcode_barcodemaster_dashboard"
        )
        return super().changelist_view(request, extra_context=extra_context)

    def dashboard_view(self, request):
        today = timezone.localdate()
        start_of_day = timezone.make_aware(
            datetime.combine(today, time.min)
        )
        total_boxes = Box.objects.count()
        total_pallets = Pallet.objects.count()
        total_generated = total_boxes + total_pallets
        total_scan_logs = ScanLog.objects.count()
        total_dispatch_scans = DispatchScanLog.objects.count()
        rejected_dispatch_scans = DispatchScanLog.objects.filter(
            result=DispatchScanResult.REJECTED
        ).count()
        failed_scan_logs = ScanLog.objects.exclude(
            scan_result=ScanResult.SUCCESS
        ).count()
        pending_dispatch_count = DispatchSession.objects.filter(
            status__in=[
                DispatchSessionStatus.DRAFT,
                DispatchSessionStatus.ACTIVE,
                DispatchSessionStatus.PARTIAL,
                DispatchSessionStatus.READY_TO_DISPATCH,
            ]
        ).count()
        active_box_count = Box.objects.filter(status=BoxStatus.ACTIVE).count()
        dispatched_box_count = Box.objects.filter(status=BoxStatus.DISPATCHED).count()
        new_box_count = Box.objects.filter(barcode_data__has_key="barcode").count()
        legacy_box_count = Box.objects.filter(barcode_data__has_key="type").count()

        box_status = list(
            Box.objects.values("status").annotate(count=Count("id")).order_by("status")
        )
        pallet_status = list(
            Pallet.objects.values("status").annotate(count=Count("id")).order_by("status")
        )
        warehouse_rows = list(
            Box.objects.values("current_warehouse")
            .annotate(count=Count("id"), qty=Sum("qty"))
            .order_by("-count")[:10]
        )
        item_rows = list(
            Box.objects.values("item_code", "item_name")
            .annotate(count=Count("id"), qty=Sum("qty"))
            .order_by("-count")[:10]
        )
        recent_activity = list(
            ScanLog.objects.select_related("scanned_by")
            .order_by("-scanned_at")[:8]
        )
        recent_dispatch_conflicts = list(
            DispatchScanLog.objects.select_related("session", "line", "scanned_by")
            .filter(result=DispatchScanResult.REJECTED)
            .order_by("-scanned_at")[:8]
        )
        for row in box_status:
            row["percent"] = round((row["count"] / total_boxes) * 100) if total_boxes else 0
        for row in pallet_status:
            row["percent"] = round((row["count"] / total_pallets) * 100) if total_pallets else 0

        total_conflicts = rejected_dispatch_scans + failed_scan_logs
        total_scans = total_scan_logs + total_dispatch_scans
        success_rate = round(((total_scans - total_conflicts) / total_scans) * 100) if total_scans else 100

        context = {
            **self.admin_site.each_context(request),
            "title": "Barcode Management Dashboard",
            "opts": self.model._meta,
            "today": today,
            "success_rate": success_rate,
            "kpis": [
                {
                    "label": "Generated Barcodes",
                    "value": total_generated,
                    "hint": "Boxes + pallets",
                    "tone": "blue",
                },
                {
                    "label": "Scanned Barcodes",
                    "value": total_scans,
                    "hint": "All scan logs",
                    "tone": "green",
                },
                {
                    "label": "Generated Today",
                    "value": Box.objects.filter(created_at__gte=start_of_day).count()
                    + Pallet.objects.filter(created_at__gte=start_of_day).count(),
                    "hint": today.strftime("%d %b %Y"),
                    "tone": "ink",
                },
                {
                    "label": "Scanned Today",
                    "value": ScanLog.objects.filter(scanned_at__gte=start_of_day).count()
                    + DispatchScanLog.objects.filter(scanned_at__gte=start_of_day).count(),
                    "hint": today.strftime("%d %b %Y"),
                    "tone": "green",
                },
                {
                    "label": "Failed / Rejected Scans",
                    "value": total_conflicts,
                    "hint": "Needs review",
                    "tone": "red",
                },
                {
                    "label": "Duplicate Alerts",
                    "value": DispatchScanLog.objects.filter(
                        Q(reject_code__icontains="DUPLICATE")
                        | Q(reject_message__icontains="already")
                    ).count(),
                    "hint": "Duplicate or already used",
                    "tone": "amber",
                },
                {
                    "label": "Pending Dispatch",
                    "value": pending_dispatch_count,
                    "hint": "Open barcode actions",
                    "tone": "blue",
                },
                {
                    "label": "Old vs New",
                    "value": (
                        f"{new_box_count} / "
                        f"{legacy_box_count}"
                    ),
                    "hint": "New / legacy boxes",
                    "tone": "ink",
                },
            ],
            "summary_cards": [
                {
                    "label": "Active boxes",
                    "value": active_box_count,
                    "percent": round((active_box_count / total_boxes) * 100) if total_boxes else 0,
                },
                {
                    "label": "Dispatched boxes",
                    "value": dispatched_box_count,
                    "percent": round((dispatched_box_count / total_boxes) * 100) if total_boxes else 0,
                },
                {
                    "label": "Pallet labels",
                    "value": total_pallets,
                    "percent": round((total_pallets / total_generated) * 100) if total_generated else 0,
                },
                {
                    "label": "Healthy scans",
                    "value": f"{success_rate}%",
                    "percent": success_rate,
                },
            ],
            "box_status": box_status,
            "pallet_status": pallet_status,
            "warehouse_rows": [
                {
                    "warehouse": row["current_warehouse"] or "Unassigned",
                    "count": row["count"],
                    "qty": _decimal_to_float(row["qty"]),
                    "percent": round((row["count"] / total_boxes) * 100) if total_boxes else 0,
                }
                for row in warehouse_rows
            ],
            "item_rows": [
                {
                    "item_code": row["item_code"],
                    "item_name": row["item_name"],
                    "count": row["count"],
                    "qty": _decimal_to_float(row["qty"]),
                    "percent": round((row["count"] / total_boxes) * 100) if total_boxes else 0,
                }
                for row in item_rows
            ],
            "recent_activity": recent_activity,
            "recent_dispatch_conflicts": recent_dispatch_conflicts,
            "links": {
                "masters": reverse("admin:barcode_barcodemaster_changelist"),
                "boxes": reverse("admin:barcode_box_changelist"),
                "pallets": reverse("admin:barcode_pallet_changelist"),
                "scans": reverse("admin:barcode_scanlog_changelist"),
                "dispatch_scans": reverse("admin:barcode_dispatchscanlog_changelist"),
                "dispatch_sessions": reverse("admin:barcode_dispatchsession_changelist"),
                "settings": reverse("admin:barcode_dispatchsettings_changelist"),
            },
        }
        return TemplateResponse(request, "admin/barcode/dashboard.html", context)

    @admin.display(description="Linked entity")
    def linked_entity(self, obj):
        if obj.box_id:
            url = reverse("admin:barcode_box_change", args=[obj.box_id])
            return format_html('<a href="{}">{}</a>', url, obj.box.box_barcode)
        if obj.pallet_id:
            url = reverse("admin:barcode_pallet_change", args=[obj.pallet_id])
            return format_html('<a href="{}">{}</a>', url, obj.pallet.pallet_id)
        return "-"

    @admin.action(description="Activate selected barcode records")
    def activate_records(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="Deactivate selected barcode records")
    def deactivate_records(self, request, queryset):
        queryset.update(is_active=False)


@admin.register(Pallet)
class PalletAdmin(admin.ModelAdmin):
    list_display = [
        "pallet_id",
        "item_code",
        "batch_number",
        "box_count",
        "available_boxes",
        "dispatched_boxes",
        "total_qty",
        "current_warehouse",
        "status_badge",
        "created_at",
    ]
    list_filter = ["status", WarehouseListFilter, BarcodeSystemFilter, "created_at"]
    search_fields = ["pallet_id", "item_code", "item_name", "batch_number"]
    autocomplete_fields = ["company", "production_run", "dispatch_session", "created_by"]
    readonly_fields = [
        "barcode_data",
        "created_at",
        "updated_at",
        "traceability_panel",
    ]
    actions = [export_as_csv, "mark_void", "delete_empty_pallets"]
    inlines = [BoxInline]
    list_per_page = 50

    fieldsets = (
        ("Identity", {"fields": ("company", "pallet_id", "barcode_data", "status")}),
        (
            "Material",
            {
                "fields": (
                    "item_code",
                    "item_name",
                    "batch_number",
                    "total_qty",
                    "uom",
                    "mfg_date",
                    "exp_date",
                )
            },
        ),
        (
            "Warehouse",
            {"fields": ("current_warehouse", "current_bin", "production_line")},
        ),
        (
            "Counts",
            {
                "fields": (
                    "box_count",
                    "total_boxes",
                    "available_boxes",
                    "dispatched_boxes",
                    "max_box_count",
                )
            },
        ),
        (
            "Dispatch",
            {"fields": ("dispatch_session", "dispatched_at")},
        ),
        ("Traceability", {"fields": ("traceability_panel",)}),
        ("Audit", {"fields": ("created_by", "created_at", "updated_at")}),
    )

    def _pallet_delete_blocker(self, obj):
        if not obj:
            return ""
        if obj.boxes.exists():
            return "This pallet has boxes attached."
        if obj.dispatch_session_id or obj.dispatched_at:
            return "This pallet is linked with a dispatch session."
        if obj.dispatch_scanned_units.exists():
            return "This pallet has dispatch scan history."
        return ""

    def has_delete_permission(self, request, obj=None):
        has_permission = super().has_delete_permission(request, obj=obj)
        if not has_permission or obj is None:
            return has_permission
        return not self._pallet_delete_blocker(obj)

    def delete_model(self, request, obj):
        blocker = self._pallet_delete_blocker(obj)
        if blocker:
            self.message_user(
                request,
                f"{obj.pallet_id} was not deleted. {blocker}",
                level=messages.ERROR,
            )
            return
        try:
            super().delete_model(request, obj)
        except ProtectedError as exc:
            self.message_user(
                request,
                f"{obj.pallet_id} could not be deleted because it is used by another barcode record: {exc}",
                level=messages.ERROR,
            )

    def delete_queryset(self, request, queryset):
        deleted_count = 0
        blocked = []
        for pallet in queryset:
            blocker = self._pallet_delete_blocker(pallet)
            if blocker:
                blocked.append(f"{pallet.pallet_id}: {blocker}")
                continue
            try:
                pallet.delete()
                deleted_count += 1
            except ProtectedError as exc:
                blocked.append(f"{pallet.pallet_id}: protected by related data ({exc})")

        if deleted_count:
            self.message_user(
                request,
                f"Deleted {deleted_count} empty pallet(s).",
                level=messages.SUCCESS,
            )
        if blocked:
            self.message_user(
                request,
                "Some pallets were not deleted. " + " ".join(blocked[:5]),
                level=messages.WARNING,
            )

    @admin.display(description="Status")
    def status_badge(self, obj):
        color = {
            PalletStatus.ACTIVE: "#198754",
            PalletStatus.PARTIAL: "#fd7e14",
            PalletStatus.DISPATCHED: "#0d6efd",
            PalletStatus.VOID: "#dc3545",
        }.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Traceability")
    def traceability_panel(self, obj):
        if not obj.pk:
            return "-"
        movement_url = (
            reverse("admin:barcode_palletmovement_changelist")
            + f"?pallet__id__exact={obj.pk}"
        )
        box_url = reverse("admin:barcode_box_changelist") + f"?pallet__id__exact={obj.pk}"
        scan_url = (
            reverse("admin:barcode_dispatchscanlog_changelist")
            + f"?entity_type=PALLET&entity_id={obj.pallet_id}"
        )
        return format_html(
            '<div class="help">'
            '<a class="button" href="{}">Movements</a> '
            '<a class="button" href="{}">Boxes</a> '
            '<a class="button" href="{}">Dispatch scans</a>'
            "</div>",
            movement_url,
            box_url,
            scan_url,
        )

    @admin.action(description="Mark selected pallets as void")
    def mark_void(self, request, queryset):
        queryset.update(status=PalletStatus.VOID)

    @admin.action(description="Delete selected empty pallets")
    def delete_empty_pallets(self, request, queryset):
        self.delete_queryset(request, queryset)


@admin.register(Box)
class BoxAdmin(admin.ModelAdmin):
    list_display = [
        "box_barcode",
        "item_code",
        "batch_number",
        "qty",
        "uom",
        "pallet_link",
        "current_warehouse",
        "status_badge",
        "created_at",
    ]
    list_filter = ["status", WarehouseListFilter, BarcodeSystemFilter, "created_at"]
    search_fields = [
        "box_barcode",
        "item_code",
        "item_name",
        "batch_number",
        "pallet__pallet_id",
    ]
    autocomplete_fields = ["company", "pallet", "production_run", "dispatch_session", "created_by"]
    readonly_fields = [
        "barcode_data",
        "created_at",
        "updated_at",
        "traceability_panel",
    ]
    actions = [export_as_csv, "mark_void", "mark_active"]
    list_per_page = 50

    fieldsets = (
        ("Identity", {"fields": ("company", "box_barcode", "barcode_data", "status")}),
        (
            "Material",
            {
                "fields": (
                    "item_code",
                    "item_name",
                    "batch_number",
                    "qty",
                    "uom",
                    "g_weight",
                    "n_weight",
                    "mfg_date",
                    "exp_date",
                )
            },
        ),
        ("Warehouse", {"fields": ("current_warehouse", "current_bin", "production_line")}),
        ("Pallet", {"fields": ("pallet", "removed_from_pallet_at", "removed_from_pallet_reason")}),
        ("Dispatch", {"fields": ("dispatch_session", "dispatched_at")}),
        ("Traceability", {"fields": ("traceability_panel",)}),
        ("Audit", {"fields": ("created_by", "created_at", "updated_at")}),
    )

    @admin.display(description="Pallet")
    def pallet_link(self, obj):
        if not obj.pallet_id:
            return "-"
        url = reverse("admin:barcode_pallet_change", args=[obj.pallet_id])
        return format_html('<a href="{}">{}</a>', url, obj.pallet.pallet_id)

    @admin.display(description="Status")
    def status_badge(self, obj):
        color = {
            BoxStatus.ACTIVE: "#198754",
            BoxStatus.PARTIAL: "#fd7e14",
            BoxStatus.DISPATCHED: "#0d6efd",
            BoxStatus.VOID: "#dc3545",
        }.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Traceability")
    def traceability_panel(self, obj):
        if not obj.pk:
            return "-"
        movement_url = (
            reverse("admin:barcode_boxmovement_changelist")
            + f"?box__id__exact={obj.pk}"
        )
        print_url = (
            reverse("admin:barcode_labelprintlog_changelist")
            + f"?q={obj.box_barcode}"
        )
        scan_url = (
            reverse("admin:barcode_scanlog_changelist")
            + f"?q={obj.box_barcode}"
        )
        dispatch_scan_url = (
            reverse("admin:barcode_dispatchscanlog_changelist")
            + f"?q={obj.box_barcode}"
        )
        return format_html(
            '<div class="help">'
            '<a class="button" href="{}">Movements</a> '
            '<a class="button" href="{}">Prints</a> '
            '<a class="button" href="{}">Scans</a> '
            '<a class="button" href="{}">Dispatch scans</a>'
            "</div>",
            movement_url,
            print_url,
            scan_url,
            dispatch_scan_url,
        )

    @admin.action(description="Mark selected boxes as void")
    def mark_void(self, request, queryset):
        queryset.update(status=BoxStatus.VOID)

    @admin.action(description="Mark selected boxes as active")
    def mark_active(self, request, queryset):
        from barcode.services.pallet_state import recalculate_pallet_state

        boxes = list(queryset.select_related("pallet", "company"))
        queryset.update(status=BoxStatus.ACTIVE)
        # Re-activating a box changes its pallet's derived status/qty -- recompute
        # each affected pallet so it can't be left stale (e.g. stuck DISPATCHED
        # with a live box), the way a bare .update() would.
        seen = set()
        for box in boxes:
            if box.pallet_id and box.pallet_id not in seen:
                seen.add(box.pallet_id)
                recalculate_pallet_state(box.company, box.pallet)


@admin.register(LabelPrintLog)
class LabelPrintLogAdmin(admin.ModelAdmin):
    list_display = [
        "label_type",
        "reference_code",
        "print_type",
        "printed_by_label",
        "printer_name",
        "printed_at",
    ]
    list_filter = ["label_type", "print_type", "printed_at"]
    search_fields = ["reference_code", "printer_name", "printed_by__username"]
    autocomplete_fields = ["company", "printed_by"]
    readonly_fields = ["printed_at"]
    actions = [export_as_csv]
    list_per_page = 50

    @admin.display(description="Printed by")
    def printed_by_label(self, obj):
        return _user_label(obj.printed_by)


@admin.register(PalletMovement)
class PalletMovementAdmin(admin.ModelAdmin):
    list_display = [
        "pallet",
        "movement_type",
        "from_warehouse",
        "to_warehouse",
        "quantity",
        "performed_by_label",
        "performed_at",
    ]
    list_filter = ["movement_type", "from_warehouse", "to_warehouse", "performed_at"]
    search_fields = ["pallet__pallet_id", "notes"]
    autocomplete_fields = ["company", "pallet", "performed_by"]
    readonly_fields = ["performed_at"]
    actions = [export_as_csv]

    @admin.display(description="Performed by")
    def performed_by_label(self, obj):
        return _user_label(obj.performed_by)


@admin.register(BoxMovement)
class BoxMovementAdmin(admin.ModelAdmin):
    list_display = [
        "box",
        "movement_type",
        "from_warehouse",
        "to_warehouse",
        "from_pallet",
        "to_pallet",
        "performed_by_label",
        "performed_at",
    ]
    list_filter = ["movement_type", "from_warehouse", "to_warehouse", "performed_at"]
    search_fields = ["box__box_barcode", "from_pallet__pallet_id", "to_pallet__pallet_id"]
    autocomplete_fields = ["company", "box", "from_pallet", "to_pallet", "performed_by"]
    readonly_fields = ["performed_at"]
    actions = [export_as_csv]

    @admin.display(description="Performed by")
    def performed_by_label(self, obj):
        return _user_label(obj.performed_by)


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin):
    list_display = [
        "scan_type",
        "barcode_raw",
        "entity_type",
        "entity_id",
        "scan_result_badge",
        "scanned_by_label",
        "device_info",
        "scanned_at",
    ]
    list_filter = ["scan_type", "entity_type", "scan_result", "scanned_at"]
    search_fields = ["barcode_raw", "entity_id", "device_info", "scanned_by__username"]
    autocomplete_fields = ["company", "scanned_by"]
    readonly_fields = ["scanned_at", "barcode_parsed"]
    actions = [export_as_csv]
    list_per_page = 50

    @admin.display(description="Result")
    def scan_result_badge(self, obj):
        color = "#198754" if obj.scan_result == ScanResult.SUCCESS else "#dc3545"
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_scan_result_display(),
        )

    @admin.display(description="Scanned by")
    def scanned_by_label(self, obj):
        return _user_label(obj.scanned_by)


@admin.register(LooseStock)
class LooseStockAdmin(admin.ModelAdmin):
    list_display = [
        "item_code",
        "batch_number",
        "qty",
        "original_qty",
        "reason",
        "source_box",
        "status",
        "current_warehouse",
        "created_at",
    ]
    list_filter = ["status", "reason", "current_warehouse", "created_at"]
    search_fields = ["item_code", "item_name", "batch_number", "source_box__box_barcode"]
    autocomplete_fields = [
        "company",
        "source_box",
        "source_pallet",
        "repacked_into_box",
        "created_by",
    ]
    readonly_fields = ["created_at", "updated_at"]
    actions = [export_as_csv]


@admin.register(LooseStockConsumption)
class LooseStockConsumptionAdmin(admin.ModelAdmin):
    list_display = ["loose_stock", "box", "qty", "created_by", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["loose_stock__item_code", "box__box_barcode"]
    autocomplete_fields = ["company", "loose_stock", "box", "created_by"]
    readonly_fields = ["created_at"]
    actions = [export_as_csv]


@admin.register(BarcodeSequence)
class BarcodeSequenceAdmin(admin.ModelAdmin):
    list_display = ["company", "sequence_type", "date_str", "line_key", "next_value", "updated_at"]
    list_filter = ["sequence_type", "date_str", "line_key", "updated_at"]
    search_fields = ["company__code", "sequence_type", "date_str", "line_key"]
    autocomplete_fields = ["company"]
    readonly_fields = ["updated_at"]


@admin.register(PalletBoxHistory)
class PalletBoxHistoryAdmin(admin.ModelAdmin):
    list_display = [
        "action",
        "pallet",
        "box",
        "old_status",
        "new_status",
        "dispatch_session",
        "created_by_label",
        "created_at",
    ]
    list_filter = ["action", "old_status", "new_status", "created_at"]
    search_fields = ["pallet__pallet_id", "box__box_barcode", "remarks"]
    autocomplete_fields = ["company", "pallet", "box", "dispatch_session", "created_by"]
    readonly_fields = ["created_at"]
    actions = [export_as_csv]

    @admin.display(description="Created by")
    def created_by_label(self, obj):
        return _user_label(obj.created_by)


@admin.register(DispatchSettings)
class DispatchSettingsAdmin(admin.ModelAdmin):
    list_display = [
        "company",
        "allow_partial_dispatch",
        "allow_partial_pallet_dispatch",
        "allow_box_dispatch_from_pallet",
        "require_sequential_item_scanning",
        "require_sap_sync_on_completion",
        "updated_at",
    ]
    autocomplete_fields = ["company"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = (
        ("Company", {"fields": ("company",)}),
        (
            "Scan Validation",
            {
                "fields": (
                    "allow_partial_dispatch",
                    "allow_partial_pallet_dispatch",
                    "allow_box_dispatch_from_pallet",
                    "require_sequential_item_scanning",
                    "allow_admin_override",
                )
            },
        ),
        (
            "SAP / Closure",
            {
                "fields": (
                    "require_sap_sync_on_completion",
                    "allow_manual_close",
                )
            },
        ),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(DispatchSession)
class DispatchSessionAdmin(admin.ModelAdmin):
    list_display = [
        "bill_number",
        "customer_name",
        "status_badge",
        "total_expected_qty",
        "total_scanned_qty",
        "sap_update_status",
        "created_by_label",
        "updated_at",
    ]
    list_filter = ["status", "sap_update_status", "sap_system_type", "created_at", "updated_at"]
    search_fields = [
        "bill_number",
        "sap_doc_entry",
        "sap_doc_num",
        "delivery_number",
        "customer_code",
        "customer_name",
    ]
    autocomplete_fields = [
        "company",
        "dispatched_by",
        "closed_by",
        "cancelled_by",
        "created_by",
        "updated_by",
    ]
    readonly_fields = ["created_at", "updated_at", "started_at", "completed_at", "dispatched_at"]
    inlines = [DispatchSessionLineInline, DispatchScannedUnitInline]
    actions = [export_as_csv]
    list_per_page = 50

    fieldsets = (
        ("Session", {"fields": ("company", "bill_number", "status")}),
        (
            "SAP Document",
            {
                "fields": (
                    "sap_system_type",
                    "sap_object_type",
                    "sap_doc_entry",
                    "sap_doc_num",
                    "delivery_number",
                    "reference_delivery_number",
                    "sap_dispatch_status",
                    "sap_update_status",
                    "sap_update_error",
                )
            },
        ),
        (
            "Customer",
            {
                "fields": (
                    "customer_code",
                    "customer_name",
                    "ship_to_code",
                    "ship_to_name",
                    "bill_date",
                )
            },
        ),
        ("Quantities", {"fields": ("total_expected_qty", "total_scanned_qty")}),
        (
            "Lifecycle",
            {
                "fields": (
                    "started_at",
                    "completed_at",
                    "dispatched_at",
                    "dispatched_by",
                    "closed_at",
                    "closed_by",
                    "close_reason",
                    "cancelled_at",
                    "cancelled_by",
                    "cancel_reason",
                )
            },
        ),
        ("Snapshot", {"classes": ("collapse",), "fields": ("sap_snapshot",)}),
        ("Audit", {"fields": ("created_by", "updated_by", "created_at", "updated_at")}),
    )

    @admin.display(description="Status")
    def status_badge(self, obj):
        color = {
            DispatchSessionStatus.DRAFT: "#6c757d",
            DispatchSessionStatus.ACTIVE: "#0d6efd",
            DispatchSessionStatus.PARTIAL: "#fd7e14",
            DispatchSessionStatus.READY_TO_DISPATCH: "#20c997",
            DispatchSessionStatus.COMPLETED: "#198754",
            DispatchSessionStatus.SAP_SYNC_FAILED: "#dc3545",
            DispatchSessionStatus.CANCELLED: "#6f42c1",
        }.get(obj.status, "#6c757d")
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_status_display(),
        )

    @admin.display(description="Created by")
    def created_by_label(self, obj):
        return _user_label(obj.created_by)


@admin.register(DispatchSessionLine)
class DispatchSessionLineAdmin(admin.ModelAdmin):
    list_display = [
        "session",
        "sequence_no",
        "material_code",
        "batch_number",
        "warehouse_code",
        "bill_qty",
        "scanned_qty",
        "status",
    ]
    list_filter = ["status", "warehouse_code", "serial_required", "created_at"]
    search_fields = [
        "session__bill_number",
        "material_code",
        "material_description",
        "batch_number",
        "warehouse_code",
    ]
    autocomplete_fields = ["session"]
    readonly_fields = ["created_at", "updated_at"]
    actions = [export_as_csv]


@admin.register(DispatchScanLog)
class DispatchScanLogAdmin(admin.ModelAdmin):
    list_display = [
        "session",
        "raw_barcode_short",
        "entity_type",
        "material_code",
        "batch_number",
        "qty",
        "result_badge",
        "reject_code",
        "scanned_by_label",
        "scanned_at",
    ]
    list_filter = [
        "result",
        DispatchConflictFilter,
        "entity_type",
        "reject_code",
        "device_id",
        "scanned_at",
    ]
    search_fields = [
        "session__bill_number",
        "raw_barcode",
        "entity_id",
        "material_code",
        "batch_number",
        "reject_code",
        "reject_message",
        "device_id",
    ]
    autocomplete_fields = ["session", "line", "scanned_by"]
    readonly_fields = ["scanned_at", "parsed_barcode", "request_id"]
    actions = [export_as_csv]
    list_per_page = 50

    @admin.display(description="Barcode")
    def raw_barcode_short(self, obj):
        value = obj.raw_barcode or ""
        return value if len(value) <= 40 else f"{value[:37]}..."

    @admin.display(description="Result")
    def result_badge(self, obj):
        color = "#198754" if obj.result == DispatchScanResult.ACCEPTED else "#dc3545"
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_result_display(),
        )

    @admin.display(description="Scanned by")
    def scanned_by_label(self, obj):
        return _user_label(obj.scanned_by)


@admin.register(DispatchScannedUnit)
class DispatchScannedUnitAdmin(admin.ModelAdmin):
    list_display = [
        "barcode_value",
        "session",
        "line",
        "entity_type",
        "material_code",
        "batch_number",
        "dispatch_qty",
        "remaining_qty",
        "scan_status_badge",
        "created_at",
    ]
    list_filter = ["entity_type", "scan_status", "material_code", "batch_number", "created_at"]
    search_fields = [
        "barcode_value",
        "session__bill_number",
        "material_code",
        "batch_number",
        "serial_number",
        "box__box_barcode",
        "pallet__pallet_id",
    ]
    autocomplete_fields = ["session", "line", "scan_log", "box", "pallet"]
    readonly_fields = ["created_at"]
    actions = [export_as_csv]
    list_per_page = 50

    @admin.display(description="Scan status")
    def scan_status_badge(self, obj):
        color = {
            DispatchScannedUnitStatus.ACTIVE: "#0d6efd",
            DispatchScannedUnitStatus.REMOVED: "#fd7e14",
            DispatchScannedUnitStatus.DISPATCHED: "#198754",
        }.get(obj.scan_status, "#6c757d")
        return format_html(
            '<span style="background:{};color:white;border-radius:12px;'
            'padding:3px 9px;font-size:12px;">{}</span>',
            color,
            obj.get_scan_status_display(),
        )


@admin.register(DispatchSapSyncLog)
class DispatchSapSyncLogAdmin(admin.ModelAdmin):
    list_display = ["session", "operation", "status", "attempt_no", "error_short", "created_at"]
    list_filter = ["operation", "status", "created_at"]
    search_fields = ["session__bill_number", "operation", "error_message"]
    autocomplete_fields = ["session"]
    readonly_fields = ["created_at", "request_payload", "response_payload"]
    actions = [export_as_csv]

    @admin.display(description="Error")
    def error_short(self, obj):
        if not obj.error_message:
            return "-"
        return obj.error_message[:80]
