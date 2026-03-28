from django.urls import path

from .views import InventoryAgeDashboardAPI, InventoryAgeFilterOptionsAPI

urlpatterns = [
    path("filter-options/", InventoryAgeFilterOptionsAPI.as_view(), name="inventory-age-filter-options"),
    path("report/", InventoryAgeDashboardAPI.as_view(), name="inventory-age-report"),
]
