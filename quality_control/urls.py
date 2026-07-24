# quality_control/urls.py

from django.urls import path
from .views import (
    # Material Type APIs
    MaterialTypeListCreateAPI,
    MaterialTypeDetailAPI,
    MaterialTypeBySAPItemAPI,
    MaterialTypeSAPItemLinkAPI,
    SAPItemSearchAPI,
    QCPrintDocumentListCreateAPI,
    QCPrintDocumentDetailAPI,
    # QC Parameter Master APIs
    QCParameterListCreateAPI,
    QCParameterDetailAPI,
    # Material Arrival Slip APIs
    ArrivalSlipListAPI,
    ArrivalSlipCreateUpdateAPI,
    ArrivalSlipDetailAPI,
    ArrivalSlipSubmitAPI,
    ArrivalSlipSendBackAPI,
    # Raw Material Inspection APIs
    InspectionPendingListAPI,
    InspectionCreateUpdateAPI,
    InspectionDetailAPI,
    InspectionParameterResultsAPI,
    InspectionSubmitAPI,
    # Approval APIs
    InspectionApproveChemistAPI,
    InspectionApproveQAMAPI,
    InspectionRejectAPI,
    # Inspection List APIs (Status-Based)
    InspectionListAPI,
    InspectionDraftListAPI,
    InspectionActionableListAPI,
    InspectionCountsAPI,
    InspectionAwaitingChemistAPI,
    InspectionAwaitingQAMAPI,
    InspectionCompletedAPI,
    InspectionRejectedAPI,
    InspectionReturnToVendorAPI,
    InspectionDecisionChangedAPI,
)
from .views_online_monitoring import (
    OnlineMonitoringLinesAPI,
    OnlineMonitoringListCreateAPI,
    OnlineMonitoringDetailAPI,
    OnlineMonitoringReadingCreateAPI,
    OnlineMonitoringReadingDetailAPI,
    OnlineMonitoringSubmitAPI,
    OnlineMonitoringApproveAPI,
    OnlineMonitoringRejectAPI,
    OnlineMonitoringReopenAPI,
    OnlineMonitoringSpecListAPI,
    OnlineMonitoringSpecDetailAPI,
)
from .views_production_qc import (
    ProductionQCSessionListCreateAPI,
    ProductionQCFinalRequestAPI,
    ProductionQCSessionDetailAPI,
    ProductionQCResultsAPI,
    ProductionQCSubmitAPI,
    ProductionQCPendingAPI,
    ProductionQCRunningRunsAPI,
    ProductionQCApproveAPI,
    ProductionQCRejectAPI,
    ProductionQCAllListAPI,
    ProductionQCCountsAPI,
)

