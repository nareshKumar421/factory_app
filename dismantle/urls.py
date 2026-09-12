from django.urls import path

from .views import (
    DismantleBatchesAPI,
    DismantleBulkCreateAPI,
    DismantleComponentsAPI,
    DismantleDetailAPI,
    DismantleListCreateAPI,
    DismantlePostAPI,
    DismantlePreviewAPI,
    DismantleRebuildComponentsAPI,
    DismantleReturnedLinesAPI,
    DismantleStockAPI,
    DismantleWarehousesAPI,
)

urlpatterns = [
    # Static routes first, so they are not swallowed as an <int:pk>.
    path("returned-lines/", DismantleReturnedLinesAPI.as_view(), name="dismantle-returned-lines"),
    path("stock/", DismantleStockAPI.as_view(), name="dismantle-stock"),
    path("batches/", DismantleBatchesAPI.as_view(), name="dismantle-batches"),
    path("warehouses/", DismantleWarehousesAPI.as_view(), name="dismantle-warehouses"),

    path("bulk/", DismantleBulkCreateAPI.as_view(), name="dismantle-bulk-create"),

    path("", DismantleListCreateAPI.as_view(), name="dismantle-list-create"),
    path("<int:pk>/", DismantleDetailAPI.as_view(), name="dismantle-detail"),
    path("<int:pk>/components/", DismantleComponentsAPI.as_view(), name="dismantle-components"),
    path(
        "<int:pk>/components/rebuild/",
        DismantleRebuildComponentsAPI.as_view(),
        name="dismantle-components-rebuild",
    ),
    path("<int:pk>/preview/", DismantlePreviewAPI.as_view(), name="dismantle-preview"),
    path("<int:pk>/post/", DismantlePostAPI.as_view(), name="dismantle-post"),
]
