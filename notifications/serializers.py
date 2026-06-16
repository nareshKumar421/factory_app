from rest_framework import serializers
from .models import Notification, NotificationType


class DeviceRegistrationSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(required=True)
    device_type = serializers.ChoiceField(
        choices=["WEB", "ANDROID", "IOS"],
        default="WEB"
    )
    device_info = serializers.CharField(required=False, allow_blank=True, default="")


class DeviceUnregisterSerializer(serializers.Serializer):
    fcm_token = serializers.CharField(required=True)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "title",
            "body",
            "notification_type",
            "click_action_url",
            "reference_type",
            "reference_id",
            "is_read",
            "read_at",
            "extra_data",
            "created_at",
        ]


class NotificationPreferenceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    is_enabled = serializers.BooleanField()


class NotificationPreferenceUpdateSerializer(serializers.Serializer):
    notification_type_id = serializers.IntegerField(required=False)
    notification_type = serializers.ChoiceField(
        choices=NotificationType.values,
        required=False,
    )
    is_enabled = serializers.BooleanField()

    def validate(self, attrs):
        if not attrs.get("notification_type_id") and not attrs.get("notification_type"):
            raise serializers.ValidationError(
                "notification_type_id or notification_type is required."
            )
        return attrs


class NotificationMarkReadSerializer(serializers.Serializer):
    notification_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[],
        help_text="List of notification IDs to mark as read. If empty, marks all as read."
    )


class SendNotificationSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    body = serializers.CharField()
    notification_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")
    recipient_user_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[],
        help_text="Specific user IDs. If empty, sends to all company users."
    )
    role_filter = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Filter recipients by role name"
    )


class SendByPermissionSerializer(serializers.Serializer):
    permission_codename = serializers.CharField(
        max_length=255,
        help_text="Permission codename (e.g., 'can_send_notification')"
    )
    title = serializers.CharField(max_length=255)
    body = serializers.CharField()
    notification_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")


class SendByGroupSerializer(serializers.Serializer):
    group_name = serializers.CharField(
        max_length=150,
        help_text="Django auth group name (e.g., 'grpo', 'quality_control')"
    )
    title = serializers.CharField(max_length=255)
    body = serializers.CharField()
    notification_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")


class TestNotificationSerializer(serializers.Serializer):
    token = serializers.CharField(required=True)
    title = serializers.CharField(max_length=255)
    body = serializers.CharField()
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")
