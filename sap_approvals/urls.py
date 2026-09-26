from django.urls import path

from .views import (
    ApprovalRequestDecisionAPI,
    ApprovalRequestDetailAPI,
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
    path("pending-count/", PendingCountAPI.as_view(), name="sap-approvals-pending-count"),
]
