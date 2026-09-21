from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views_manager import (
    ElectricityMeterScopeGapsAPI,
    MyElectricityMetersAPI,
    UserElectricityMeterDetailAPI,
    UserElectricityMeterListAPI,
)

from .views import (
    AssetCategoryViewSet,
    DailyElectricityReadingViewSet,
    DailyWastageLogViewSet,
    ElectricityMeterViewSet,
    AssetDepartmentViewSet,
    AssetDocumentViewSet,
    AssetLocationViewSet,
    AssetPhotoViewSet,
    AssetViewSet,
    FireCategoryViewSet,
    FireEquipmentIssueViewSet,
    FireMovementViewSet,
    FireRequestViewSet,
    FireShiftReportAttachmentViewSet,
    FireShiftReportItemViewSet,
    FireShiftReportPhotoViewSet,
    FireShiftReportViewSet,
    MaintenanceDashboardAPI,
    MaintenanceFireViewSet,
    MaintenanceGateLinkViewSet,
    MaintenanceAlertsAPI,
    MaintenanceOptionsAPI,
    MaintenanceReportsAPI,
    MaintenanceScanLookupAPI,
    MaintenanceScanWorkOrderAPI,
    MaintenanceChecklistTemplateItemViewSet,
    MaintenanceSpareViewSet,
    MaintenanceSpareStockAPI,
    MaintenanceSpareReceiptViewSet,
    MaintenanceVendorVisitViewSet,
    MaintenanceWorkOrderAttachmentViewSet,
    MaintenanceWorkOrderPhotoViewSet,
    MaintenanceWorkOrderViewSet,
    PreventiveMaintenanceExecutionViewSet,
    PreventiveMaintenancePlanViewSet,
    MaterialIndentAttachmentViewSet,
    MaterialIndentQuotationViewSet,
    MaterialIndentViewSet,
    SafetyFinePhotoViewSet,
    SafetyFineViewSet,
    SafetyViolationTypeViewSet,
    SpareCategoryViewSet,
    SpareMovementViewSet,
    SpareRequestViewSet,
    WorkPermitAttachmentViewSet,
    WorkPermitViewSet,
    WorkPermitWorkerViewSet,
)

