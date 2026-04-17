from django.urls import path
from .views import (
    CreateVerificationRequestAPI,
    ListVerificationRequestsAPI,
    TodayVerificationRequestAPI,
    VerificationRequestDetailAPI,
    CloseVerificationRequestAPI,
    SubmitDepartmentResponseAPI,
    MyDepartmentResponseAPI,
)

urlpatterns = [
    path("request/create/", CreateVerificationRequestAPI.as_view(), name="create-verification-request"),
    path("requests/", ListVerificationRequestsAPI.as_view(), name="list-verification-requests"),
    path("request/today/", TodayVerificationRequestAPI.as_view(), name="today-verification-request"),
    path("request/<int:pk>/", VerificationRequestDetailAPI.as_view(), name="verification-request-detail"),
    path("request/<int:pk>/close/", CloseVerificationRequestAPI.as_view(), name="close-verification-request"),
    path("request/<int:pk>/respond/", SubmitDepartmentResponseAPI.as_view(), name="submit-department-response"),
    path("request/<int:pk>/my-response/", MyDepartmentResponseAPI.as_view(), name="my-department-response"),
]
