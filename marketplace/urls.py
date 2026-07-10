from django.urls import path

from .views import (
    ComboDetailView,
    ComboListCreateView,
    DispatchCancelView,
    DispatchConfirmView,
    DispatchDetailView,
    DispatchListCreateView,
    DispatchScanDetailView,
    DispatchScanView,
    OrderListView,
    OrderResolveView,
    ReconciliationView,
    ReturnDetailView,
    ReturnListCreateView,
    ReturnScanView,
    ReturnSubmitView,
    SkuMappingDetailView,
    SkuMappingImportView,
    SkuMappingListCreateView,
    WarehouseDetailView,
    WarehouseListCreateView,
)

urlpatterns = [
    # Masters
    path("warehouses/", WarehouseListCreateView.as_view(), name="mp-warehouse-list"),
    path("warehouses/<int:pk>/", WarehouseDetailView.as_view(), name="mp-warehouse-detail"),
    path("sku-mappings/", SkuMappingListCreateView.as_view(), name="mp-sku-list"),
    path("sku-mappings/import/", SkuMappingImportView.as_view(), name="mp-sku-import"),
    path("sku-mappings/<int:pk>/", SkuMappingDetailView.as_view(), name="mp-sku-detail"),
    path("combos/", ComboListCreateView.as_view(), name="mp-combo-list"),
    path("combos/<int:pk>/", ComboDetailView.as_view(), name="mp-combo-detail"),

    # Orders
    path("orders/", OrderListView.as_view(), name="mp-order-list"),
    path("orders/resolve/", OrderResolveView.as_view(), name="mp-order-resolve"),

    # Dispatches (outward)
    path("dispatches/", DispatchListCreateView.as_view(), name="mp-dispatch-list"),
    path("dispatches/<int:pk>/", DispatchDetailView.as_view(), name="mp-dispatch-detail"),
    path("dispatches/<int:pk>/scans/", DispatchScanView.as_view(), name="mp-dispatch-scans"),
    path("dispatches/<int:pk>/scans/<int:scan_id>/", DispatchScanDetailView.as_view(), name="mp-dispatch-scan-detail"),
    path("dispatches/<int:pk>/confirm/", DispatchConfirmView.as_view(), name="mp-dispatch-confirm"),
    path("dispatches/<int:pk>/cancel/", DispatchCancelView.as_view(), name="mp-dispatch-cancel"),

    # Returns (inward)
    path("returns/", ReturnListCreateView.as_view(), name="mp-return-list"),
    path("returns/<int:pk>/", ReturnDetailView.as_view(), name="mp-return-detail"),
    path("returns/<int:pk>/scans/", ReturnScanView.as_view(), name="mp-return-scans"),
    path("returns/<int:pk>/submit/", ReturnSubmitView.as_view(), name="mp-return-submit"),

    # Reconciliation
    path("reconciliation/", ReconciliationView.as_view(), name="mp-reconciliation"),
]
