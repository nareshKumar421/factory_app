from django.urls import path

from .views import (
    UniversalSearchAPI,
    UniversalSearchDocumentAPI,
    UniversalSearchItemStockAPI,
)

urlpatterns = [
    path("search/", UniversalSearchAPI.as_view(), name="universal-search"),
    path(
        "document/",
        UniversalSearchDocumentAPI.as_view(),
        name="universal-search-document",
    ),
    path(
        "item-stock/",
        UniversalSearchItemStockAPI.as_view(),
        name="universal-search-item-stock",
    ),
]
