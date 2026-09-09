"""Routes for the SAP-identity admin page.

Mounted separately from ``sap_client.urls`` (which sits under the legacy
``/api/v1/po/`` prefix) so these read as what they are.
"""
from django.urls import path

from .views_identity import (
    MySapIdentityView,
    SapApproverIdentityDetailView,
    SapApproverIdentityListCreateView,
    SapUserListView,
)

urlpatterns = [
    path(
        "identities/",
        SapApproverIdentityListCreateView.as_view(),
        name="sap-identity-list-create",
    ),
    path(
        "identities/<int:pk>/",
        SapApproverIdentityDetailView.as_view(),
        name="sap-identity-detail",
    ),
    path("sap-users/", SapUserListView.as_view(), name="sap-identity-sap-users"),
    path("me/", MySapIdentityView.as_view(), name="sap-identity-me"),
]
