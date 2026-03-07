from django.urls import path
from .views import (
    # Master Data
    LineListCreateAPI, LineDetailAPI,
    MachineListCreateAPI, MachineDetailAPI,
    ChecklistTemplateListCreateAPI, ChecklistTemplateDetailAPI,
    # Production Runs
    RunListCreateAPI, RunDetailAPI, CompleteRunAPI,
    # Hourly Logs
    RunLogListCreateAPI, RunLogDetailAPI,
    # Breakdowns
    BreakdownListCreateAPI, BreakdownDetailAPI,
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

    # ------------------------------------------------------------------
    # Hourly Production Logs
    # ------------------------------------------------------------------
    path('runs/<int:run_id>/logs/', RunLogListCreateAPI.as_view(), name='pe-run-log-list-create'),
    path('runs/<int:run_id>/logs/<int:log_id>/', RunLogDetailAPI.as_view(), name='pe-run-log-detail'),

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
]
