"""Import / Export, under ``api/v1/exim/``."""

from django.urls import path

from .views_customs_rates import CustomsRatesAPI
from .views_licence import (
    LicenceDetailAPI,
    LicenceLineCreateAPI,
    LicenceLineDetailAPI,
    LicenceListCreateAPI,
)

urlpatterns = [
    path("licences/", LicenceListCreateAPI.as_view(), name="exim-licences"),
    path("licences/<int:pk>/", LicenceDetailAPI.as_view(), name="exim-licence"),
    path("licences/<int:pk>/lines/", LicenceLineCreateAPI.as_view(), name="exim-licence-lines"),
    path("licence-lines/<int:pk>/", LicenceLineDetailAPI.as_view(), name="exim-licence-line"),
    path("customs-rates/", CustomsRatesAPI.as_view(), name="exim-customs-rates"),
]
