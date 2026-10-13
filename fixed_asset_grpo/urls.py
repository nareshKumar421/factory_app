"""Fixed Asset GRPO routes. Mounted by the lead at ``api/v1/fixed-asset-grpo/``."""

from django.urls import path

from . import views

urlpatterns = [
    # -- lists ---------------------------------------------------------------
    path("summary/", views.FAGRPODashboardSummaryAPI.as_view(), name="fixed-asset-grpo-summary"),
    path("all-entries/", views.FAAllEntriesListAPI.as_view(), name="fixed-asset-grpo-all-entries"),
    path("pending/", views.FAPendingGRPOListAPI.as_view(), name="fixed-asset-grpo-pending"),
    path("needs-po/", views.FANeedsPOListAPI.as_view(), name="fixed-asset-grpo-needs-po"),
    path("history/", views.FAGRPOPostingHistoryAPI.as_view(), name="fixed-asset-grpo-history"),
    path(
        "history/<int:posting_id>/",
        views.FAGRPOPostingDetailAPI.as_view(),
        name="fixed-asset-grpo-history-detail",
    ),
    path(
        "history/<int:posting_id>/print/",
        views.FAGRPOPrintAPI.as_view(),
        name="fixed-asset-grpo-print",
    ),
    path(
        "history/<int:posting_id>/discard-simulated/",
        views.FAGRPODiscardSimulatedAPI.as_view(),
        name="fixed-asset-grpo-discard-simulated",
    ),

    # -- one entry -----------------------------------------------------------
    path(
        "entries/<int:vehicle_entry_id>/",
        views.FAEntryDetailAPI.as_view(),
        name="fixed-asset-grpo-entry",
    ),
    path(
        "entries/<int:vehicle_entry_id>/po-receipts/",
        views.FAPOReceiptListCreateAPI.as_view(),
        name="fixed-asset-grpo-po-receipts",
    ),
    path(
        "entries/<int:vehicle_entry_id>/po-receipts/<int:po_receipt_id>/",
        views.FAPOReceiptDetailAPI.as_view(),
        name="fixed-asset-grpo-po-receipt-detail",
    ),
    path(
        "entries/<int:vehicle_entry_id>/open-pos/",
        views.FAOpenPOListAPI.as_view(),
        name="fixed-asset-grpo-open-pos",
    ),
    path(
        "entries/<int:vehicle_entry_id>/preview/",
        views.FAGRPOPreviewAPI.as_view(),
        name="fixed-asset-grpo-preview",
    ),
    path(
        "entries/<int:vehicle_entry_id>/close/",
        views.FAEntryCloseAPI.as_view(),
        name="fixed-asset-grpo-entry-close",
    ),

    # -- posting -------------------------------------------------------------
    path("post/", views.FAPostGRPOAPI.as_view(), name="fixed-asset-grpo-post"),
    path("drafts/<int:posting_id>/", views.FAGRPODraftAPI.as_view(), name="fixed-asset-grpo-draft"),
]
