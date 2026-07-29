from django.urls import path

from .views import (
    MovementListAPI,
    ProductionStockBoardAPI,
    ProductionWarehouseStockAPI,
    TransferCreateAPI,
    TransferOptionsAPI,
    WarehouseRoleListAPI,
)

app_name = "production_movements"

urlpatterns = [
    path("warehouse-roles/", WarehouseRoleListAPI.as_view(), name="warehouse-roles"),
    path("stock-board/", ProductionStockBoardAPI.as_view(), name="stock-board"),
    path(
        "stock/<str:whs_code>/",
        ProductionWarehouseStockAPI.as_view(),
        name="warehouse-stock",
    ),
    path("transfers/options/", TransferOptionsAPI.as_view(), name="transfer-options"),
    path("transfers/", TransferCreateAPI.as_view(), name="transfer-create"),
    path("movements/", MovementListAPI.as_view(), name="movement-list"),
]
