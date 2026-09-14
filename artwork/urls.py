from django.urls import path

from .views import (
    ArtworkDownloadAPI,
    ArtworkItemListAPI,
    ArtworkOptionsAPI,
    ArtworkRecordDetailAPI,
    ArtworkRecordListCreateAPI,
    ArtworkRevisionDownloadAPI,
    ArtworkRevisionListAPI,
    ArtworkSummaryAPI,
)

urlpatterns = [
    # The page's main list: every label and carton item, captured or not.
    path("items/", ArtworkItemListAPI.as_view(), name="artwork-items"),
    path("options/", ArtworkOptionsAPI.as_view(), name="artwork-options"),
    path("summary/", ArtworkSummaryAPI.as_view(), name="artwork-summary"),
    path("records/", ArtworkRecordListCreateAPI.as_view(), name="artwork-records"),
    path(
        "records/<int:pk>/",
        ArtworkRecordDetailAPI.as_view(),
        name="artwork-record-detail",
    ),
    path(
        "records/<int:pk>/revisions/",
        ArtworkRevisionListAPI.as_view(),
        name="artwork-record-revisions",
    ),
    path(
        "records/<int:pk>/download/<str:kind>/",
        ArtworkDownloadAPI.as_view(),
        name="artwork-record-download",
    ),
    path(
        "revisions/<int:pk>/download/<str:kind>/",
        ArtworkRevisionDownloadAPI.as_view(),
        name="artwork-revision-download",
    ),
]
