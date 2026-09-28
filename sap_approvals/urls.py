from django.urls import path

from .views import (
    ApprovalRequestAttachmentDownloadAPI,
    ApprovalRequestAttachmentLinesAPI,
    ApprovalRequestDecisionAPI,
    ApprovalRequestDetailAPI,
    ApprovalRequestDocumentAPI,
    ApprovalRequestListAPI,
    ApprovalRequestWithdrawAPI,
    PendingCountAPI,
)

urlpatterns = [
    path("requests/", ApprovalRequestListAPI.as_view(), name="sap-approvals-requests"),
    path("requests/<int:wdd_code>/", ApprovalRequestDetailAPI.as_view(), name="sap-approvals-request"),
    path(
        "requests/<int:wdd_code>/decision/",
        ApprovalRequestDecisionAPI.as_view(),
        name="sap-approvals-decision",
    ),
    path(
        "requests/<int:wdd_code>/withdraw/",
        ApprovalRequestWithdrawAPI.as_view(),
        name="sap-approvals-withdraw",
    ),
    # The draft in full, and its attachments, for someone on the request.
    path(
        "requests/<int:wdd_code>/document/",
        ApprovalRequestDocumentAPI.as_view(),
        name="sap-approvals-document",
    ),
    path(
        "requests/<int:wdd_code>/attachments/<int:abs_entry>/",
        ApprovalRequestAttachmentLinesAPI.as_view(),
        name="sap-approvals-attachment-lines",
    ),
    path(
        "requests/<int:wdd_code>/attachments/<int:abs_entry>/<int:line>/download/",
        ApprovalRequestAttachmentDownloadAPI.as_view(),
        name="sap-approvals-attachment-download",
    ),
    path("pending-count/", PendingCountAPI.as_view(), name="sap-approvals-pending-count"),
]
