from django.urls import path
from .views import (
    OutboundGateEntryCreateAPI,
    OutboundGateEntryUpdateAPI,
    OutboundGateCompleteAPI,
    ReleaseForLoadingAPI,
    OutboundGateEntryListAPI,
    OutboundPurposeListAPI,
)

urlpatterns = [
    # Create / Read outbound entry for a gate entry
    path(
        "gate-entries/<int:gate_entry_id>/outbound/",
        OutboundGateEntryCreateAPI.as_view(),
        name="outbound-entry-create",
    ),
    # Update outbound entry
    path(
        "gate-entries/<int:gate_entry_id>/outbound/update/",
        OutboundGateEntryUpdateAPI.as_view(),
        name="outbound-entry-update",
    ),
    # Complete / lock gate entry
    path(
        "gate-entries/<int:gate_entry_id>/complete/",
        OutboundGateCompleteAPI.as_view(),
        name="outbound-complete",
    ),
    # Release vehicle for loading
    path(
        "gate-entries/<int:gate_entry_id>/release-for-loading/",
        ReleaseForLoadingAPI.as_view(),
        name="outbound-release-for-loading",
    ),
    # List available outbound vehicles (for link-vehicle dropdown)
    path(
        "available-vehicles/",
        OutboundGateEntryListAPI.as_view(),
        name="outbound-available-vehicles",
    ),
    # Outbound purpose lookup
    path(
        "purposes/",
        OutboundPurposeListAPI.as_view(),
        name="outbound-purposes",
    ),
]
