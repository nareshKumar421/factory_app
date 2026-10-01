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
    QCPrintDocumentOptionsAPI,
    # QC Parameter Set APIs
    QCParameterSetListCreateAPI,
    QCParameterSetDetailAPI,
    QCParameterSetCopyAPI,
    # QC Parameter Master APIs
    QCParameterListCreateAPI,
    MaterialTypeDefaultParameterListCreateAPI,
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
from .views_production_qc import (
    ProductionQCEntryListCreateAPI,
    ProductionQCEntryCountsAPI,
    ProductionQCEntryDetailAPI,
    ProductionQCEntryApproveAPI,
    ProductionQCEntrySendBackAPI,
    ProductionParameterTypeListCreateAPI,
    ProductionParameterTypeDetailAPI,
    ProductionParameterListCreateAPI,
    ProductionParameterDetailAPI,
)
from .views_qc_document_file import (
    QCDocumentFileListCreateAPI,
    QCDocumentFileDetailAPI,
    QCDocumentFileDownloadAPI,
)

from .views_qc_document_file_audit import (
    QCDocumentFileAuditLogAPI,
    QCDocumentFileAuditFilterOptionsAPI,
)

urlpatterns = [
    # ==================== QA Reports ("production QC" in code) ====================
    path("production-qc/entries/", ProductionQCEntryListCreateAPI.as_view(),
         name="production-qc-entries"),
    path("production-qc/entries/counts/", ProductionQCEntryCountsAPI.as_view(),
         name="production-qc-entry-counts"),
    path("production-qc/entries/<int:entry_id>/", ProductionQCEntryDetailAPI.as_view(),
         name="production-qc-entry-detail"),
    path("production-qc/entries/<int:entry_id>/approve/", ProductionQCEntryApproveAPI.as_view(),
         name="production-qc-entry-approve"),
    path("production-qc/entries/<int:entry_id>/send-back/", ProductionQCEntrySendBackAPI.as_view(),
         name="production-qc-entry-send-back"),
    path("production-qc/parameter-types/", ProductionParameterTypeListCreateAPI.as_view(),
         name="production-qc-parameter-types"),
    path("production-qc/parameter-types/<int:type_id>/", ProductionParameterTypeDetailAPI.as_view(),
         name="production-qc-parameter-type-detail"),
    path("production-qc/parameter-types/<int:type_id>/parameters/",
         ProductionParameterListCreateAPI.as_view(), name="production-qc-parameters"),
    path("production-qc/parameters/<int:parameter_id>/", ProductionParameterDetailAPI.as_view(),
         name="production-qc-parameter-detail"),

    # ==================== QC PDF Document Library ====================
    path(
        "document-files/",
        QCDocumentFileListCreateAPI.as_view(),
        name="qc-document-file-list-create"
    ),
    # Before the <int:document_id> routes: "audit-log" is not an int, so it
    # would not match them anyway, but keeping the literal paths first makes
    # that independent of how the converter behaves.
    path(
        "document-files/audit-log/",
        QCDocumentFileAuditLogAPI.as_view(),
        name="qc-document-file-audit-log"
    ),
    path(
        "document-files/audit-log/filters/",
        QCDocumentFileAuditFilterOptionsAPI.as_view(),
        name="qc-document-file-audit-filters"
    ),
    path(
        "document-files/<int:document_id>/audit-log/",
        QCDocumentFileAuditLogAPI.as_view(),
        name="qc-document-file-audit-log-detail"
    ),
    path(
        "document-files/<int:document_id>/",
        QCDocumentFileDetailAPI.as_view(),
        name="qc-document-file-detail"
    ),
    path(
        "document-files/<int:document_id>/download/",
        QCDocumentFileDownloadAPI.as_view(),
        name="qc-document-file-download"
    ),

    # ==================== QC Print Document APIs ====================
    path(
        "print-documents/",
        QCPrintDocumentListCreateAPI.as_view(),
        name="qc-print-document-list-create"
    ),
    path(
        "print-documents/options/",
        QCPrintDocumentOptionsAPI.as_view(),
        name="qc-print-document-options"
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

    # ==================== QC Parameter Set APIs ====================
    path(
        "material-types/<int:material_type_id>/parameter-sets/",
        QCParameterSetListCreateAPI.as_view(),
        name="qc-parameter-set-list-create"
    ),
    path(
        "parameter-sets/<int:parameter_set_id>/",
        QCParameterSetDetailAPI.as_view(),
        name="qc-parameter-set-detail"
    ),
    path(
        "parameter-sets/<int:parameter_set_id>/copy-parameters/",
        QCParameterSetCopyAPI.as_view(),
        name="qc-parameter-set-copy"
    ),

    # ==================== QC Parameter Master APIs ====================
    path(
        "parameter-sets/<int:parameter_set_id>/parameters/",
        QCParameterListCreateAPI.as_view(),
        name="qc-parameter-set-parameter-list-create"
    ),
    # Kept for existing clients: reads/writes the material type's default set.
    path(
        "material-types/<int:material_type_id>/parameters/",
        MaterialTypeDefaultParameterListCreateAPI.as_view(),
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
]
