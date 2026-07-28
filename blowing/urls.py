from django.urls import path

from .views import (
    MachineListCreateAPI, MachineDetailAPI,
    PreformSpecListCreateAPI, PreformSpecDetailAPI,
    RateConfigListCreateAPI, RateConfigDetailAPI,
    CostRateListCreateAPI, CostRateDetailAPI,
    RunListCreateAPI, RunDetailAPI, CompleteRunAPI, RunCostAPI,
    DailyReportAPI, MonthlyReportAPI,
    BuyPriceListCreateAPI, BuyPriceDetailAPI, MakeVsBuyReportAPI,
    VarianceReportAPI,
    SAPItemSearchAPI,
    AuditLogListAPI,
    StartProductionAPI, StopProductionAPI, AddBreakdownAPI, ResolveBreakdownAPI,
    AddManualSegmentAPI, AddManualBreakdownAPI,
    SegmentUpdateAPI,
    BreakdownCategoryListCreateAPI, BreakdownCategoryDetailAPI,
    SubmitPreformRequestAPI,
)

urlpatterns = [
    # Master data
    path('machines/', MachineListCreateAPI.as_view(), name='blowing-machines'),
    path('machines/<int:machine_id>/', MachineDetailAPI.as_view(), name='blowing-machine-detail'),
    path('preform-specs/', PreformSpecListCreateAPI.as_view(), name='blowing-preform-specs'),
    path('preform-specs/<int:spec_id>/', PreformSpecDetailAPI.as_view(), name='blowing-preform-spec-detail'),
    path('rate-configs/', RateConfigListCreateAPI.as_view(), name='blowing-rate-configs'),
    path('rate-configs/<int:config_id>/', RateConfigDetailAPI.as_view(), name='blowing-rate-config-detail'),
    path('cost-rates/', CostRateListCreateAPI.as_view(), name='blowing-cost-rates'),
    path('cost-rates/<int:rate_id>/', CostRateDetailAPI.as_view(), name='blowing-cost-rate-detail'),

    # Runs
    path('runs/', RunListCreateAPI.as_view(), name='blowing-runs'),
    path('runs/<int:run_id>/', RunDetailAPI.as_view(), name='blowing-run-detail'),
    path('runs/<int:run_id>/complete/', CompleteRunAPI.as_view(), name='blowing-run-complete'),
    path('runs/<int:run_id>/cost/', RunCostAPI.as_view(), name='blowing-run-cost'),

    # Run lifecycle
    path('runs/<int:run_id>/start-production/', StartProductionAPI.as_view(), name='blowing-start-production'),
    path('runs/<int:run_id>/stop-production/', StopProductionAPI.as_view(), name='blowing-stop-production'),
    path('runs/<int:run_id>/add-breakdown/', AddBreakdownAPI.as_view(), name='blowing-add-breakdown'),
    path('runs/<int:run_id>/segments/manual/', AddManualSegmentAPI.as_view(), name='blowing-add-manual-segment'),
    path('runs/<int:run_id>/breakdowns/manual/', AddManualBreakdownAPI.as_view(), name='blowing-add-manual-breakdown'),
    path('runs/<int:run_id>/breakdowns/<int:breakdown_id>/resolve/', ResolveBreakdownAPI.as_view(), name='blowing-resolve-breakdown'),
    path('runs/<int:run_id>/segments/<int:segment_id>/', SegmentUpdateAPI.as_view(), name='blowing-segment-update'),

    # Breakdown categories
    path('breakdown-categories/', BreakdownCategoryListCreateAPI.as_view(), name='blowing-breakdown-categories'),
    path('breakdown-categories/<int:category_id>/', BreakdownCategoryDetailAPI.as_view(), name='blowing-breakdown-category-detail'),

    # Warehouse-approval — preform request (raised into warehouse BOM queue)
    path('runs/<int:run_id>/preform-request/', SubmitPreformRequestAPI.as_view(), name='blowing-submit-preform-request'),

    # Buy prices (Phase 2)
    path('buy-prices/', BuyPriceListCreateAPI.as_view(), name='blowing-buy-prices'),
    path('buy-prices/<int:buy_id>/', BuyPriceDetailAPI.as_view(), name='blowing-buy-price-detail'),

    # Reports
    path('reports/daily/', DailyReportAPI.as_view(), name='blowing-report-daily'),
    path('reports/monthly/', MonthlyReportAPI.as_view(), name='blowing-report-monthly'),
    path('reports/make-vs-buy/', MakeVsBuyReportAPI.as_view(), name='blowing-report-make-vs-buy'),
    path('reports/variances/', VarianceReportAPI.as_view(), name='blowing-report-variances'),

    # Audit log
    path('audit/', AuditLogListAPI.as_view(), name='blowing-audit'),

    # SAP item pickers (read-only)
    path('sap/items/', SAPItemSearchAPI.as_view(), name='blowing-sap-items'),
]
