from django.urls import path

from .views import PmDemandReportAPI

urlpatterns = [
    path("report/", PmDemandReportAPI.as_view(), name="pm-demand-report"),
]
