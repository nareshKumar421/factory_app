from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import EmployeeViewSet, AttendanceRecordViewSet

router = DefaultRouter()
router.register("employees", EmployeeViewSet, basename="attendance-employee")
router.register("records", AttendanceRecordViewSet, basename="attendance-record")

urlpatterns = [
    path("", include(router.urls)),
]
