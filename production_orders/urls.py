from django.urls import path

from .views import (
    EntryDetailAPI,
    EntryListCreateAPI,
    EntryStepAPI,
    EntryUnreleaseAPI,
    MeAPI,
    PlanPreviewAPI,
    ProductSearchAPI,
    ReceiptPreviewAPI,
    SapOrdersAPI,
    VarietiesAPI,
)

urlpatterns = [
    path("me/", MeAPI.as_view(), name="production-orders-me"),
    path("products/", ProductSearchAPI.as_view(), name="production-orders-products"),
    path("varieties/", VarietiesAPI.as_view(), name="production-orders-varieties"),
    path("plan-preview/", PlanPreviewAPI.as_view(), name="production-orders-plan-preview"),
    path("sap-orders/", SapOrdersAPI.as_view(), name="production-orders-sap-orders"),
    path("entries/", EntryListCreateAPI.as_view(), name="production-orders-entries"),
    path("entries/<int:pk>/", EntryDetailAPI.as_view(), name="production-orders-entry"),
    path(
        "entries/<int:pk>/receipt/preview/",
        ReceiptPreviewAPI.as_view(),
        name="production-orders-receipt-preview",
    ),
    path(
        "entries/<int:pk>/unrelease/",
        EntryUnreleaseAPI.as_view(),
        name="production-orders-entry-unrelease",
    ),
    path("entries/<int:pk>/<slug:step>/", EntryStepAPI.as_view(), name="production-orders-entry-step"),
]
