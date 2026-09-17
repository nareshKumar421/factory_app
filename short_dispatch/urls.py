from django.urls import path

from .views import (
    ShortDispatchDetailAPI,
    ShortDispatchInvoiceLookupAPI,
    ShortDispatchListCreateAPI,
    ShortDispatchPrintAPI,
    ShortDispatchWarehousesAPI,
)

urlpatterns = [
    # Static routes first, so they are not swallowed as an <int:pk>.
    path("invoice/", ShortDispatchInvoiceLookupAPI.as_view(), name="short-dispatch-invoice"),
    path("warehouses/", ShortDispatchWarehousesAPI.as_view(), name="short-dispatch-warehouses"),

    path("", ShortDispatchListCreateAPI.as_view(), name="short-dispatch-list-create"),
    path("<int:pk>/", ShortDispatchDetailAPI.as_view(), name="short-dispatch-detail"),
    path("<int:pk>/print/", ShortDispatchPrintAPI.as_view(), name="short-dispatch-print"),
]
