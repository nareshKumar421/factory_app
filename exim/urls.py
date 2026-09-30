"""Import / Export, under ``api/v1/exim/``."""

from django.urls import path

from .views_contract import ContractDetailAPI, ContractListAPI, ContractTermsAPI
from .views_customs_rates import CustomsRatesAPI
from .views_lot import (
    ContractHistoryAPI,
    DirectorInventoryAPI,
    LotArriveAPI,
    LotBulkAPI,
    LotChangeLogAPI,
    LotDetailAPI,
    LotDispatchAPI,
    LotInsightsAPI,
    LotIntoTankAPI,
    LotListAPI,
    LotMoveAPI,
    ShortageListAPI,
    StockDashboardAPI,
    StockDashboardOrderAPI,
    VehicleReportAPI,
    VendorListAPI,
)
from .views_tank import (
    OilDetailAPI,
    OilListAPI,
    OpeningStockAPI,
    SapOilListAPI,
    TankAverageAPI,
    TankDetailAPI,
    TankEmptyAPI,
    TankListAPI,
    TankLogAPI,
    TankSummaryAPI,
)
from .views_licence import (
    LicenceDetailAPI,
    LicenceLineCreateAPI,
    LicenceLineDetailAPI,
    LicenceListCreateAPI,
)

urlpatterns = [
    path("licences/", LicenceListCreateAPI.as_view(), name="exim-licences"),
    path("licences/<int:pk>/", LicenceDetailAPI.as_view(), name="exim-licence"),
    path("licences/<int:pk>/lines/", LicenceLineCreateAPI.as_view(), name="exim-licence-lines"),
    path("licence-lines/<int:pk>/", LicenceLineDetailAPI.as_view(), name="exim-licence-line"),
    path("customs-rates/", CustomsRatesAPI.as_view(), name="exim-customs-rates"),
    # The tank farm.
    path("oils/", OilListAPI.as_view(), name="exim-oils"),
    path("oils/<int:pk>/", OilDetailAPI.as_view(), name="exim-oil"),
    path("oils/sap/", SapOilListAPI.as_view(), name="exim-sap-oils"),
    path("tanks/", TankListAPI.as_view(), name="exim-tanks"),
    path("tanks/summary/", TankSummaryAPI.as_view(), name="exim-tank-summary"),
    path("tanks/average/", TankAverageAPI.as_view(), name="exim-tank-average"),
    path("tanks/opening-stock/", OpeningStockAPI.as_view(), name="exim-opening-stock"),
    path("tanks/<int:pk>/", TankDetailAPI.as_view(), name="exim-tank"),
    path("tanks/<int:pk>/empty/", TankEmptyAPI.as_view(), name="exim-tank-empty"),
    path("tank-log/", TankLogAPI.as_view(), name="exim-tank-log"),
    # Oil lots.
    path("lots/", LotListAPI.as_view(), name="exim-lots"),
    path("lots/insights/", LotInsightsAPI.as_view(), name="exim-lot-insights"),
    path("lots/bulk/", LotBulkAPI.as_view(), name="exim-lot-bulk"),
    path("lots/changes/", LotChangeLogAPI.as_view(), name="exim-lot-changes"),
    path("lots/dashboard/", StockDashboardAPI.as_view(), name="exim-stock-dashboard"),
    path("lots/dashboard/order/", StockDashboardOrderAPI.as_view(), name="exim-stock-dashboard-order"),
    path("lots/vehicle-report/", VehicleReportAPI.as_view(), name="exim-vehicle-report"),
    path("lots/<int:pk>/", LotDetailAPI.as_view(), name="exim-lot"),
    path("lots/<int:pk>/move/", LotMoveAPI.as_view(), name="exim-lot-move"),
    path("lots/<int:pk>/dispatch/", LotDispatchAPI.as_view(), name="exim-lot-dispatch"),
    path("lots/<int:pk>/arrive/", LotArriveAPI.as_view(), name="exim-lot-arrive"),
    path("lots/<int:pk>/into-tank/", LotIntoTankAPI.as_view(), name="exim-lot-into-tank"),
    path("shortages/", ShortageListAPI.as_view(), name="exim-shortages"),
    path("contract-history/", ContractHistoryAPI.as_view(), name="exim-contract-history"),
    path("vendors/", VendorListAPI.as_view(), name="exim-vendors"),
    path("director-inventory/", DirectorInventoryAPI.as_view(), name="exim-director-inventory"),
    path("contracts/", ContractListAPI.as_view(), name="exim-contracts"),
    path("contracts/<str:po_number>/", ContractDetailAPI.as_view(), name="exim-contract"),
    path("contracts/<str:po_number>/terms/", ContractTermsAPI.as_view(), name="exim-contract-terms"),
]
