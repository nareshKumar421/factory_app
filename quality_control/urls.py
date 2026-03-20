# quality_control/urls.py

from django.urls import path
from .views import (
    # Material Type APIs
    MaterialTypeListCreateAPI,
    MaterialTypeDetailAPI,
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
)
from .views_production_qc import (
    ProductionQCSessionListCreateAPI,
    ProductionQCSessionDetailAPI,
    ProductionQCResultsAPI,
    ProductionQCSubmitAPI,
    ProductionQCAllListAPI,
    ProductionQCCountsAPI,
)

urlpatterns = [
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
        "inspections/<int:inspection_id>/approve/qam/",
        InspectionApproveQAMAPI.as_view(),
        name="inspection-approve-qam"
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
        "production-qc/runs/<int:run_id>/sessions/",
        ProductionQCSessionListCreateAPI.as_view(),
        name="production-qc-session-list-create"
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
]
