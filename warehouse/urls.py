from django.urls import path
from .views import (
    # BOM Request
    BOMRequestCreateAPI, BOMRequestListAPI, BOMRequestDetailAPI,
    BOMRequestApproveAPI, BOMRequestRejectAPI, BOMRequestReRequestAPI,
    # Material Issue
    MaterialIssueAPI,
    # Stock Check
    StockCheckAPI,
    # Finished Goods Receipt
    FGReceiptCreateAPI, FGReceiptListAPI, FGReceiptDetailAPI,
    FGReceiptReceiveAPI, FGReceiptPostToSAPAPI,
)
from .views_wms import (
    WMSWarehouseListAPI,
    WMSItemGroupListAPI,
)
from .views_bst import (
    BSTSAPTransferListView,
    BSTSAPTransferDetailView,
    BSTTransferListCreateView,
    BSTTransferDetailView,
    BSTBoxScanListCreateView,
    BSTBoxScanBatchView,
    BSTBoxScanDetailView,
    BSTApproveView,
    BSTCancelView,
    BSTPartialTransferRequestView,
    BSTPartialTransferListView,
    BSTPartialTransferApproveView,
    BSTPartialTransferRejectView,
    BSTIncomingListView,
    BSTIncomingDetailView,
    BSTReceiveScanView,
    BSTReceiveCompleteView,
    BSTGateOutwardsListView,
    BSTGateInwardsListView,
    BSTGateMarkOutView,
    BSTGateMarkInView,
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
    path('bom-requests/<int:request_id>/re-request/', BOMRequestReRequestAPI.as_view(), name='wh-bom-request-re-request'),

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
    # WMS — Dropdowns (shared: used by barcode pallet pages + stock-level dashboard)
    # ------------------------------------------------------------------
    path('wms/warehouses/', WMSWarehouseListAPI.as_view(), name='wms-warehouse-list'),
    path('wms/item-groups/', WMSItemGroupListAPI.as_view(), name='wms-item-groups'),

    # ------------------------------------------------------------------
    # Branch Stock Transfer (BST)
    # ------------------------------------------------------------------
    path('bst/sap-transfers/', BSTSAPTransferListView.as_view(), name='bst-sap-transfer-list'),
    path('bst/sap-transfers/<int:doc_entry>/', BSTSAPTransferDetailView.as_view(), name='bst-sap-transfer-detail'),
    path('bst/incoming/', BSTIncomingListView.as_view(), name='bst-incoming-list'),
    path('bst/incoming/<int:transfer_id>/', BSTIncomingDetailView.as_view(), name='bst-incoming-detail'),
    path('bst/gate/expected-outwards/', BSTGateOutwardsListView.as_view(), name='bst-gate-outwards'),
    path('bst/gate/expected-inwards/', BSTGateInwardsListView.as_view(), name='bst-gate-inwards'),
    path('bst/partial-transfers/', BSTPartialTransferListView.as_view(), name='bst-partial-transfer-list'),
    path('bst/partial-transfers/<int:pk>/approve/', BSTPartialTransferApproveView.as_view(), name='bst-partial-transfer-approve'),
    path('bst/partial-transfers/<int:pk>/reject/', BSTPartialTransferRejectView.as_view(), name='bst-partial-transfer-reject'),
    path('bst/', BSTTransferListCreateView.as_view(), name='bst-list-create'),
    path('bst/<int:transfer_id>/', BSTTransferDetailView.as_view(), name='bst-detail'),
    path('bst/<int:transfer_id>/receive-scans/', BSTReceiveScanView.as_view(), name='bst-receive-scan'),
    path('bst/<int:transfer_id>/receive/complete/', BSTReceiveCompleteView.as_view(), name='bst-receive-complete'),
    path('bst/<int:transfer_id>/gate/mark-out/', BSTGateMarkOutView.as_view(), name='bst-gate-mark-out'),
    path('bst/<int:transfer_id>/gate/mark-in/', BSTGateMarkInView.as_view(), name='bst-gate-mark-in'),
    path('bst/<int:transfer_id>/box-scans/', BSTBoxScanListCreateView.as_view(), name='bst-box-scan-list-create'),
    path('bst/<int:transfer_id>/box-scans/batch/', BSTBoxScanBatchView.as_view(), name='bst-box-scan-batch'),
    path('bst/<int:transfer_id>/box-scans/<int:scan_id>/', BSTBoxScanDetailView.as_view(), name='bst-box-scan-detail'),
    path('bst/<int:transfer_id>/approve/', BSTApproveView.as_view(), name='bst-approve'),
    path('bst/<int:transfer_id>/partial-transfer/request/', BSTPartialTransferRequestView.as_view(), name='bst-partial-transfer-request'),
    path('bst/<int:transfer_id>/cancel/', BSTCancelView.as_view(), name='bst-cancel'),
]
