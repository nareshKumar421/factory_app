from django.urls import path

from .views import (
    APInvoiceDraftCheckReviewAPI,
    APInvoiceDraftDetailAPI,
    APInvoiceDraftListCreateAPI,
    APInvoiceDraftReadInvoiceAPI,
    APInvoiceDraftRecheckAPI,
    APInvoiceDraftSendToSapAPI,
    GRPOAPStatusAPI,
    OpenGRPOListAPI,
)

urlpatterns = [
    # Static routes first, so they are not swallowed as an <int:pk>.
    path("grpos/", OpenGRPOListAPI.as_view(), name="ap-invoice-draft-grpos"),
    path("grpo-status/", GRPOAPStatusAPI.as_view(), name="ap-invoice-draft-grpo-status"),

    path("", APInvoiceDraftListCreateAPI.as_view(), name="ap-invoice-draft-list-create"),
    path("<int:pk>/", APInvoiceDraftDetailAPI.as_view(), name="ap-invoice-draft-detail"),
    path("<int:pk>/read-invoice/", APInvoiceDraftReadInvoiceAPI.as_view(), name="ap-invoice-draft-read"),
    path("<int:pk>/send-to-sap/", APInvoiceDraftSendToSapAPI.as_view(), name="ap-invoice-draft-send"),
    path("<int:pk>/recheck/", APInvoiceDraftRecheckAPI.as_view(), name="ap-invoice-draft-recheck"),
    path(
        "<int:pk>/checks/<str:key>/review/",
        APInvoiceDraftCheckReviewAPI.as_view(),
        name="ap-invoice-draft-check-review",
    ),
]
