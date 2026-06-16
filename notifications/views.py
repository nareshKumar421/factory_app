import logging

from django.db import models
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .models import Notification, NotificationPreference, NotificationType
from .services import NotificationService
from .serializers import (
    DeviceRegistrationSerializer,
    DeviceUnregisterSerializer,
    NotificationPreferenceSerializer,
    NotificationPreferenceUpdateSerializer,
    NotificationSerializer,
    NotificationMarkReadSerializer,
    SendNotificationSerializer,
    SendByPermissionSerializer,
    SendByGroupSerializer,
    TestNotificationSerializer,
)
from .permissions import CanSendNotification, CanSendBulkNotification

logger = logging.getLogger(__name__)


def _positive_int(value, default: int, maximum: int = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(parsed, 1)
    return min(parsed, maximum) if maximum else parsed


def _non_negative_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0)


def _notification_type_choices():
    return list(NotificationType.choices)


def _notification_type_id(code: str) -> int:
    for index, (choice_code, _) in enumerate(_notification_type_choices(), start=1):
        if choice_code == code:
            return index
    return 0


def _notification_type_code(notification_type_id: int) -> str:
    choices = _notification_type_choices()
    if 1 <= notification_type_id <= len(choices):
        return choices[notification_type_id - 1][0]
    return ""


def _preference_payload(user, code: str, label: str, preference_map=None) -> dict:
    if preference_map is None:
        preference_map = dict(
            NotificationPreference.objects.filter(user=user).values_list(
                "notification_type",
                "is_enabled",
            )
        )
    return {
        "id": _notification_type_id(code),
        "code": code,
        "name": label,
        "description": label,
        "is_enabled": preference_map.get(code, True),
    }


class DeviceRegisterAPI(APIView): 
    """
    Register FCM device token for the authenticated user.
    POST /api/v1/notifications/devices/register/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = NotificationService.register_device(
            user=request.user,
            fcm_token=serializer.validated_data["fcm_token"],
            device_type=serializer.validated_data.get("device_type", "WEB"),
            device_info=serializer.validated_data.get("device_info", ""),
        )

        return Response(
            {"message": "Device registered successfully", "device_id": device.id},
            status=status.HTTP_201_CREATED
        )


class DeviceUnregisterAPI(APIView):
    """
    Unregister FCM device token (on logout).
    POST /api/v1/notifications/devices/unregister/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceUnregisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        removed = NotificationService.unregister_device(
            user=request.user,
            fcm_token=serializer.validated_data["fcm_token"],
        )

        if removed:
            return Response({"message": "Device unregistered successfully"})
        return Response(
            {"detail": "Device token not found"},
            status=status.HTTP_404_NOT_FOUND
        )


