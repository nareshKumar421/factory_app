from django.urls import path
from .views import (
    # Master Data
    LineListCreateAPI, LineDetailAPI,
    MachineListCreateAPI, MachineDetailAPI,
    ChecklistTemplateListCreateAPI, ChecklistTemplateDetailAPI,
    # Production Runs
    RunListCreateAPI, RunDetailAPI, CompleteRunAPI, RetrySAPGoodsReceiptAPI,
    # Breakdowns
    BreakdownListCreateAPI, BreakdownDetailAPI,
    BreakdownCategoryListCreateAPI, BreakdownCategoryDetailAPI,
    # Timeline Actions
    StartProductionAPI, StopProductionAPI, AddBreakdownAPI, ResolveBreakdownAPI,
    SegmentUpdateAPI, BreakdownUpdateAPI,
    # Material Usage
    MaterialUsageListCreateAPI, MaterialUsageDetailAPI,
    # Machine Runtime
    MachineRuntimeListCreateAPI, MachineRuntimeDetailAPI,
    # Manpower
    ManpowerListCreateAPI, ManpowerDetailAPI,
    # Line Clearance
    LineClearanceListCreateAPI, LineClearanceDetailAPI,
    SubmitClearanceAPI, ApproveClearanceAPI,
    # Machine Checklists
    MachineChecklistListCreateAPI, MachineChecklistBulkAPI,
    MachineChecklistDetailAPI,
    # Waste Management
    WasteLogListCreateAPI, WasteLogDetailAPI,
    WasteApproveEngineerAPI, WasteApproveAMAPI,
    WasteApproveStoreAPI, WasteApproveHODAPI,
    # Reports
    DailyProductionReportAPI, YieldReportAPI,
    LineClearanceReportAPI, AnalyticsAPI,
    # SAP Orders & BOM
    SAPProductionOrderListAPI, SAPProductionOrderDetailAPI, SAPItemSearchAPI,
    SAPItemBOMAPI,
    # Resource Tracking
    ResourceElectricityListCreateAPI, ResourceElectricityDetailAPI,
    ResourceWaterListCreateAPI, ResourceWaterDetailAPI,
    ResourceGasListCreateAPI, ResourceGasDetailAPI,
    ResourceCompressedAirListCreateAPI, ResourceCompressedAirDetailAPI,
    ResourceLabourListCreateAPI, ResourceLabourDetailAPI,
    ResourceMachineCostListCreateAPI, ResourceMachineCostDetailAPI,
    ResourceOverheadListCreateAPI, ResourceOverheadDetailAPI,
    # Cost
    RunCostSummaryAPI, CostAnalyticsAPI,
    # QC
    InProcessQCListCreateAPI, InProcessQCDetailAPI, FinalQCCheckAPI,
    # Extended Analytics
    OEEAnalyticsAPI, DowntimeAnalyticsAPI, WasteAnalyticsAPI,
    # Phase 1 Reports
    ResourceConsumptionReportAPI, MonthlySummaryReportAPI,
    PlanVsProductionReportAPI, ProcurementVsPlannedReportAPI,
    # Phase 2 Reports
    OEETrendReportAPI, DowntimeParetoReportAPI,
    CostAnalysisReportAPI, WasteTrendReportAPI,
    # Line SKU Config
    LineSkuConfigListCreateAPI, LineSkuConfigDetailAPI,
    LineSkuConfigAutoFillAPI,
)

