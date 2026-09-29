from django.urls import path

from .views import (
    SapPostingCancelView,
    SapPostingCountsView,
    SapPostingDetailView,
    SapPostingListView,
    SapPostingRetryView,
)

urlpatterns = [
    path("", SapPostingListView.as_view(), name="sap-posting-list"),
    path("counts/", SapPostingCountsView.as_view(), name="sap-posting-counts"),
    path("<int:pk>/", SapPostingDetailView.as_view(), name="sap-posting-detail"),
    path("<int:pk>/retry/", SapPostingRetryView.as_view(), name="sap-posting-retry"),
    path("<int:pk>/cancel/", SapPostingCancelView.as_view(), name="sap-posting-cancel"),
]
