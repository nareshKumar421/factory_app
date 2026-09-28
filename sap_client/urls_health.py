"""Routes for the SAP health check (see ``sap_client.health``)."""
from django.urls import path

from .views_health import SAPHealthView

urlpatterns = [
    path("", SAPHealthView.as_view(), name="sap-health"),
]
