from django.urls import path
from .views import (
    # BOM Request
    BOMRequestCreateAPI, BOMRequestListAPI, BOMRequestDetailAPI,
    BOMRequestApproveAPI, BOMRequestRejectAPI,
    # Material Issue
    MaterialIssueAPI,
    # Stock Check
    StockCheckAPI,
    # Finished Goods Receipt
    FGReceiptCreateAPI, FGReceiptListAPI, FGReceiptDetailAPI,
    FGReceiptReceiveAPI, FGReceiptPostToSAPAPI,
)
from .views_wms import (
    WMSDashboardAPI,
    WMSStockOverviewAPI,
    WMSItemDetailAPI,
    WMSStockMovementsAPI,
    WMSTransferOverviewAPI,
    WMSBatchExpiryAPI,
    WMSSalesOrderBacklogAPI,
    WMSWarehouseSummaryAPI,
    WMSBillingOverviewAPI,
    WMSWarehouseListAPI,
    WMSItemGroupListAPI,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # BOM Requests
    # ------------------------------------------------------------------
    path('bom-requests/', BOMRequestListAPI.as_view(), name='wh-bom-request-list'),
    path('bom-requests/create/', BOMRequestCreateAPI.as_view(), name='wh-bom-request-create'),
    path('bom-requests/<int:request_id>/', BOMRequestDetailAPI.as_view(), name='wh-bom-request-detail'),
    path('bom-requests/<int:request_id>/approve/', BOMRequestApproveAPI.as_view(), name='wh-bom-request-approve'),
    path('bom-requests/<int:request_id>/reject/', BOMRequestRejectAPI.as_view(), name='wh-bom-request-reject'),

    # ------------------------------------------------------------------
    # Material Issue (to SAP)
    # ------------------------------------------------------------------
    path('bom-requests/<int:request_id>/issue/', MaterialIssueAPI.as_view(), name='wh-material-issue'),

    # ------------------------------------------------------------------
    # Stock Check
    # ------------------------------------------------------------------
    path('stock/check/', StockCheckAPI.as_view(), name='wh-stock-check'),

    # ------------------------------------------------------------------
    # Finished Goods Receipt
    # ------------------------------------------------------------------
    path('fg-receipts/', FGReceiptListAPI.as_view(), name='wh-fg-receipt-list'),
    path('fg-receipts/create/', FGReceiptCreateAPI.as_view(), name='wh-fg-receipt-create'),
    path('fg-receipts/<int:receipt_id>/', FGReceiptDetailAPI.as_view(), name='wh-fg-receipt-detail'),
    path('fg-receipts/<int:receipt_id>/receive/', FGReceiptReceiveAPI.as_view(), name='wh-fg-receipt-receive'),
    path('fg-receipts/<int:receipt_id>/post-to-sap/', FGReceiptPostToSAPAPI.as_view(), name='wh-fg-receipt-post-sap'),

    # ------------------------------------------------------------------
    # WMS — Dashboard
    # ------------------------------------------------------------------
    path('wms/dashboard/', WMSDashboardAPI.as_view(), name='wms-dashboard'),

    # ------------------------------------------------------------------
    # WMS — Stock
    # ------------------------------------------------------------------
    path('wms/stock/overview/', WMSStockOverviewAPI.as_view(), name='wms-stock-overview'),
    path('wms/stock/items/<str:item_code>/', WMSItemDetailAPI.as_view(), name='wms-item-detail'),
    path('wms/stock/movements/', WMSStockMovementsAPI.as_view(), name='wms-stock-movements'),
    path('wms/transfers/overview/', WMSTransferOverviewAPI.as_view(), name='wms-transfer-overview'),
    path('wms/batches/expiry/', WMSBatchExpiryAPI.as_view(), name='wms-batch-expiry'),
    path('wms/sales-orders/backlog/', WMSSalesOrderBacklogAPI.as_view(), name='wms-sales-order-backlog'),

    # ------------------------------------------------------------------
    # WMS — Warehouses
    # ------------------------------------------------------------------
    path('wms/warehouses/summary/', WMSWarehouseSummaryAPI.as_view(), name='wms-warehouse-summary'),
    path('wms/warehouses/', WMSWarehouseListAPI.as_view(), name='wms-warehouse-list'),

    # ------------------------------------------------------------------
    # WMS — Billing
    # ------------------------------------------------------------------
    path('wms/billing/overview/', WMSBillingOverviewAPI.as_view(), name='wms-billing-overview'),

    # ------------------------------------------------------------------
    # WMS — Dropdowns
    # ------------------------------------------------------------------
    path('wms/item-groups/', WMSItemGroupListAPI.as_view(), name='wms-item-groups'),
]
