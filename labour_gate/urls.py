from django.urls import path

from .views import (
    LabourGateDayAPI,
    LabourInAPI,
    LabourEntryDetailAPI,
    LabourOutAPI,
    LabourOutUndoAPI,
)

urlpatterns = [
    path("", LabourGateDayAPI.as_view(), name="labour-gate-day"),
    path("in/", LabourInAPI.as_view(), name="labour-gate-in"),
    path("<int:pk>/", LabourEntryDetailAPI.as_view(), name="labour-gate-entry-detail"),
    path("<int:pk>/out/", LabourOutAPI.as_view(), name="labour-gate-out"),
    path("<int:pk>/out/undo/", LabourOutUndoAPI.as_view(), name="labour-gate-out-undo"),
]
