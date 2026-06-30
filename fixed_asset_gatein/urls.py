from django.urls import path
from .views import (
    FixedAssetGateEntryCreateAPI,
    FixedAssetGateEntryUpdateAPI,
    FixedAssetGateCompleteAPI,
    AssetCategoryListAPI,
)

urlpatterns = [
    # Create/Read fixed asset entry
    path(
        "gate-entries/<int:gate_entry_id>/fixed-asset/",
        FixedAssetGateEntryCreateAPI.as_view(),
        name="fixed-asset-entry-create"
    ),
    # Update fixed asset entry
    path(
        "gate-entries/<int:gate_entry_id>/fixed-asset/update/",
        FixedAssetGateEntryUpdateAPI.as_view(),
        name="fixed-asset-entry-update"
    ),
    # Complete/lock gate entry
    path(
        "gate-entries/<int:gate_entry_id>/complete/",
        FixedAssetGateCompleteAPI.as_view(),
        name="fixed-asset-complete"
    ),
    # List asset categories (for dropdown)
    path(
        "gate-entries/fixed-asset/categories/",
        AssetCategoryListAPI.as_view(),
        name="fixed-asset-categories-list"
    ),
]
