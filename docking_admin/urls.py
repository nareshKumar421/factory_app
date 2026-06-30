from django.urls import path

from .views import (
    DockingPartialScanRequestApproveView,
    DockingPartialScanRequestForDispatchView,
    DockingPartialScanRequestListCreateView,
    DockingPartialScanRequestRejectView,
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
    path(
        "partial-scan-requests/",
        DockingPartialScanRequestListCreateView.as_view(),
        name="docking-partial-scan-list-create",
    ),
    path(
        "partial-scan-requests/by-sales-dispatch/<int:entry_id>/",
        DockingPartialScanRequestForDispatchView.as_view(),
        name="docking-partial-scan-by-dispatch",
    ),
    path(
        "partial-scan-requests/<int:pk>/approve/",
        DockingPartialScanRequestApproveView.as_view(),
        name="docking-partial-scan-approve",
    ),
    path(
        "partial-scan-requests/<int:pk>/reject/",
        DockingPartialScanRequestRejectView.as_view(),
        name="docking-partial-scan-reject",
    ),
]
