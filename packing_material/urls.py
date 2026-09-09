from django.urls import path

from .views import (
    PackingMaterialDispatchAPI,
    PackingMaterialProductionAPI,
    PackingMaterialStockAPI,
)

urlpatterns = [
    path("stock/", PackingMaterialStockAPI.as_view(), name="packing-material-stock"),
    path(
        "production/",
        PackingMaterialProductionAPI.as_view(),
        name="packing-material-production",
    ),
    path(
        "dispatch/",
        PackingMaterialDispatchAPI.as_view(),
        name="packing-material-dispatch",
    ),
]
