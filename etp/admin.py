"""Django-admin registration for the ETP / STP registers (support / back office)."""

from django.contrib import admin

from .models import (
    BackwashEntry,
    BackwashEquipment,
    CalibrationInstrument,
    CalibrationPoint,
    CalibrationReading,
    CalibrationRecord,
    ChemicalConsumptionLine,
    ChemicalConsumptionLog,
    DailyPlantLog,
    EtpPrintDocument,
    MonitoringParameter,
    MonitoringReading,
    MonitoringRecord,
    MonitoringValue,
    PlantChemical,
    PlantOption,
    PlantStaff,
    RegisterChangeLog,
    SludgeGenerationEntry,
    TreatmentPlant,
)


@admin.register(TreatmentPlant)
class TreatmentPlantAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "plant_type", "location", "sequence", "is_active")
    list_filter = ("plant_type", "is_active", "companies")
    search_fields = ("code", "name", "location")
    filter_horizontal = ("companies",)


@admin.register(PlantStaff)
class PlantStaffAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "employee_code", "sequence", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("name", "employee_code")
    filter_horizontal = ("plants",)


@admin.register(PlantOption)
class PlantOptionAdmin(admin.ModelAdmin):
    list_display = ("category", "label", "sequence", "is_default", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("label",)


@admin.register(PlantChemical)
class PlantChemicalAdmin(admin.ModelAdmin):
    list_display = ("name", "default_uom", "sequence", "is_active")
    list_filter = ("default_uom", "is_active", "plants")
    search_fields = ("name",)
    filter_horizontal = ("plants",)


@admin.register(BackwashEquipment)
class BackwashEquipmentAdmin(admin.ModelAdmin):
    list_display = ("plant", "name", "default_chemical", "sequence", "is_active")
    list_filter = ("plant", "is_active")
    search_fields = ("name", "equipment_code")


@admin.register(MonitoringParameter)
class MonitoringParameterAdmin(admin.ModelAdmin):
    list_display = (
        "plant",
        "stage",
        "parameter_name",
        "unit",
        "specification_text",
        "sequence",
        "is_active",
    )
    list_filter = ("plant", "stage", "is_active")
    search_fields = ("parameter_name", "parameter_key")


@admin.register(DailyPlantLog)
class DailyPlantLogAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "plant",
        "inlet_total",
        "outlet_total",
        "ph_reading",
        "energy_units",
        "operator",
    )
    list_filter = ("plant", "date")
    date_hierarchy = "date"


class MonitoringValueInline(admin.TabularInline):
    model = MonitoringValue
    extra = 0


@admin.register(MonitoringReading)
class MonitoringReadingAdmin(admin.ModelAdmin):
    list_display = ("record", "reading_time", "operator")
    inlines = [MonitoringValueInline]


class MonitoringReadingInline(admin.TabularInline):
    model = MonitoringReading
    extra = 0


@admin.register(MonitoringRecord)
class MonitoringRecordAdmin(admin.ModelAdmin):
    list_display = ("date", "plant", "interval_hours", "chemist", "verified_at")
    list_filter = ("plant", "date")
    date_hierarchy = "date"
    inlines = [MonitoringReadingInline]


class ChemicalConsumptionLineInline(admin.TabularInline):
    model = ChemicalConsumptionLine
    extra = 0


@admin.register(ChemicalConsumptionLog)
class ChemicalConsumptionLogAdmin(admin.ModelAdmin):
    list_display = ("date", "plant", "operator", "verified_by")
    list_filter = ("plant", "date")
    date_hierarchy = "date"
    inlines = [ChemicalConsumptionLineInline]


@admin.register(SludgeGenerationEntry)
class SludgeGenerationEntryAdmin(admin.ModelAdmin):
    list_display = (
        "serial_no",
        "date",
        "plant",
        "quantity_kg",
        "collection_mode",
        "storage_method",
        "operator",
    )
    list_filter = ("plant", "date")
    date_hierarchy = "date"


@admin.register(BackwashEntry)
class BackwashEntryAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "plant",
        "equipment",
        "start_time",
        "stop_time",
        "contact_minutes",
        "operator",
    )
    list_filter = ("plant", "equipment", "date")
    date_hierarchy = "date"


class CalibrationPointInline(admin.TabularInline):
    model = CalibrationPoint
    extra = 0


@admin.register(CalibrationInstrument)
class CalibrationInstrumentAdmin(admin.ModelAdmin):
    list_display = (
        "equipment_name",
        "equipment_id",
        "plant",
        "frequency",
        "tolerance",
        "is_active",
    )
    list_filter = ("plant", "frequency", "is_active")
    search_fields = ("equipment_name", "equipment_id")
    inlines = [CalibrationPointInline]


class CalibrationReadingInline(admin.TabularInline):
    model = CalibrationReading
    extra = 0
    readonly_fields = ("variation", "is_within_tolerance")


@admin.register(CalibrationRecord)
class CalibrationRecordAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "time",
        "instrument",
        "due_date",
        "is_out_of_calibration",
        "checked_by",
    )
    list_filter = ("instrument", "is_out_of_calibration", "date")
    date_hierarchy = "date"
    inlines = [CalibrationReadingInline]


@admin.register(RegisterChangeLog)
class RegisterChangeLogAdmin(admin.ModelAdmin):
    """Append-only: readable in the admin, never editable there."""

    list_display = (
        "changed_at",
        "register",
        "action",
        "plant",
        "entry_date",
        "summary",
        "changed_by",
    )
    list_filter = ("register", "action", "plant", "entry_date")
    search_fields = ("summary", "model_name")
    date_hierarchy = "changed_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EtpPrintDocument)
class EtpPrintDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "document_key",
        "company",
        "document_code",
        "revision",
        "issue_date",
        "document_id",
        "is_active",
    )
    list_filter = ("document_key", "company", "is_active")
    search_fields = ("document_code", "form_name", "document_id")
