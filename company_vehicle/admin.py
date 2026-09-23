from django.contrib import admin

from .models import DailyReading, FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument


@admin.register(FleetVehicle)
class FleetVehicleAdmin(admin.ModelAdmin):
    list_display = ("vehicle_number", "nickname", "category", "fuel_type", "status")
    list_filter = ("category", "fuel_type", "status", "is_active")
    search_fields = ("vehicle_number", "nickname", "make_model", "assigned_to")


@admin.register(FuelEntry)
class FuelEntryAdmin(admin.ModelAdmin):
    list_display = (
        "vehicle",
        "entry_date",
        "fuel_type",
        "odometer",
        "quantity",
        "amount",
        "mileage",
    )
    list_filter = ("fuel_type", "payment_mode")
    search_fields = ("vehicle__vehicle_number", "bill_number", "station_name")
    date_hierarchy = "entry_date"
    readonly_fields = ("distance_km", "mileage")


@admin.register(ServiceEntry)
class ServiceEntryAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "entry_date", "kind", "total_amount", "approval_status")
    list_filter = ("approval_status", "kind")
    search_fields = ("vehicle__vehicle_number", "bill_number", "workshop_name")
    date_hierarchy = "entry_date"


@admin.register(VehicleDocument)
class VehicleDocumentAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "doc_type", "document_number", "expiry_date")
    list_filter = ("doc_type",)
    search_fields = ("vehicle__vehicle_number", "document_number")


@admin.register(DailyReading)
class DailyReadingAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "reading_date", "odometer")
    list_filter = ("vehicle",)
    search_fields = ("vehicle__vehicle_number",)
    date_hierarchy = "reading_date"
