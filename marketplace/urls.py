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
from .views_sheet import (
    BatchDetailView,
    BatchIssuanceExportView,
    BatchListView,
    BatchStockListView,
    IssueRequestDetailView,
    IssueRequestIssueView,
    IssueRequestListCreateView,
    IssueRequestReceiveView,
    IssueRequestRejectView,
    IssueRequestReviewView,
    OrderImportPreviewView,
    OrderImportView,
    SapItemSearchView,
    WarehouseInsightsView,
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
    path("orders/import/preview/", OrderImportPreviewView.as_view(), name="mp-order-import-preview"),
    path("orders/import/", OrderImportView.as_view(), name="mp-order-import"),

    # Sheet import batches
    path("batches/", BatchListView.as_view(), name="mp-batch-list"),
    path("batches/<int:pk>/", BatchDetailView.as_view(), name="mp-batch-detail"),
    path("batches/<int:pk>/stock-list/", BatchStockListView.as_view(), name="mp-batch-stock-list"),
    path("batches/<int:pk>/issuance.csv", BatchIssuanceExportView.as_view(), name="mp-batch-export"),

    # Warehouse issue requests
    path("issue-requests/", IssueRequestListCreateView.as_view(), name="mp-issue-list"),
    path("issue-requests/<int:pk>/", IssueRequestDetailView.as_view(), name="mp-issue-detail"),
    path("issue-requests/<int:pk>/review/", IssueRequestReviewView.as_view(), name="mp-issue-review"),
    path("issue-requests/<int:pk>/reject/", IssueRequestRejectView.as_view(), name="mp-issue-reject"),
    path("issue-requests/<int:pk>/issue/", IssueRequestIssueView.as_view(), name="mp-issue-issue"),
    path("issue-requests/<int:pk>/receive/", IssueRequestReceiveView.as_view(), name="mp-issue-receive"),
    path("warehouse-insights/", WarehouseInsightsView.as_view(), name="mp-warehouse-insights"),
    path("sap-items/", SapItemSearchView.as_view(), name="mp-sap-items"),

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
