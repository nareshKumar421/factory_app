from django.urls import path

from .views import (
    ItemBatchAPI,
    LogisticsBoardSettingsAPI,
    OwnedVehicleStatusAPI,
    StockInTransitAPI,
    StockDashboardAPI,
    StockDashboardAsOfAPI,
    StockDashboardExportAPI,
    StockItemDetailAPI,
    WarehouseBoardSettingsAPI,
    WarehouseOccupancyAPI,
)

urlpatterns = [
    path("", StockDashboardAPI.as_view(), name="stock-dashboard"),
    path("as-of/", StockDashboardAsOfAPI.as_view(), name="stock-dashboard-as-of"),
    path("occupancy/", WarehouseOccupancyAPI.as_view(), name="warehouse-occupancy"),
    path("export/", StockDashboardExportAPI.as_view(), name="stock-dashboard-export"),
    # Before the `<str:item_code>/` routes below. No collision today -- those
    # need a second segment -- but an item code called "warehouse-settings"
    # would be a very quiet bug to chase.
    path(
        "board-settings/",
        LogisticsBoardSettingsAPI.as_view(),
        name="logistics-board-settings",
    ),
    path(
        "owned-vehicles/",
        OwnedVehicleStatusAPI.as_view(),
        name="owned-vehicle-status",
    ),
    path(
        "stock-in-transit/",
        StockInTransitAPI.as_view(),
        name="stock-in-transit",
    ),
    path(
        "warehouse-settings/",
        WarehouseBoardSettingsAPI.as_view(),
        name="warehouse-board-settings",
    ),
    path("<str:item_code>/batches/", ItemBatchAPI.as_view(), name="stock-item-batches"),
    path("<str:item_code>/warehouses/", StockItemDetailAPI.as_view(), name="stock-item-detail"),
]
