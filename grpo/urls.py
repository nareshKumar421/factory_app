from django.urls import path
from .views import (
    AllGRPOEntriesListAPI,
    PendingGRPOListAPI,
    GRPOPreviewAPI,
    PostGRPOAPI,
    GRPOPostingHistoryAPI,
    GRPOPostingDetailAPI,
    GRPOAttachmentListCreateAPI,
    GRPOAttachmentDeleteAPI,
    GRPOAttachmentRetryAPI,
)

urlpatterns = [
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
