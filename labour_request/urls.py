from django.urls import path

from .views import (
    LabourRequestAuditAPI,
    LabourRequestDayAPI,
    LabourRequestDecisionAPI,
    LabourRequestDetailAPI,
    LabourRequestRaiseAPI,
    LabourRequestReopenAPI,
    LabourRequestRestoreAPI,
)

urlpatterns = [
    path("", LabourRequestDayAPI.as_view(), name="labour-request-day"),
    path("raise/", LabourRequestRaiseAPI.as_view(), name="labour-request-raise"),
    path("<int:pk>/", LabourRequestDetailAPI.as_view(), name="labour-request-detail"),
    path("<int:pk>/audit/", LabourRequestAuditAPI.as_view(), name="labour-request-audit"),
    path("<int:pk>/restore/", LabourRequestRestoreAPI.as_view(), name="labour-request-restore"),
    path("<int:pk>/decision/", LabourRequestDecisionAPI.as_view(), name="labour-request-decision"),
    path("<int:pk>/reopen/", LabourRequestReopenAPI.as_view(), name="labour-request-reopen"),
]