class NotificationListAPI(APIView):
    """
    List notifications for the authenticated user.
    GET /api/v1/notifications/
    Query params:
    - ?is_read=true|false&type=GRPO_POSTED&page=1&page_size=20
    - ?is_read=true|false&type=GRPO_POSTED&limit=20&offset=0
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Notification.objects.filter(recipient=request.user)

        # Filter by company if header present
        company_code = request.headers.get("Company-Code")
        if company_code:
            queryset = queryset.filter(
                models.Q(company__code=company_code) | models.Q(company__isnull=True)
            )

        # Filter by read status
        is_read = request.query_params.get("is_read")
        if is_read is not None:
            queryset = queryset.filter(is_read=is_read.lower() == "true")

        # Filter by type
        ntype = request.query_params.get("type")
        if ntype:
            queryset = queryset.filter(notification_type=ntype)

        # Pagination supports both backend page/page_size and frontend limit/offset.
        if "limit" in request.query_params or "offset" in request.query_params:
            limit = _positive_int(request.query_params.get("limit"), 20, maximum=100)
            offset = _non_negative_int(request.query_params.get("offset"))
            page_size = limit
            page = (offset // page_size) + 1
            start = offset
            end = start + page_size
        else:
            page = _positive_int(request.query_params.get("page"), 1)
            page_size = _positive_int(request.query_params.get("page_size"), 20, maximum=100)
            start = (page - 1) * page_size
            end = start + page_size

        total_count = queryset.count()
        unread_count = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).count()
        notifications = queryset[start:end]

        serializer = NotificationSerializer(notifications, many=True)

        return Response({
            "results": serializer.data,
            "count": total_count,
            "total_count": total_count,
            "unread_count": unread_count,
            "page": page,
            "page_size": page_size,
            "limit": page_size,
            "offset": start,
        })


class NotificationDetailAPI(APIView):
    """
    Retrieve a notification for the authenticated user and mark it as read.
    GET /api/v1/notifications/<id>/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, notification_id: int):
        try:
            notification = Notification.objects.get(
                id=notification_id,
                recipient=request.user,
            )
        except Notification.DoesNotExist:
            return Response(
                {"detail": "Notification not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=["is_read", "read_at"])

        serializer = NotificationSerializer(notification)
        return Response(serializer.data)


class NotificationMarkReadAPI(APIView):
    """
    Mark notifications as read.
    POST /api/v1/notifications/mark-read/
    Body: {"notification_ids": [1, 2, 3]} or {} to mark all as read.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = NotificationMarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        notification_ids = serializer.validated_data.get("notification_ids", [])
        now = timezone.now()

        queryset = Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        )

        if notification_ids:
            queryset = queryset.filter(id__in=notification_ids)

        updated = queryset.update(is_read=True, read_at=now)

        return Response({
            "message": f"{updated} notification(s) marked as read",
            "marked_read": updated,
        })


class NotificationUnreadCountAPI(APIView):
    """
    Get unread notification count.
    GET /api/v1/notifications/unread-count/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        ).count()
        return Response({"unread_count": count})


class NotificationPreferencesAPI(APIView):
    """
    List or update per-user notification preferences.
    GET /api/v1/notifications/preferences/
    POST /api/v1/notifications/preferences/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        preference_map = dict(
            NotificationPreference.objects.filter(user=request.user).values_list(
                "notification_type",
                "is_enabled",
            )
        )
        payload = [
            _preference_payload(request.user, code, label, preference_map)
            for code, label in _notification_type_choices()
        ]
        serializer = NotificationPreferenceSerializer(payload, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = NotificationPreferenceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        notification_type = serializer.validated_data.get("notification_type")
        if not notification_type:
            notification_type = _notification_type_code(
                serializer.validated_data["notification_type_id"]
            )

        if not notification_type:
            return Response(
                {"detail": "Invalid notification_type_id"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        preference, _ = NotificationPreference.objects.update_or_create(
            user=request.user,
            notification_type=notification_type,
            defaults={"is_enabled": serializer.validated_data["is_enabled"]},
        )
        label = dict(_notification_type_choices())[preference.notification_type]
        payload = _preference_payload(
            request.user,
            preference.notification_type,
            label,
            {preference.notification_type: preference.is_enabled},
        )
        return Response(payload)


class SendNotificationAPI(APIView):
    """
    Admin endpoint to send manual notifications.
    POST /api/v1/notifications/send/
    Requires: notifications.can_send_notification permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendNotification]

    def post(self, request):
        serializer = SendNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company
        recipient_user_ids = data.get("recipient_user_ids", [])
        role_filter = data.get("role_filter", "")

        if recipient_user_ids:
            from accounts.models import User
            users = User.objects.filter(id__in=recipient_user_ids, is_active=True)
            notifications = NotificationService.send_notification_to_group(
                users=users,
                title=data["title"],
                body=data["body"],
                notification_type=data.get("notification_type", "GENERAL_ANNOUNCEMENT"),
                click_action_url=data.get("click_action_url", ""),
                company=company,
                created_by=request.user,
            )
            count = len(notifications)
        else:
            count = NotificationService.send_bulk_notification(
                company=company,
                title=data["title"],
                body=data["body"],
                notification_type=data.get("notification_type", "GENERAL_ANNOUNCEMENT"),
                click_action_url=data.get("click_action_url", ""),
                role_name=role_filter or None,
                created_by=request.user,
            )

        return Response({
            "message": f"Notification sent to {count} users",
            "recipients_count": count,
        })


class SendByPermissionAPI(APIView):
    """
    Send notification to all users who have a specific permission.
    POST /api/v1/notifications/send-by-permission/
    Requires: notifications.can_send_bulk_notification permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendBulkNotification]

    def post(self, request):
        serializer = SendByPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company

        count = NotificationService.send_notification_by_permission(
            permission_codename=data["permission_codename"],
            title=data["title"],
            body=data["body"],
            notification_type=data.get("notification_type", "GENERAL_ANNOUNCEMENT"),
            click_action_url=data.get("click_action_url", ""),
            company=company,
            created_by=request.user,
        )

        return Response({
            "message": f"Notification sent to {count} users with permission '{data['permission_codename']}'",
            "recipients_count": count,
        })


class SendByGroupAPI(APIView):
    """
    Send notification to all users in a Django auth group.
    POST /api/v1/notifications/send-by-group/
    Requires: notifications.can_send_bulk_notification permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendBulkNotification]

    def post(self, request):
        serializer = SendByGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company

        count = NotificationService.send_notification_by_auth_group(
            group_name=data["group_name"],
            title=data["title"],
            body=data["body"],
            notification_type=data.get("notification_type", "GENERAL_ANNOUNCEMENT"),
            click_action_url=data.get("click_action_url", ""),
            company=company,
            created_by=request.user,
        )

        return Response({
            "message": f"Notification sent to {count} users in group '{data['group_name']}'",
            "recipients_count": count,
        })


class TestNotificationAPI(APIView):
    """
    Send a push-only test notification to one FCM token.
    POST /api/v1/notifications/test/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = TestNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = NotificationService._send_to_tokens(
            tokens=[data["token"]],
            title=data["title"],
            body=data["body"],
            data={"notification_type": NotificationType.GENERAL_ANNOUNCEMENT},
            click_action_url=data.get("click_action_url", ""),
        )
        success = result["success_count"] > 0
        error = result.get("error")
        if not success and not error:
            error = "FCM send failed for the provided token."

        return Response(
            {
                "success": success,
                "message_id": None,
                "error": error,
                "result": result,
            },
            status=status.HTTP_200_OK if success else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
