from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AttendanceEmployeeViewSet,
    AttendanceRecordViewSet,
    DailyAttendanceViewSet,
)

router = DefaultRouter()
# The daily sheet is the module's main surface; the other two support it.
router.register("daily", DailyAttendanceViewSet, basename="attendance-daily")
router.register("employees", AttendanceEmployeeViewSet, basename="attendance-employee")
router.register("records", AttendanceRecordViewSet, basename="attendance-record")

urlpatterns = [
    path("", include(router.urls)),
]
