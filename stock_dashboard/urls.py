from django.urls import path

from .views import (
    StockDashboardAPI,
    StockDashboardAsOfAPI,
    StockDashboardExportAPI,
    StockItemDetailAPI,
)

urlpatterns = [
    path("", StockDashboardAPI.as_view(), name="stock-dashboard"),
    path("as-of/", StockDashboardAsOfAPI.as_view(), name="stock-dashboard-as-of"),
    path("export/", StockDashboardExportAPI.as_view(), name="stock-dashboard-export"),
    path("<str:item_code>/warehouses/", StockItemDetailAPI.as_view(), name="stock-item-detail"),
]