urlpatterns = [
    # ------------------------------------------------------------------
    # Master Data — Production Lines
    # ------------------------------------------------------------------
    path('lines/', LineListCreateAPI.as_view(), name='pe-line-list-create'),
    path('lines/<int:line_id>/', LineDetailAPI.as_view(), name='pe-line-detail'),

    # ------------------------------------------------------------------
    # Master Data — Machines
    # ------------------------------------------------------------------
    path('machines/', MachineListCreateAPI.as_view(), name='pe-machine-list-create'),
    path('machines/<int:machine_id>/', MachineDetailAPI.as_view(), name='pe-machine-detail'),

    # ------------------------------------------------------------------
    # Master Data — Checklist Templates
    # ------------------------------------------------------------------
    path('checklist-templates/', ChecklistTemplateListCreateAPI.as_view(), name='pe-checklist-template-list-create'),
    path('checklist-templates/<int:template_id>/', ChecklistTemplateDetailAPI.as_view(), name='pe-checklist-template-detail'),

    # ------------------------------------------------------------------
    # Production Runs
    # ------------------------------------------------------------------
    path('runs/', RunListCreateAPI.as_view(), name='pe-run-list-create'),
    path('runs/<int:run_id>/', RunDetailAPI.as_view(), name='pe-run-detail'),
    path('runs/<int:run_id>/complete/', CompleteRunAPI.as_view(), name='pe-run-complete'),
    path('runs/<int:run_id>/retry-sap-receipt/', RetrySAPGoodsReceiptAPI.as_view(), name='pe-run-retry-sap'),

    # ------------------------------------------------------------------
    # Breakdown Categories
    # ------------------------------------------------------------------
    path('breakdown-categories/', BreakdownCategoryListCreateAPI.as_view(), name='pe-breakdown-category-list'),
    path('breakdown-categories/<int:category_id>/', BreakdownCategoryDetailAPI.as_view(), name='pe-breakdown-category-detail'),

    # ------------------------------------------------------------------
    # Timeline Actions
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/start-production/', StartProductionAPI.as_view(), name='pe-start-production'),
    path('runs/<int:run_id>/stop-production/', StopProductionAPI.as_view(), name='pe-stop-production'),
    path('runs/<int:run_id>/add-breakdown/', AddBreakdownAPI.as_view(), name='pe-add-breakdown'),
    path('runs/<int:run_id>/breakdowns/<int:breakdown_id>/resolve/', ResolveBreakdownAPI.as_view(), name='pe-resolve-breakdown'),
    path('runs/<int:run_id>/segments/<int:segment_id>/', SegmentUpdateAPI.as_view(), name='pe-segment-update'),
    path('runs/<int:run_id>/breakdowns/<int:breakdown_id>/update/', BreakdownUpdateAPI.as_view(), name='pe-breakdown-update'),

    # ------------------------------------------------------------------
    # Machine Breakdowns
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/breakdowns/', BreakdownListCreateAPI.as_view(), name='pe-breakdown-list-create'),
    path('runs/<int:run_id>/breakdowns/<int:breakdown_id>/', BreakdownDetailAPI.as_view(), name='pe-breakdown-detail'),

    # ------------------------------------------------------------------
    # Material Usage (Yield)
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/materials/', MaterialUsageListCreateAPI.as_view(), name='pe-material-list-create'),
    path('runs/<int:run_id>/materials/<int:material_id>/', MaterialUsageDetailAPI.as_view(), name='pe-material-detail'),

    # ------------------------------------------------------------------
    # Machine Runtime
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/machine-runtime/', MachineRuntimeListCreateAPI.as_view(), name='pe-runtime-list-create'),
    path('runs/<int:run_id>/machine-runtime/<int:runtime_id>/', MachineRuntimeDetailAPI.as_view(), name='pe-runtime-detail'),

    # ------------------------------------------------------------------
    # Manpower
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/manpower/', ManpowerListCreateAPI.as_view(), name='pe-manpower-list-create'),
    path('runs/<int:run_id>/manpower/<int:manpower_id>/', ManpowerDetailAPI.as_view(), name='pe-manpower-detail'),

    # ------------------------------------------------------------------
    # Line Clearance
    # ------------------------------------------------------------------
    path('line-clearance/', LineClearanceListCreateAPI.as_view(), name='pe-clearance-list-create'),
    path('line-clearance/<int:clearance_id>/', LineClearanceDetailAPI.as_view(), name='pe-clearance-detail'),
    path('line-clearance/<int:clearance_id>/submit/', SubmitClearanceAPI.as_view(), name='pe-clearance-submit'),
    path('line-clearance/<int:clearance_id>/approve/', ApproveClearanceAPI.as_view(), name='pe-clearance-approve'),

    # ------------------------------------------------------------------
    # Machine Checklists
    # ------------------------------------------------------------------
    path('machine-checklists/', MachineChecklistListCreateAPI.as_view(), name='pe-checklist-list-create'),
    path('machine-checklists/bulk/', MachineChecklistBulkAPI.as_view(), name='pe-checklist-bulk'),
    path('machine-checklists/<int:entry_id>/', MachineChecklistDetailAPI.as_view(), name='pe-checklist-detail'),

    # ------------------------------------------------------------------
    # Waste Management
    # ------------------------------------------------------------------
    path('waste/', WasteLogListCreateAPI.as_view(), name='pe-waste-list-create'),
    path('waste/<int:waste_id>/', WasteLogDetailAPI.as_view(), name='pe-waste-detail'),
    path('waste/<int:waste_id>/approve/engineer/', WasteApproveEngineerAPI.as_view(), name='pe-waste-approve-engineer'),
    path('waste/<int:waste_id>/approve/am/', WasteApproveAMAPI.as_view(), name='pe-waste-approve-am'),
    path('waste/<int:waste_id>/approve/store/', WasteApproveStoreAPI.as_view(), name='pe-waste-approve-store'),
    path('waste/<int:waste_id>/approve/hod/', WasteApproveHODAPI.as_view(), name='pe-waste-approve-hod'),

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    path('reports/daily-production/', DailyProductionReportAPI.as_view(), name='pe-report-daily'),
    path('reports/yield/<int:run_id>/', YieldReportAPI.as_view(), name='pe-report-yield'),
    path('reports/line-clearance/', LineClearanceReportAPI.as_view(), name='pe-report-clearance'),
    path('reports/analytics/', AnalyticsAPI.as_view(), name='pe-report-analytics'),

    # ------------------------------------------------------------------
    # SAP Orders (proxy)
    # ------------------------------------------------------------------
    path('sap/orders/', SAPProductionOrderListAPI.as_view(), name='pe-sap-orders'),
    path('sap/orders/<int:doc_entry>/', SAPProductionOrderDetailAPI.as_view(), name='pe-sap-order-detail'),
    path('sap/items/', SAPItemSearchAPI.as_view(), name='pe-sap-items'),
    path('sap/bom/', SAPItemBOMAPI.as_view(), name='pe-sap-item-bom'),

    # ------------------------------------------------------------------
    # Resource Tracking — Electricity
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/electricity/', ResourceElectricityListCreateAPI.as_view(), name='pe-resource-electricity-list'),
    path('runs/<int:run_id>/resources/electricity/<int:entry_id>/', ResourceElectricityDetailAPI.as_view(), name='pe-resource-electricity-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Water
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/water/', ResourceWaterListCreateAPI.as_view(), name='pe-resource-water-list'),
    path('runs/<int:run_id>/resources/water/<int:entry_id>/', ResourceWaterDetailAPI.as_view(), name='pe-resource-water-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Gas
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/gas/', ResourceGasListCreateAPI.as_view(), name='pe-resource-gas-list'),
    path('runs/<int:run_id>/resources/gas/<int:entry_id>/', ResourceGasDetailAPI.as_view(), name='pe-resource-gas-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Compressed Air
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/compressed-air/', ResourceCompressedAirListCreateAPI.as_view(), name='pe-resource-air-list'),
    path('runs/<int:run_id>/resources/compressed-air/<int:entry_id>/', ResourceCompressedAirDetailAPI.as_view(), name='pe-resource-air-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Labour
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/labour/', ResourceLabourListCreateAPI.as_view(), name='pe-resource-labour-list'),
    path('runs/<int:run_id>/resources/labour/<int:entry_id>/', ResourceLabourDetailAPI.as_view(), name='pe-resource-labour-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Machine Costs
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/machine-costs/', ResourceMachineCostListCreateAPI.as_view(), name='pe-resource-machine-list'),
    path('runs/<int:run_id>/resources/machine-costs/<int:entry_id>/', ResourceMachineCostDetailAPI.as_view(), name='pe-resource-machine-detail'),

    # ------------------------------------------------------------------
    # Resource Tracking — Overhead
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/resources/overhead/', ResourceOverheadListCreateAPI.as_view(), name='pe-resource-overhead-list'),
    path('runs/<int:run_id>/resources/overhead/<int:entry_id>/', ResourceOverheadDetailAPI.as_view(), name='pe-resource-overhead-detail'),

    # ------------------------------------------------------------------
    # Cost Summary
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/cost/', RunCostSummaryAPI.as_view(), name='pe-run-cost'),
    path('costs/analytics/', CostAnalyticsAPI.as_view(), name='pe-cost-analytics'),

    # ------------------------------------------------------------------
    # QC Checks
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/qc/inprocess/', InProcessQCListCreateAPI.as_view(), name='pe-qc-inprocess-list'),
    path('runs/<int:run_id>/qc/inprocess/<int:check_id>/', InProcessQCDetailAPI.as_view(), name='pe-qc-inprocess-detail'),
    path('runs/<int:run_id>/qc/final/', FinalQCCheckAPI.as_view(), name='pe-qc-final'),

    # ------------------------------------------------------------------
    # Extended Analytics
    # ------------------------------------------------------------------
    path('reports/analytics/oee/', OEEAnalyticsAPI.as_view(), name='pe-analytics-oee'),
    path('reports/analytics/downtime/', DowntimeAnalyticsAPI.as_view(), name='pe-analytics-downtime'),
    path('reports/analytics/waste/', WasteAnalyticsAPI.as_view(), name='pe-analytics-waste'),

    # ------------------------------------------------------------------
    # Phase 1 Reports
    # ------------------------------------------------------------------
    path('reports/analytics/resource-consumption/', ResourceConsumptionReportAPI.as_view(), name='pe-analytics-resource-consumption'),
    path('reports/analytics/monthly-summary/', MonthlySummaryReportAPI.as_view(), name='pe-analytics-monthly-summary'),
    path('reports/analytics/plan-vs-production/', PlanVsProductionReportAPI.as_view(), name='pe-analytics-plan-vs-production'),
    path('reports/analytics/procurement-vs-planned/', ProcurementVsPlannedReportAPI.as_view(), name='pe-analytics-procurement-vs-planned'),

    # ------------------------------------------------------------------
    # Phase 2 Reports
    # ------------------------------------------------------------------
    path('reports/analytics/oee-trend/', OEETrendReportAPI.as_view(), name='pe-analytics-oee-trend'),
    path('reports/analytics/downtime-pareto/', DowntimeParetoReportAPI.as_view(), name='pe-analytics-downtime-pareto'),
    path('reports/analytics/cost-analysis/', CostAnalysisReportAPI.as_view(), name='pe-analytics-cost-analysis'),
    path('reports/analytics/waste-trend/', WasteTrendReportAPI.as_view(), name='pe-analytics-waste-trend'),

    # ------------------------------------------------------------------
    # Line SKU Config
    # ------------------------------------------------------------------
    path('line-configs/', LineSkuConfigListCreateAPI.as_view(), name='pe-line-config-list-create'),
    path('line-configs/<int:config_id>/', LineSkuConfigDetailAPI.as_view(), name='pe-line-config-detail'),
    path('line-configs/auto-fill/', LineSkuConfigAutoFillAPI.as_view(), name='pe-line-config-autofill'),
]
