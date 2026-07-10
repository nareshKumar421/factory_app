from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ReturnableDashboardView,
    ReturnableGatePassAttachmentViewSet,
    ReturnableGatePassItemViewSet,
    ReturnableGatePassViewSet,
    ReturnableOptionsView,
    ReturnableReportsView,
    ReturnableReturnEventViewSet,
    ReturnableSapItemSearchView,
)

router = DefaultRouter()
router.register(r"returnable-gatepasses", ReturnableGatePassViewSet, basename="returnable-gatepass")
router.register(
    r"returnable-gatepass-items", ReturnableGatePassItemViewSet, basename="returnable-gatepass-item"
)
router.register(
    r"returnable-return-events", ReturnableReturnEventViewSet, basename="returnable-return-event"
)
router.register(
    r"returnable-attachments",
    ReturnableGatePassAttachmentViewSet,
    basename="returnable-attachment",
)

urlpatterns = [
    path("", include(router.urls)),
    path("dashboard/", ReturnableDashboardView.as_view(), name="returnable-dashboard"),
    path("reports/", ReturnableReportsView.as_view(), name="returnable-reports"),
    path("options/", ReturnableOptionsView.as_view(), name="returnable-options"),
    path("sap-items/", ReturnableSapItemSearchView.as_view(), name="returnable-sap-items"),
]
