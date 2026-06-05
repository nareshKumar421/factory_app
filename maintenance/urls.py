from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AssetCategoryViewSet,
    AssetDepartmentViewSet,
    AssetDocumentViewSet,
    AssetLocationViewSet,
    AssetPhotoViewSet,
    AssetViewSet,
    MaintenanceDashboardAPI,
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
    MaintenanceWorkOrderPhotoViewSet,
    MaintenanceWorkOrderViewSet,
    PreventiveMaintenanceExecutionViewSet,
    PreventiveMaintenancePlanViewSet,
    SpareCategoryViewSet,
    SpareMovementViewSet,
    SpareRequestViewSet,
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
router.register("gate-links", MaintenanceGateLinkViewSet, basename="maintenance-gate-link")
router.register("spare-receipts", MaintenanceSpareReceiptViewSet, basename="maintenance-spare-receipt")
router.register("vendor-visits", MaintenanceVendorVisitViewSet, basename="maintenance-vendor-visit")
router.register(
    "work-order-photos",
    MaintenanceWorkOrderPhotoViewSet,
    basename="maintenance-work-order-photo",
)

urlpatterns = [
    path("dashboard/", MaintenanceDashboardAPI.as_view(), name="maintenance-dashboard"),
    path("reports/", MaintenanceReportsAPI.as_view(), name="maintenance-reports"),
    path("scan/lookup/", MaintenanceScanLookupAPI.as_view(), name="maintenance-scan-lookup"),
    path("scan/work-order/", MaintenanceScanWorkOrderAPI.as_view(), name="maintenance-scan-work-order"),
    path("spares/stock/", MaintenanceSpareStockAPI.as_view(), name="maintenance-spare-stock"),
    path("alerts/", MaintenanceAlertsAPI.as_view(), name="maintenance-alerts"),
    path("options/", MaintenanceOptionsAPI.as_view(), name="maintenance-options"),
    path("", include(router.urls)),
]
