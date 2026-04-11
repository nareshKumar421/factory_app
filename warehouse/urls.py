from django.urls import path

from .views import (
    WarehouseBatchExpiryAPI,
    WarehouseDashboardSummaryAPI,
    WarehouseFilterOptionsAPI,
    WarehouseInventoryDetailAPI,
    WarehouseInventoryListAPI,
    WarehouseMovementHistoryAPI,
)

urlpatterns = [
    path("filter-options/", WarehouseFilterOptionsAPI.as_view(), name="warehouse-filter-options"),
    path("inventory/", WarehouseInventoryListAPI.as_view(), name="warehouse-inventory-list"),
    path("inventory/<str:item_code>/", WarehouseInventoryDetailAPI.as_view(), name="warehouse-inventory-detail"),
    path("inventory/<str:item_code>/movements/", WarehouseMovementHistoryAPI.as_view(), name="warehouse-movement-history"),
    path("dashboard/summary/", WarehouseDashboardSummaryAPI.as_view(), name="warehouse-dashboard-summary"),
    path("batch-expiry/", WarehouseBatchExpiryAPI.as_view(), name="warehouse-batch-expiry"),
]
