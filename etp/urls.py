from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BackwashEntryViewSet,
    BackwashEquipmentViewSet,
    CalibrationInstrumentViewSet,
    CalibrationRecordViewSet,
    ChemicalConsumptionLogViewSet,
    DailyPlantLogViewSet,
    EtpDashboardAPI,
    EtpPrintDocumentViewSet,
    EtpSummaryAPI,
    MonitoringParameterViewSet,
    MonitoringRecordViewSet,
    PlantChemicalViewSet,
    PlantOptionViewSet,
    PlantStaffViewSet,
    RegisterChangeLogViewSet,
    SludgeGenerationEntryViewSet,
    TreatmentPlantViewSet,
)

router = DefaultRouter()

# Masters (Settings screen)
router.register("plants", TreatmentPlantViewSet, basename="etp-plant")
router.register("staff", PlantStaffViewSet, basename="etp-staff")
router.register("options", PlantOptionViewSet, basename="etp-option")
router.register("chemicals", PlantChemicalViewSet, basename="etp-chemical")
router.register(
    "backwash-equipment", BackwashEquipmentViewSet, basename="etp-backwash-equipment"
)
router.register(
    "monitoring-parameters",
    MonitoringParameterViewSet,
    basename="etp-monitoring-parameter",
)
router.register(
    "instruments", CalibrationInstrumentViewSet, basename="etp-instrument"
)
# The document numbers the registers print (QC's print-document pattern).
router.register(
    "print-documents", EtpPrintDocumentViewSet, basename="etp-print-document"
)

# Registers
router.register("daily-logs", DailyPlantLogViewSet, basename="etp-daily-log")
router.register(
    "monitoring-records", MonitoringRecordViewSet, basename="etp-monitoring-record"
)
router.register(
    "chemical-logs", ChemicalConsumptionLogViewSet, basename="etp-chemical-log"
)
router.register(
    "sludge-entries", SludgeGenerationEntryViewSet, basename="etp-sludge-entry"
)
router.register("backwash-entries", BackwashEntryViewSet, basename="etp-backwash-entry")
router.register(
    "calibration-records", CalibrationRecordViewSet, basename="etp-calibration-record"
)

# The registers' edit trail (read-only).
router.register("change-log", RegisterChangeLogViewSet, basename="etp-change-log")

urlpatterns = [
    path("dashboard/", EtpDashboardAPI.as_view(), name="etp-dashboard"),
    path("summary/", EtpSummaryAPI.as_view(), name="etp-summary"),
    path("", include(router.urls)),
]
