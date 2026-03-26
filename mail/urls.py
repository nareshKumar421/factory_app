from django.urls import path
from .views import (
    EmailLogListAPI,
    SendEmailAPI,
    SendByPermissionAPI,
    SendByGroupAPI,
    TestEmailAPI,
)

urlpatterns = [
    # Email logs
    path("", EmailLogListAPI.as_view(), name="email-log-list"),

    # Admin sending
    path("send/", SendEmailAPI.as_view(), name="email-send"),
    path("send-by-permission/", SendByPermissionAPI.as_view(), name="email-send-by-permission"),
    path("send-by-group/", SendByGroupAPI.as_view(), name="email-send-by-group"),

    # Test
    path("test/", TestEmailAPI.as_view(), name="email-test"),
]
