from django.urls import path

from .views import OperationsReportDaysAPI

app_name = "operations_report"

urlpatterns = [
    path("days/", OperationsReportDaysAPI.as_view(), name="operations-report-days"),
]
