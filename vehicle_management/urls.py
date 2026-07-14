from django.urls import path
from vehicle_management.views import (
    TransporterDetailAPI,
    TransporterListCreateAPI,
    TransporterNameListAPI,
    VehicleDetailAPI,
    VehicleHistoryAPI,
    VehicleListCreateAPI,
    VehicleEntryListCreateAPI,
    VehicleEntryDetailAPI,
    VehicleEntryCountAPI,
    VehicleEntryListByStatus,
    VehicleNameListAPI,
    VehicleTypeListAPI,
)

urlpatterns = [
    # Transporter
    path("transporters/", TransporterListCreateAPI.as_view()),
    path("transporters/names/", TransporterNameListAPI.as_view()),
    path("transporters/<int:id>/", TransporterDetailAPI.as_view()),

    # Vehicle
    path("vehicles/", VehicleListCreateAPI.as_view()),
    path("vehicles/names/", VehicleNameListAPI.as_view()),
    path("vehicles/by-number/<str:vehicle_number>/history/", VehicleHistoryAPI.as_view()),
    path("vehicles/<int:id>/", VehicleDetailAPI.as_view()),

    # Vehicle Entry (Gate root)
    path("vehicle-entries/", VehicleEntryListCreateAPI.as_view()),
    path("vehicle-entries/<int:id>/", VehicleEntryDetailAPI.as_view()),
    path("vehicle-entries/count/", VehicleEntryCountAPI.as_view()),
    path("vehicle-entries/list-by-status/", VehicleEntryListByStatus.as_view()),

    path("vehicle-types/", VehicleTypeListAPI.as_view()),

]
