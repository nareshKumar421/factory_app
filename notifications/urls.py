from django.urls import path
from .views import (
    DeviceRegisterAPI,
    DeviceUnregisterAPI,
    NotificationDetailAPI,
    NotificationListAPI,
    NotificationMarkReadAPI,
    NotificationPreferencesAPI,
    NotificationUnreadCountAPI,
    SendNotificationAPI,
    SendByPermissionAPI,
    SendByGroupAPI,
    TestNotificationAPI,
)

urlpatterns = [
    # Device token management
    path("devices/register/", DeviceRegisterAPI.as_view(), name="device-register"),
    path("devices/unregister/", DeviceUnregisterAPI.as_view(), name="device-unregister"),

    # Notification CRUD
    path("", NotificationListAPI.as_view(), name="notification-list"),
    path("<int:notification_id>/", NotificationDetailAPI.as_view(), name="notification-detail"),
    path("mark-read/", NotificationMarkReadAPI.as_view(), name="notification-mark-read"),
    path("unread-count/", NotificationUnreadCountAPI.as_view(), name="notification-unread-count"),
    path("preferences/", NotificationPreferencesAPI.as_view(), name="notification-preferences"),
    path("test/", TestNotificationAPI.as_view(), name="notification-test"),

    # Admin sending
    path("send/", SendNotificationAPI.as_view(), name="notification-send"),
    path("send-by-permission/", SendByPermissionAPI.as_view(), name="notification-send-by-permission"),
    path("send-by-group/", SendByGroupAPI.as_view(), name="notification-send-by-group"),
]
