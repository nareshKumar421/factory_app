from django.urls import path

from .views import NonMovingRMReportAPI, ItemGroupDropdownAPI

urlpatterns = [
    path("report/", NonMovingRMReportAPI.as_view(), name="non-moving-rm-report"),
    path("item-groups/", ItemGroupDropdownAPI.as_view(), name="non-moving-rm-item-groups"),
]
