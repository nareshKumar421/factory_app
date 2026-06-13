from django.urls import path

from .views import (
    DockingScanSkipRequestApproveView,
    DockingScanSkipRequestForDispatchView,
    DockingScanSkipRequestListCreateView,
    DockingScanSkipRequestRejectView,
)

urlpatterns = [
    path(
        "scan-skip-requests/",
        DockingScanSkipRequestListCreateView.as_view(),
        name="docking-scan-skip-list-create",
    ),
    path(
        "scan-skip-requests/by-sales-dispatch/<int:entry_id>/",
        DockingScanSkipRequestForDispatchView.as_view(),
        name="docking-scan-skip-by-dispatch",
    ),
    path(
        "scan-skip-requests/<int:pk>/approve/",
        DockingScanSkipRequestApproveView.as_view(),
        name="docking-scan-skip-approve",
    ),
    path(
        "scan-skip-requests/<int:pk>/reject/",
        DockingScanSkipRequestRejectView.as_view(),
        name="docking-scan-skip-reject",
    ),
]
