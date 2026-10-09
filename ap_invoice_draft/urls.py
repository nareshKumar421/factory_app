from django.urls import path

from .views import (
    APInvoiceDraftDetailAPI,
    APInvoiceDraftListCreateAPI,
    APInvoiceDraftSendToSapAPI,
    OpenGRPOListAPI,
)

urlpatterns = [
    # Static routes first, so they are not swallowed as an <int:pk>.
    path("grpos/", OpenGRPOListAPI.as_view(), name="ap-invoice-draft-grpos"),

    path("", APInvoiceDraftListCreateAPI.as_view(), name="ap-invoice-draft-list-create"),
    path("<int:pk>/", APInvoiceDraftDetailAPI.as_view(), name="ap-invoice-draft-detail"),
    path("<int:pk>/send-to-sap/", APInvoiceDraftSendToSapAPI.as_view(), name="ap-invoice-draft-send"),
]
