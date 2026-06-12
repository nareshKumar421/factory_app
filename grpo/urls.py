from django.urls import path
from .views import (
    GRPODashboardSummaryAPI,
    AllGRPOEntriesListAPI,
    PendingGRPOListAPI,
    GRPOPreviewAPI,
    PostGRPOAPI,
    GRPOPostingHistoryAPI,
    GRPOPostingDetailAPI,
    GRPOInspectionReportAPI,
    GRPOAttachmentListCreateAPI,
    GRPOAttachmentDeleteAPI,
    GRPOAttachmentRetryAPI,
    PendingServiceGRPOListAPI,
    ServiceGRPOOptionsAPI,
    ServiceGRPOPreviewAPI,
    PostServiceGRPOAPI,
    ServiceGRPOPostingHistoryAPI,
    ServiceGRPOPostingDetailAPI,
)

urlpatterns = [
    # Material GRPO dashboard insight totals
    path("summary/", GRPODashboardSummaryAPI.as_view(), name="grpo-summary"),

    # List all RAW_MATERIAL gate entries (including in-flight ones)
    path("all-entries/", AllGRPOEntriesListAPI.as_view(), name="grpo-all-entries"),

    # List pending GRPO entries
    path("pending/", PendingGRPOListAPI.as_view(), name="grpo-pending"),

    # Preview GRPO data for a gate entry
    path("preview/<int:vehicle_entry_id>/", GRPOPreviewAPI.as_view(), name="grpo-preview"),

    # Post GRPO to SAP
    path("post/", PostGRPOAPI.as_view(), name="grpo-post"),

    # GRPO posting history
    path("history/", GRPOPostingHistoryAPI.as_view(), name="grpo-history"),

    # QC report payload for direct GRPO printing
    path(
        "inspection-report/<int:arrival_slip_id>/",
        GRPOInspectionReportAPI.as_view(),
        name="grpo-inspection-report",
    ),

    # Service GRPO endpoints for transport bookings
    path(
        "service/pending/",
        PendingServiceGRPOListAPI.as_view(),
        name="service-grpo-pending",
    ),
    path(
        "service/options/",
        ServiceGRPOOptionsAPI.as_view(),
        name="service-grpo-options",
    ),
    path(
        "service/preview/<int:dispatch_plan_id>/",
        ServiceGRPOPreviewAPI.as_view(),
        name="service-grpo-preview",
    ),
    path(
        "service/post/",
        PostServiceGRPOAPI.as_view(),
        name="service-grpo-post",
    ),
    path(
        "service/history/",
        ServiceGRPOPostingHistoryAPI.as_view(),
        name="service-grpo-history",
    ),
    path(
        "service/<int:posting_id>/",
        ServiceGRPOPostingDetailAPI.as_view(),
        name="service-grpo-detail",
    ),

    # GRPO attachment endpoints
    path(
        "<int:posting_id>/attachments/",
        GRPOAttachmentListCreateAPI.as_view(),
        name="grpo-attachment-list-create"
    ),
    path(
        "<int:posting_id>/attachments/<int:attachment_id>/",
        GRPOAttachmentDeleteAPI.as_view(),
        name="grpo-attachment-delete"
    ),
    path(
        "<int:posting_id>/attachments/<int:attachment_id>/retry/",
        GRPOAttachmentRetryAPI.as_view(),
        name="grpo-attachment-retry"
    ),

    # GRPO posting detail (keep last - catch-all pattern)
    path("<int:posting_id>/", GRPOPostingDetailAPI.as_view(), name="grpo-detail"),
]