urlpatterns = [
    # ==================== QC Print Document APIs ====================
    path(
        "print-documents/",
        QCPrintDocumentListCreateAPI.as_view(),
        name="qc-print-document-list-create"
    ),
    path(
        "print-documents/<int:document_id>/",
        QCPrintDocumentDetailAPI.as_view(),
        name="qc-print-document-detail"
    ),

    # ==================== Material Type APIs ====================
    path(
        "material-types/",
        MaterialTypeListCreateAPI.as_view(),
        name="material-type-list-create"
    ),
    path(
        "material-types/<int:material_type_id>/",
        MaterialTypeDetailAPI.as_view(),
        name="material-type-detail"
    ),
    path(
        "material-types/by-sap-item/<str:item_code>/",
        MaterialTypeBySAPItemAPI.as_view(),
        name="material-type-by-sap-item"
    ),
    path(
        "material-types/link-sap-item/",
        MaterialTypeSAPItemLinkAPI.as_view(),
        name="material-type-link-sap-item"
    ),
    path(
        "sap-items/",
        SAPItemSearchAPI.as_view(),
        name="qc-sap-item-search"
    ),

    # ==================== QC Parameter Master APIs ====================
    path(
        "material-types/<int:material_type_id>/parameters/",
        QCParameterListCreateAPI.as_view(),
        name="qc-parameter-list-create"
    ),
    path(
        "parameters/<int:parameter_id>/",
        QCParameterDetailAPI.as_view(),
        name="qc-parameter-detail"
    ),

    # ==================== Material Arrival Slip APIs ====================
    path(
        "arrival-slips/",
        ArrivalSlipListAPI.as_view(),
        name="arrival-slip-list"
    ),
    path(
        "po-items/<int:po_item_id>/arrival-slip/",
        ArrivalSlipCreateUpdateAPI.as_view(),
        name="arrival-slip-create-update"
    ),
    path(
        "arrival-slips/<int:slip_id>/",
        ArrivalSlipDetailAPI.as_view(),
        name="arrival-slip-detail"
    ),
    path(
        "arrival-slips/<int:slip_id>/submit/",
        ArrivalSlipSubmitAPI.as_view(),
        name="arrival-slip-submit"
    ),
    path(
        "arrival-slips/<int:slip_id>/send-back/",
        ArrivalSlipSendBackAPI.as_view(),
        name="arrival-slip-send-back"
    ),

    # ==================== Raw Material Inspection APIs ====================
    # List all inspections (with optional filters)
    path(
        "inspections/",
        InspectionListAPI.as_view(),
        name="inspection-list"
    ),
    # List by workflow stage
    path(
        "inspections/pending/",
        InspectionPendingListAPI.as_view(),
        name="inspection-pending-list"
    ),
    path(
        "inspections/draft/",
        InspectionDraftListAPI.as_view(),
        name="inspection-draft-list"
    ),
    path(
        "inspections/actionable/",
        InspectionActionableListAPI.as_view(),
        name="inspection-actionable-list"
    ),
    path(
        "inspections/counts/",
        InspectionCountsAPI.as_view(),
        name="inspection-counts"
    ),
    path(
        "inspections/awaiting-chemist/",
        InspectionAwaitingChemistAPI.as_view(),
        name="inspection-awaiting-chemist"
    ),
    path(
        "inspections/awaiting-qam/",
        InspectionAwaitingQAMAPI.as_view(),
        name="inspection-awaiting-qam"
    ),
    path(
        "inspections/completed/",
        InspectionCompletedAPI.as_view(),
        name="inspection-completed"
    ),
    path(
        "inspections/rejected/",
        InspectionRejectedAPI.as_view(),
        name="inspection-rejected"
    ),
    path(
        "inspections/return-to-vendor/",
        InspectionReturnToVendorAPI.as_view(),
        name="inspection-return-to-vendor"
    ),
    path(
        "inspections/decision-changed/",
        InspectionDecisionChangedAPI.as_view(),
        name="inspection-decision-changed"
    ),
    # Create/update inspection for an arrival slip
    path(
        "arrival-slips/<int:slip_id>/inspection/",
        InspectionCreateUpdateAPI.as_view(),
        name="inspection-create-update"
    ),
    # Get inspection by ID (must be after named paths)
    path(
        "inspections/<int:inspection_id>/",
        InspectionDetailAPI.as_view(),
        name="inspection-detail"
    ),
    # Update parameter results
    path(
        "inspections/<int:inspection_id>/parameters/",
        InspectionParameterResultsAPI.as_view(),
        name="inspection-parameters"
    ),
    # Submit inspection for approval
    path(
        "inspections/<int:inspection_id>/submit/",
        InspectionSubmitAPI.as_view(),
        name="inspection-submit"
    ),

    # ==================== Approval APIs ====================
    path(
        "inspections/<int:inspection_id>/approve/chemist/",
        InspectionApproveChemistAPI.as_view(),
        name="inspection-approve-chemist"
    ),
    path(
        "inspections/<int:inspection_id>/chemist-decision/",
        InspectionApproveChemistAPI.as_view(),
        name="inspection-chemist-decision"
    ),
    path(
        "inspections/<int:inspection_id>/approve/qam/",
        InspectionApproveQAMAPI.as_view(),
        name="inspection-approve-qam"
    ),
    path(
        "inspections/<int:inspection_id>/manager-decision/",
        InspectionApproveQAMAPI.as_view(),
        name="inspection-manager-decision"
    ),
    path(
        "inspections/<int:inspection_id>/reject/",
        InspectionRejectAPI.as_view(),
        name="inspection-reject"
    ),

    # ==================== Production QC APIs ====================
    path(
        "production-qc/",
        ProductionQCAllListAPI.as_view(),
        name="production-qc-list"
    ),
    path(
        "production-qc/counts/",
        ProductionQCCountsAPI.as_view(),
        name="production-qc-counts"
    ),
    path(
        "production-qc/pending/",
        ProductionQCPendingAPI.as_view(),
        name="production-qc-pending"
    ),
    path(
        "production-qc/running-runs/",
        ProductionQCRunningRunsAPI.as_view(),
        name="production-qc-running-runs"
    ),
    path(
        "production-qc/runs/<int:run_id>/sessions/",
        ProductionQCSessionListCreateAPI.as_view(),
        name="production-qc-session-list-create"
    ),
    path(
        "production-qc/runs/<int:run_id>/request-final/",
        ProductionQCFinalRequestAPI.as_view(),
        name="production-qc-final-request"
    ),
    path(
        "production-qc/sessions/<int:session_id>/",
        ProductionQCSessionDetailAPI.as_view(),
        name="production-qc-session-detail"
    ),
    path(
        "production-qc/sessions/<int:session_id>/results/",
        ProductionQCResultsAPI.as_view(),
        name="production-qc-results"
    ),
    path(
        "production-qc/sessions/<int:session_id>/submit/",
        ProductionQCSubmitAPI.as_view(),
        name="production-qc-submit"
    ),
    path(
        "production-qc/sessions/<int:session_id>/approve/",
        ProductionQCApproveAPI.as_view(),
        name="production-qc-approve"
    ),
    path(
        "production-qc/sessions/<int:session_id>/reject/",
        ProductionQCRejectAPI.as_view(),
        name="production-qc-reject"
    ),

    # ==================== Online Quality Monitoring APIs ====================
    path("online-monitoring/", OnlineMonitoringListCreateAPI.as_view(),
         name="online-monitoring-list"),
    path("online-monitoring/lines/", OnlineMonitoringLinesAPI.as_view(),
         name="online-monitoring-lines"),
    path("online-monitoring/specs/", OnlineMonitoringSpecListAPI.as_view(),
         name="online-monitoring-specs"),
    path("online-monitoring/specs/<int:spec_id>/", OnlineMonitoringSpecDetailAPI.as_view(),
         name="online-monitoring-spec-detail"),
    path("online-monitoring/<int:record_id>/", OnlineMonitoringDetailAPI.as_view(),
         name="online-monitoring-detail"),
    path("online-monitoring/<int:record_id>/readings/", OnlineMonitoringReadingCreateAPI.as_view(),
         name="online-monitoring-reading-create"),
    path("online-monitoring/<int:record_id>/readings/<int:reading_id>/",
         OnlineMonitoringReadingDetailAPI.as_view(), name="online-monitoring-reading-detail"),
    path("online-monitoring/<int:record_id>/submit/", OnlineMonitoringSubmitAPI.as_view(),
         name="online-monitoring-submit"),
    path("online-monitoring/<int:record_id>/approve/", OnlineMonitoringApproveAPI.as_view(),
         name="online-monitoring-approve"),
    path("online-monitoring/<int:record_id>/reject/", OnlineMonitoringRejectAPI.as_view(),
         name="online-monitoring-reject"),
    path("online-monitoring/<int:record_id>/reopen/", OnlineMonitoringReopenAPI.as_view(),
         name="online-monitoring-reopen"),
]
