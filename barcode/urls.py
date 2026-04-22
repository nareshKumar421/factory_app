from django.urls import path
from .views import (
    BoxGenerateAPI, BoxListAPI, BoxDetailAPI, BoxVoidAPI,
    PalletCreateAPI, PalletListAPI, PalletDetailAPI, PalletVoidAPI,
    PalletMoveAPI, PalletClearAPI, PalletSplitAPI,
    PalletAddBoxesAPI, PalletRemoveBoxesAPI, BoxTransferAPI,
    BoxPrintAPI, PalletPrintAPI, BulkPrintAPI, PrintHistoryAPI,
    DismantlePalletAPI, DismantleBoxAPI, RepackAPI,
    LooseStockListAPI, LooseStockDetailAPI,
    ScanAPI, BarcodeLookupAPI, ScanHistoryAPI,
    ProductionRunLabelsAPI, ProductionRunPalletAPI,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # Boxes
    # ------------------------------------------------------------------
    path('boxes/generate/', BoxGenerateAPI.as_view(), name='bc-box-generate'),
    path('boxes/', BoxListAPI.as_view(), name='bc-box-list'),
    path('boxes/<int:box_id>/', BoxDetailAPI.as_view(), name='bc-box-detail'),
    path('boxes/<int:box_id>/void/', BoxVoidAPI.as_view(), name='bc-box-void'),

    # ------------------------------------------------------------------
    # Pallets
    # ------------------------------------------------------------------
    path('pallets/create/', PalletCreateAPI.as_view(), name='bc-pallet-create'),
    path('pallets/', PalletListAPI.as_view(), name='bc-pallet-list'),
    path('pallets/<int:pallet_id>/', PalletDetailAPI.as_view(), name='bc-pallet-detail'),
    path('pallets/<int:pallet_id>/void/', PalletVoidAPI.as_view(), name='bc-pallet-void'),
    path('pallets/<int:pallet_id>/move/', PalletMoveAPI.as_view(), name='bc-pallet-move'),
    path('pallets/<int:pallet_id>/clear/', PalletClearAPI.as_view(), name='bc-pallet-clear'),
    path('pallets/<int:pallet_id>/split/', PalletSplitAPI.as_view(), name='bc-pallet-split'),
    path('pallets/<int:pallet_id>/add-boxes/', PalletAddBoxesAPI.as_view(), name='bc-pallet-add-boxes'),
    path('pallets/<int:pallet_id>/remove-boxes/', PalletRemoveBoxesAPI.as_view(), name='bc-pallet-remove-boxes'),

    # ------------------------------------------------------------------
    # Box Transfer
    # ------------------------------------------------------------------
    path('transfers/box/', BoxTransferAPI.as_view(), name='bc-transfer-box'),

    # ------------------------------------------------------------------
    # Print / Label
    # ------------------------------------------------------------------
    path('print/box/<int:box_id>/', BoxPrintAPI.as_view(), name='bc-print-box'),
    path('print/pallet/<int:pallet_id>/', PalletPrintAPI.as_view(), name='bc-print-pallet'),
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
    # Production Integration
    # ------------------------------------------------------------------
    path('production/<int:run_id>/generate-labels/', ProductionRunLabelsAPI.as_view(), name='bc-production-labels'),
    path('production/<int:run_id>/create-pallet/', ProductionRunPalletAPI.as_view(), name='bc-production-pallet'),
]
