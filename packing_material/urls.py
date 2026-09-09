from django.urls import path

from .views import (
    PackingMaterialDispatchAPI,
    PackingMaterialPlanListAPI,
    PackingMaterialProductionAPI,
    PackingMaterialRequirementAPI,
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
    # The requirement board: the month's plan against what is left to buy.
    path(
        "plans/",
        PackingMaterialPlanListAPI.as_view(),
        name="packing-material-plans",
    ),
    path(
        "requirement/",
        PackingMaterialRequirementAPI.as_view(),
        name="packing-material-requirement",
    ),
]
