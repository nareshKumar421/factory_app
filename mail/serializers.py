from rest_framework import serializers
from .models import EmailLog


class EmailLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmailLog
        fields = [
            "id",
            "recipient_email",
            "subject",
            "body",
            "email_type",
            "click_action_url",
            "reference_type",
            "reference_id",
            "template_name",
            "status",
            "error_message",
            "extra_data",
            "created_at",
        ]


class SendEmailSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=255)
    body = serializers.CharField()
    email_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")
    template_name = serializers.CharField(max_length=100, default="general.html")
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
        help_text="Permission codename (e.g., 'can_send_email')"
    )
    subject = serializers.CharField(max_length=255)
    body = serializers.CharField()
    email_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")
    template_name = serializers.CharField(max_length=100, default="general.html")


class SendByGroupSerializer(serializers.Serializer):
    group_name = serializers.CharField(
        max_length=150,
        help_text="Django auth group name (e.g., 'grpo', 'quality_control')"
    )
    subject = serializers.CharField(max_length=255)
    body = serializers.CharField()
    email_type = serializers.CharField(default="GENERAL_ANNOUNCEMENT")
    click_action_url = serializers.CharField(required=False, allow_blank=True, default="")
    template_name = serializers.CharField(max_length=100, default="general.html")
