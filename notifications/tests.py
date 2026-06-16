from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from .models import Notification, NotificationPreference, NotificationType


class NotificationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            email="notify@example.com",
            password="password",
            full_name="Notify User",
            employee_code="EMP-NOTIFY",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="password",
            full_name="Other User",
            employee_code="EMP-OTHER",
        )
        self.client.force_authenticate(user=self.user)

    def test_list_supports_frontend_limit_offset_and_count(self):
        Notification.objects.create(
            recipient=self.user,
            title="First",
            body="Unread notification",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )
        Notification.objects.create(
            recipient=self.user,
            title="Second",
            body="Read notification",
            notification_type=NotificationType.GRPO_POSTED,
            is_read=True,
        )
        Notification.objects.create(
            recipient=self.other_user,
            title="Other",
            body="Should not leak",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )

        response = self.client.get(
            "/api/v1/notifications/",
            {"limit": 1, "offset": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["total_count"], 2)
        self.assertEqual(response.data["unread_count"], 1)
        self.assertEqual(response.data["limit"], 1)
        self.assertEqual(response.data["offset"], 1)
        self.assertEqual(len(response.data["results"]), 1)

    def test_detail_marks_own_notification_as_read(self):
        notification = Notification.objects.create(
            recipient=self.user,
            title="Detail",
            body="Open me",
            notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
        )

        response = self.client.get(f"/api/v1/notifications/{notification.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)

    def test_preferences_default_enabled_and_update_by_type(self):
        response = self.client.get("/api/v1/notifications/preferences/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(NotificationType.choices))
        general = next(
            item
            for item in response.data
            if item["code"] == NotificationType.GENERAL_ANNOUNCEMENT
        )
        self.assertTrue(general["is_enabled"])

        response = self.client.post(
            "/api/v1/notifications/preferences/",
            {
                "notification_type": NotificationType.GRPO_POSTED,
                "is_enabled": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["code"], NotificationType.GRPO_POSTED)
        self.assertFalse(response.data["is_enabled"])
        self.assertFalse(
            NotificationPreference.objects.get(
                user=self.user,
                notification_type=NotificationType.GRPO_POSTED,
            ).is_enabled
        )

    @patch("notifications.views.NotificationService._send_to_tokens")
    def test_test_notification_endpoint_sends_to_fcm_token(self, send_to_tokens):
        send_to_tokens.return_value = {
            "success_count": 1,
            "failure_count": 0,
            "responses": [],
        }

        response = self.client.post(
            "/api/v1/notifications/test/",
            {
                "token": "valid-looking-token",
                "title": "Test title",
                "body": "Test body",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["success"])
        send_to_tokens.assert_called_once()
        _, kwargs = send_to_tokens.call_args
        self.assertEqual(kwargs["tokens"], ["valid-looking-token"])
        self.assertEqual(kwargs["title"], "Test title")
        self.assertEqual(kwargs["body"], "Test body")
        self.assertEqual(
            kwargs["data"]["notification_type"],
            NotificationType.GENERAL_ANNOUNCEMENT,
        )