router = DefaultRouter()
router.register("asset-categories", AssetCategoryViewSet, basename="maintenance-asset-category")
router.register("asset-locations", AssetLocationViewSet, basename="maintenance-asset-location")
router.register("asset-departments", AssetDepartmentViewSet, basename="maintenance-asset-department")
router.register("assets", AssetViewSet, basename="maintenance-asset")
router.register("asset-photos", AssetPhotoViewSet, basename="maintenance-asset-photo")
router.register("asset-documents", AssetDocumentViewSet, basename="maintenance-asset-document")
router.register("work-orders", MaintenanceWorkOrderViewSet, basename="maintenance-work-order")
router.register("pm-plans", PreventiveMaintenancePlanViewSet, basename="maintenance-pm-plan")
router.register(
    "pm-checklist-items",
    MaintenanceChecklistTemplateItemViewSet,
    basename="maintenance-pm-checklist-item",
)
router.register(
    "pm-executions",
    PreventiveMaintenanceExecutionViewSet,
    basename="maintenance-pm-execution",
)
router.register("spare-categories", SpareCategoryViewSet, basename="maintenance-spare-category")
router.register("spares", MaintenanceSpareViewSet, basename="maintenance-spare")
router.register("spare-requests", SpareRequestViewSet, basename="maintenance-spare-request")
router.register("spare-movements", SpareMovementViewSet, basename="maintenance-spare-movement")
router.register("fire-categories", FireCategoryViewSet, basename="maintenance-fire-category")
router.register("fire", MaintenanceFireViewSet, basename="maintenance-fire")
router.register("fire-requests", FireRequestViewSet, basename="maintenance-fire-request")
router.register("fire-movements", FireMovementViewSet, basename="maintenance-fire-movement")
router.register("fire-reports", FireShiftReportViewSet, basename="maintenance-fire-report")
router.register("fire-report-items", FireShiftReportItemViewSet, basename="maintenance-fire-report-item")
router.register(
    "fire-report-photos",
    FireShiftReportPhotoViewSet,
    basename="maintenance-fire-report-photo",
)
router.register(
    "fire-report-attachments",
    FireShiftReportAttachmentViewSet,
    basename="maintenance-fire-report-attachment",
)
router.register("fire-issues", FireEquipmentIssueViewSet, basename="maintenance-fire-issue")
router.register(
    "safety-violation-types",
    SafetyViolationTypeViewSet,
    basename="maintenance-safety-violation-type",
)
router.register("material-indents", MaterialIndentViewSet, basename="maintenance-material-indent")
router.register(
    "material-indent-quotations",
    MaterialIndentQuotationViewSet,
    basename="maintenance-material-indent-quotation",
)
router.register(
    "material-indent-attachments",
    MaterialIndentAttachmentViewSet,
    basename="maintenance-material-indent-attachment",
)
router.register("safety-fines", SafetyFineViewSet, basename="maintenance-safety-fine")
router.register(
    "safety-fine-photos",
    SafetyFinePhotoViewSet,
    basename="maintenance-safety-fine-photo",
)
router.register("work-permits", WorkPermitViewSet, basename="maintenance-work-permit")
router.register(
    "work-permit-workers",
    WorkPermitWorkerViewSet,
    basename="maintenance-work-permit-worker",
)
router.register(
    "work-permit-attachments",
    WorkPermitAttachmentViewSet,
    basename="maintenance-work-permit-attachment",
)
router.register("gate-links", MaintenanceGateLinkViewSet, basename="maintenance-gate-link")
router.register("spare-receipts", MaintenanceSpareReceiptViewSet, basename="maintenance-spare-receipt")
router.register("vendor-visits", MaintenanceVendorVisitViewSet, basename="maintenance-vendor-visit")
router.register("electricity-meters", ElectricityMeterViewSet, basename="maintenance-electricity-meter")
router.register(
    "daily-electricity-readings",
    DailyElectricityReadingViewSet,
    basename="maintenance-daily-electricity-reading",
)
router.register(
    "daily-wastage-logs",
    DailyWastageLogViewSet,
    basename="maintenance-daily-wastage-log",
)
router.register(
    "work-order-photos",
    MaintenanceWorkOrderPhotoViewSet,
    basename="maintenance-work-order-photo",
)
router.register(
    "work-order-attachments",
    MaintenanceWorkOrderAttachmentViewSet,
    basename="maintenance-work-order-attachment",
)

urlpatterns = [
    path("dashboard/", MaintenanceDashboardAPI.as_view(), name="maintenance-dashboard"),
    path("reports/", MaintenanceReportsAPI.as_view(), name="maintenance-reports"),
    path("scan/lookup/", MaintenanceScanLookupAPI.as_view(), name="maintenance-scan-lookup"),
    path("scan/work-order/", MaintenanceScanWorkOrderAPI.as_view(), name="maintenance-scan-work-order"),
    path("spares/stock/", MaintenanceSpareStockAPI.as_view(), name="maintenance-spare-stock"),
    path("alerts/", MaintenanceAlertsAPI.as_view(), name="maintenance-alerts"),
    path("options/", MaintenanceOptionsAPI.as_view(), name="maintenance-options"),

    # ------------------------------------------------------------------
    # Electricity meter managers — who may retune a meter and file its
    # readings. `my-electricity-meters/` is intentionally NOT admin-gated: the
    # register page needs it to disable the actions it cannot perform, and it
    # only answers about the caller. `gaps/` before `<int:pk>/` so the report's
    # path is never read as an assignment id.
    # ------------------------------------------------------------------
    path(
        "my-electricity-meters/",
        MyElectricityMetersAPI.as_view(),
        name="my-electricity-meters",
    ),
    path(
        "user-electricity-meters/",
        UserElectricityMeterListAPI.as_view(),
        name="user-electricity-meter-list",
    ),
    path(
        "user-electricity-meters/gaps/",
        ElectricityMeterScopeGapsAPI.as_view(),
        name="user-electricity-meter-gaps",
    ),
    path(
        "user-electricity-meters/<int:pk>/",
        UserElectricityMeterDetailAPI.as_view(),
        name="user-electricity-meter-detail",
    ),

    path("", include(router.urls)),
]
