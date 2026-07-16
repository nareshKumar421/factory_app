from django.urls import path
from .views import (
    BoxGenerateAPI, BoxListAPI, BoxDetailAPI, BoxVoidAPI,
    PalletCreateAPI, PalletListAPI, PalletDetailAPI, PalletVoidAPI,
    VoidedPalletsAPI, VoidedBoxesAPI,
    PalletMoveAPI, PalletClearAPI, PalletSplitAPI,
    PalletAddBoxesAPI, PalletRemoveBoxesAPI, PalletReconcileAPI, BoxTransferAPI,
    PalletVerifyRequestListCreateAPI, PalletVerifyRequestDetailAPI,
    PalletVerifyRequestStartAPI, PalletVerifyRequestResolveAPI,
    PalletVerifyRequestCancelAPI,
    BoxPrintAPI, PalletPrintAPI, PalletPrintWorkflowAPI, BulkPrintAPI, PrintHistoryAPI,
    DismantlePalletAPI, DismantleBoxAPI, RepackAPI,
    LooseStockListAPI, LooseStockDetailAPI,
    ScanAPI, BarcodeLookupAPI, ScanHistoryAPI,
    DispatchBillLookupAPI, DispatchSessionListCreateAPI,
    DispatchSessionFromBillAPI, DispatchSessionActiveAPI,
    DispatchSessionCompletedAPI, DispatchSessionClosedAPI,
    DispatchSessionDetailAPI, DispatchSessionScanAPI,
    DispatchScannedBoxQtyAPI, DispatchScannedBoxRemoveAPI,
    DispatchSessionDispatchAPI, DispatchSessionCompleteAPI,
    DispatchSessionCloseAPI, DispatchSessionCancelAPI,
    DispatchSessionRetrySapSyncAPI, DispatchSettingsAPI,
    DispatchSessionScanLogsAPI, DispatchSessionSapSyncLogsAPI,
    PalletHistoryAPI, BoxHistoryAPI,
    DispatchReportAPI, DispatchReportDetailAPI,
    DispatchPalletReportAPI, DispatchBoxReportAPI,
    DispatchRejectedScanReportAPI,
    BarcodeTraceabilityAPI, IntercompanyTransferDashboardAPI,
    IntercompanyTransferDetailAPI, IntercompanyTransferListCreateAPI,
    IntercompanyTransferReverseAPI, IntercompanyTransferScanAPI,
    ProductionRunLabelsAPI, ProductionRunPalletAPI, ProductionReleaseOilListAPI,
    OitmItemListAPI, OitmItemDetailAPI,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # Boxes
    # ------------------------------------------------------------------
    path('boxes/generate/', BoxGenerateAPI.as_view(), name='bc-box-generate'),
    path('boxes/', BoxListAPI.as_view(), name='bc-box-list'),
    path('boxes/<int:box_id>/', BoxDetailAPI.as_view(), name='bc-box-detail'),
    path('boxes/<int:box_id>/void/', BoxVoidAPI.as_view(), name='bc-box-void'),
    path('boxes/<int:box_id>/history/', BoxHistoryAPI.as_view(), name='bc-box-history'),

    # ------------------------------------------------------------------
    # Pallets
    # ------------------------------------------------------------------
    path('pallets/create/', PalletCreateAPI.as_view(), name='bc-pallet-create'),
    path('pallets/', PalletListAPI.as_view(), name='bc-pallet-list'),
    path('pallets/<int:pallet_id>/', PalletDetailAPI.as_view(), name='bc-pallet-detail'),
    path('pallets/<int:pallet_id>/void/', PalletVoidAPI.as_view(), name='bc-pallet-void'),
    path('voids/pallets/', VoidedPalletsAPI.as_view(), name='bc-voided-pallets'),
    path('voids/boxes/', VoidedBoxesAPI.as_view(), name='bc-voided-boxes'),
    path('pallets/<int:pallet_id>/move/', PalletMoveAPI.as_view(), name='bc-pallet-move'),
    path('pallets/<int:pallet_id>/clear/', PalletClearAPI.as_view(), name='bc-pallet-clear'),
    path('pallets/<int:pallet_id>/split/', PalletSplitAPI.as_view(), name='bc-pallet-split'),
    path('pallets/<int:pallet_id>/add-boxes/', PalletAddBoxesAPI.as_view(), name='bc-pallet-add-boxes'),
    path('pallets/<int:pallet_id>/remove-boxes/', PalletRemoveBoxesAPI.as_view(), name='bc-pallet-remove-boxes'),
    path('pallets/<int:pallet_id>/reconcile/', PalletReconcileAPI.as_view(), name='bc-pallet-reconcile'),
    path('pallets/<int:pallet_id>/history/', PalletHistoryAPI.as_view(), name='bc-pallet-history'),

    # ------------------------------------------------------------------
    # Pallet Verify Requests (ticket workflow)
    # ------------------------------------------------------------------
    path('verify-requests/', PalletVerifyRequestListCreateAPI.as_view(), name='bc-verify-request-list-create'),
    path('verify-requests/<int:request_id>/', PalletVerifyRequestDetailAPI.as_view(), name='bc-verify-request-detail'),
    path('verify-requests/<int:request_id>/start/', PalletVerifyRequestStartAPI.as_view(), name='bc-verify-request-start'),
    path('verify-requests/<int:request_id>/resolve/', PalletVerifyRequestResolveAPI.as_view(), name='bc-verify-request-resolve'),
    path('verify-requests/<int:request_id>/cancel/', PalletVerifyRequestCancelAPI.as_view(), name='bc-verify-request-cancel'),

    # ------------------------------------------------------------------
    # Box Transfer
    # ------------------------------------------------------------------
    path('transfers/box/', BoxTransferAPI.as_view(), name='bc-transfer-box'),
    path('intercompany/dashboard/', IntercompanyTransferDashboardAPI.as_view(), name='bc-intercompany-dashboard'),
    path('intercompany/transfers/', IntercompanyTransferListCreateAPI.as_view(), name='bc-intercompany-transfer-list-create'),
    path('intercompany/transfers/<int:transfer_id>/', IntercompanyTransferDetailAPI.as_view(), name='bc-intercompany-transfer-detail'),
    path('intercompany/transfers/<int:transfer_id>/reverse/', IntercompanyTransferReverseAPI.as_view(), name='bc-intercompany-transfer-reverse'),
    path('intercompany/scan/', IntercompanyTransferScanAPI.as_view(), name='bc-intercompany-scan'),
    path('intercompany/trace/', BarcodeTraceabilityAPI.as_view(), name='bc-intercompany-trace'),

    # ------------------------------------------------------------------
    # Print / Label
    # ------------------------------------------------------------------
    path('print/box/<int:box_id>/', BoxPrintAPI.as_view(), name='bc-print-box'),
    path('print/pallet/<int:pallet_id>/', PalletPrintAPI.as_view(), name='bc-print-pallet'),
    path('print/pallet/<int:pallet_id>/workflow/', PalletPrintWorkflowAPI.as_view(), name='bc-print-pallet-workflow'),
    path('print/bulk/', BulkPrintAPI.as_view(), name='bc-print-bulk'),
    path('print/history/', PrintHistoryAPI.as_view(), name='bc-print-history'),

    # ------------------------------------------------------------------
    # Dismantle & Repack
    # ------------------------------------------------------------------
    path('pallets/<int:pallet_id>/dismantle/', DismantlePalletAPI.as_view(), name='bc-dismantle-pallet'),
    path('boxes/<int:box_id>/dismantle/', DismantleBoxAPI.as_view(), name='bc-dismantle-box'),
    path('repack/', RepackAPI.as_view(), name='bc-repack'),

    # ------------------------------------------------------------------
    # Loose Stock
    # ------------------------------------------------------------------
    path('loose/', LooseStockListAPI.as_view(), name='bc-loose-list'),
    path('loose/<int:loose_id>/', LooseStockDetailAPI.as_view(), name='bc-loose-detail'),

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    path('scan/', ScanAPI.as_view(), name='bc-scan'),
    path('scan/history/', ScanHistoryAPI.as_view(), name='bc-scan-history'),
    path('lookup/<str:barcode_string>/', BarcodeLookupAPI.as_view(), name='bc-lookup'),

    # ------------------------------------------------------------------
    # Barcode Dispatch
    # ------------------------------------------------------------------
    path('dispatch/bills/lookup/', DispatchBillLookupAPI.as_view(), name='bc-dispatch-bill-lookup'),
    path('dispatch/settings/', DispatchSettingsAPI.as_view(), name='bc-dispatch-settings'),
    path('dispatch/sessions/from-bill/', DispatchSessionFromBillAPI.as_view(), name='bc-dispatch-session-from-bill'),
    path('dispatch/sessions/', DispatchSessionListCreateAPI.as_view(), name='bc-dispatch-session-list-create'),
    path('dispatch/sessions/active/', DispatchSessionActiveAPI.as_view(), name='bc-dispatch-session-active'),
    path('dispatch/sessions/completed/', DispatchSessionCompletedAPI.as_view(), name='bc-dispatch-session-completed'),
    path('dispatch/sessions/closed/', DispatchSessionClosedAPI.as_view(), name='bc-dispatch-session-closed'),
    path('dispatch/sessions/<int:session_id>/', DispatchSessionDetailAPI.as_view(), name='bc-dispatch-session-detail'),
    path('dispatch/sessions/<int:session_id>/scans/', DispatchSessionScanAPI.as_view(), name='bc-dispatch-session-scan'),
    path('dispatch/sessions/<int:session_id>/scan/', DispatchSessionScanAPI.as_view(), name='bc-dispatch-session-scan-alias'),
    path('dispatch/sessions/<int:session_id>/scanned-boxes/<int:unit_id>/', DispatchScannedBoxQtyAPI.as_view(), name='bc-dispatch-scanned-box-update'),
    path('dispatch/sessions/<int:session_id>/scanned-boxes/<int:unit_id>/remove/', DispatchScannedBoxRemoveAPI.as_view(), name='bc-dispatch-scanned-box-remove'),
    path('dispatch/sessions/<int:session_id>/dispatch/', DispatchSessionDispatchAPI.as_view(), name='bc-dispatch-session-dispatch'),
    path('dispatch/sessions/<int:session_id>/complete/', DispatchSessionCompleteAPI.as_view(), name='bc-dispatch-session-complete'),
    path('dispatch/sessions/<int:session_id>/close/', DispatchSessionCloseAPI.as_view(), name='bc-dispatch-session-close'),
    path('dispatch/sessions/<int:session_id>/cancel/', DispatchSessionCancelAPI.as_view(), name='bc-dispatch-session-cancel'),
    path('dispatch/sessions/<int:session_id>/retry-sap-sync/', DispatchSessionRetrySapSyncAPI.as_view(), name='bc-dispatch-session-retry-sap'),
    path('dispatch/sessions/<int:session_id>/scan-logs/', DispatchSessionScanLogsAPI.as_view(), name='bc-dispatch-session-scan-logs'),
    path('dispatch/sessions/<int:session_id>/sap-sync-logs/', DispatchSessionSapSyncLogsAPI.as_view(), name='bc-dispatch-session-sap-sync-logs'),
    path('dispatch/reports/', DispatchReportAPI.as_view(), name='bc-dispatch-report'),
    path('dispatch/reports/pallets/', DispatchPalletReportAPI.as_view(), name='bc-dispatch-report-pallets'),
    path('dispatch/reports/boxes/', DispatchBoxReportAPI.as_view(), name='bc-dispatch-report-boxes'),
    path('dispatch/reports/rejected-scans/', DispatchRejectedScanReportAPI.as_view(), name='bc-dispatch-report-rejected-scans'),
    path('dispatch/reports/<int:session_id>/', DispatchReportDetailAPI.as_view(), name='bc-dispatch-report-detail'),

    # ------------------------------------------------------------------
    # Production Integration
    # ------------------------------------------------------------------
    path('items/oitm/', OitmItemListAPI.as_view(), name='bc-oitm-items'),
    path('items/oitm/detail/', OitmItemDetailAPI.as_view(), name='bc-oitm-item-detail'),
    path('production-release-oil/', ProductionReleaseOilListAPI.as_view(), name='bc-production-release-oil'),
    path('production/<int:run_id>/generate-labels/', ProductionRunLabelsAPI.as_view(), name='bc-production-labels'),
    path('production/<int:run_id>/create-pallet/', ProductionRunPalletAPI.as_view(), name='bc-production-pallet'),
]
