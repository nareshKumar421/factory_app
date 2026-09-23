from django.urls import path

from .views import (
    ExpiringDocumentsAPI,
    FleetAttachmentAPI,
    FleetCostReportAPI,
    FleetOptionsAPI,
    FleetSummaryAPI,
    FleetVehicleDetailAPI,
    FleetVehicleListCreateAPI,
    FleetVehicleSummaryAPI,
    FuelEntryApprovalAPI,
    FuelEntryDetailAPI,
    FuelEntryListCreateAPI,
    PendingApprovalsAPI,
    ServiceEntryApprovalAPI,
    ServiceEntryDetailAPI,
    ServiceEntryListCreateAPI,
    VehicleDocumentDetailAPI,
    VehicleDocumentListCreateAPI,
)

urlpatterns = [
    path("options/", FleetOptionsAPI.as_view(), name="fleet-options"),
    path(
        "attachments/<str:kind>/<int:pk>/",
        FleetAttachmentAPI.as_view(),
        name="fleet-attachment",
    ),
    path("summary/", FleetSummaryAPI.as_view(), name="fleet-summary"),
    path("cost-report/", FleetCostReportAPI.as_view(), name="fleet-cost-report"),
    path("pending-approvals/", PendingApprovalsAPI.as_view(), name="fleet-pending-approvals"),

    path("vehicles/", FleetVehicleListCreateAPI.as_view(), name="fleet-vehicles"),
    path("vehicles/<int:pk>/", FleetVehicleDetailAPI.as_view(), name="fleet-vehicle-detail"),
    path(
        "vehicles/<int:pk>/summary/",
        FleetVehicleSummaryAPI.as_view(),
        name="fleet-vehicle-summary",
    ),

    path("fuel-entries/", FuelEntryListCreateAPI.as_view(), name="fleet-fuel-entries"),
    path("fuel-entries/<int:pk>/", FuelEntryDetailAPI.as_view(), name="fleet-fuel-entry-detail"),
    path(
        "fuel-entries/<int:pk>/approval/",
        FuelEntryApprovalAPI.as_view(),
        name="fleet-fuel-entry-approval",
    ),

    path("service-entries/", ServiceEntryListCreateAPI.as_view(), name="fleet-service-entries"),
    path(
        "service-entries/<int:pk>/",
        ServiceEntryDetailAPI.as_view(),
        name="fleet-service-entry-detail",
    ),
    path(
        "service-entries/<int:pk>/approval/",
        ServiceEntryApprovalAPI.as_view(),
        name="fleet-service-entry-approval",
    ),

    path("documents/", VehicleDocumentListCreateAPI.as_view(), name="fleet-documents"),
    path("documents/expiring/", ExpiringDocumentsAPI.as_view(), name="fleet-documents-expiring"),
    path("documents/<int:pk>/", VehicleDocumentDetailAPI.as_view(), name="fleet-document-detail"),
]
