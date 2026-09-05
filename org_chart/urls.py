from django.urls import path

from .views import OrgChartAPI

urlpatterns = [
    path("chart/", OrgChartAPI.as_view(), name="org-chart"),
]
