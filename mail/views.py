import logging

from django.db import models
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .models import EmailLog
from .services import EmailService
from .serializers import (
    EmailLogSerializer,
    SendEmailSerializer,
    SendByPermissionSerializer,
    SendByGroupSerializer,
)
from .permissions import CanSendEmail, CanSendBulkEmail

logger = logging.getLogger(__name__)


class EmailLogListAPI(APIView):
    """
    List email logs for the authenticated user.
    GET /api/v1/mail/
    Query params: ?status=SENT|FAILED&type=GRPO_POSTED&page=1&page_size=20
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = EmailLog.objects.filter(recipient=request.user)

        # Filter by company if header present
        company_code = request.headers.get("Company-Code")
        if company_code:
            queryset = queryset.filter(
                models.Q(company__code=company_code) | models.Q(company__isnull=True)
            )

        # Filter by status
        mail_status = request.query_params.get("status")
        if mail_status:
            queryset = queryset.filter(status=mail_status.upper())

        # Filter by type
        etype = request.query_params.get("type")
        if etype:
            queryset = queryset.filter(email_type=etype)

        # Simple pagination
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))
        page_size = min(page_size, 100)

        start = (page - 1) * page_size
        end = start + page_size

        total_count = queryset.count()
        email_logs = queryset[start:end]

        serializer = EmailLogSerializer(email_logs, many=True)

        return Response({
            "results": serializer.data,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        })


class SendEmailAPI(APIView):
    """
    Admin endpoint to send manual emails.
    POST /api/v1/mail/send/
    Requires: mail.can_send_email permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendEmail]

    def post(self, request):
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company
        recipient_user_ids = data.get("recipient_user_ids", [])
        role_filter = data.get("role_filter", "")

        if recipient_user_ids:
            from accounts.models import User
            users = User.objects.filter(id__in=recipient_user_ids, is_active=True)
            email_logs = EmailService.send_email_to_group(
                users=users,
                subject=data["subject"],
                body=data["body"],
                email_type=data.get("email_type", "GENERAL_ANNOUNCEMENT"),
                click_action_url=data.get("click_action_url", ""),
                company=company,
                created_by=request.user,
                template_name=data.get("template_name", "general.html"),
            )
            count = len(email_logs)
        else:
            count = EmailService.send_bulk_email(
                company=company,
                subject=data["subject"],
                body=data["body"],
                email_type=data.get("email_type", "GENERAL_ANNOUNCEMENT"),
                click_action_url=data.get("click_action_url", ""),
                role_name=role_filter or None,
                created_by=request.user,
                template_name=data.get("template_name", "general.html"),
            )

        return Response({
            "message": f"Email sent to {count} users",
            "recipients_count": count,
        })


class SendByPermissionAPI(APIView):
    """
    Send email to all users who have a specific permission.
    POST /api/v1/mail/send-by-permission/
    Requires: mail.can_send_bulk_email permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendBulkEmail]

    def post(self, request):
        serializer = SendByPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company

        count = EmailService.send_email_by_permission(
            permission_codename=data["permission_codename"],
            subject=data["subject"],
            body=data["body"],
            email_type=data.get("email_type", "GENERAL_ANNOUNCEMENT"),
            click_action_url=data.get("click_action_url", ""),
            company=company,
            created_by=request.user,
            template_name=data.get("template_name", "general.html"),
        )

        return Response({
            "message": f"Email sent to {count} users with permission '{data['permission_codename']}'",
            "recipients_count": count,
        })


class SendByGroupAPI(APIView):
    """
    Send email to all users in a Django auth group.
    POST /api/v1/mail/send-by-group/
    Requires: mail.can_send_bulk_email permission.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSendBulkEmail]

    def post(self, request):
        serializer = SendByGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        company = request.company.company

        count = EmailService.send_email_by_auth_group(
            group_name=data["group_name"],
            subject=data["subject"],
            body=data["body"],
            email_type=data.get("email_type", "GENERAL_ANNOUNCEMENT"),
            click_action_url=data.get("click_action_url", ""),
            company=company,
            created_by=request.user,
            template_name=data.get("template_name", "general.html"),
        )

        return Response({
            "message": f"Email sent to {count} users in group '{data['group_name']}'",
            "recipients_count": count,
        })


class TestEmailAPI(APIView):
    """
    Send a test email to the requesting user (for verifying SMTP config).
    POST /api/v1/mail/test/
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        log = EmailService.send_email_to_user(
            user=request.user,
            subject="Sampooran Factory - Test Email",
            body="Your email configuration is working correctly.",
            email_type="GENERAL_ANNOUNCEMENT",
            created_by=request.user,
            template_name="test.html",
        )

        return Response({
            "status": log.status,
            "message": f"Test email sent to {request.user.email}",
            "email_log_id": log.id,
        },
            status=status.HTTP_200_OK if log.status == "SENT" else status.HTTP_502_BAD_GATEWAY,
        )
