from django.urls import path

from .views import StockDashboardAPI, StockItemDetailAPI

urlpatterns = [
    path("", StockDashboardAPI.as_view(), name="stock-dashboard"),
    path("<str:item_code>/warehouses/", StockItemDetailAPI.as_view(), name="stock-item-detail"),
]
