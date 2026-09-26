"""Routes for the master-data pickers (``/api/v1/sap-lookups/``).

Mounted separately from ``sap_client.urls`` (the legacy ``/api/v1/po/`` prefix)
so the paths read as what they are, the way ``urls_identity`` does.
"""
from django.urls import path

from .views_lookups import LOOKUPS, SapLookupView

urlpatterns = [
    path(f"{name}/", SapLookupView.as_view(lookup=name), name=f"sap-lookup-{name}")
    for name in LOOKUPS
]
