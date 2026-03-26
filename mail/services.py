import logging
from typing import List, Dict, Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from .models import EmailLog, EmailType

logger = logging.getLogger(__name__)


class EmailService:
    """
    Centralized email service for the Sampooran Factory system.
    Mirrors NotificationService — same method signatures and patterns.
    """

    @staticmethod
    def _send_smtp(to: str, subject: str, html_body: str,
                   text_body: str = "") -> Dict[str, Any]:
        """
        Send a single email via SMTP.
        Returns dict with success status and error details.
        """
        if not text_body:
            text_body = strip_tags(html_body)

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[to],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send(fail_silently=False)
            return {"success": True, "error": ""}
        except Exception as e:
            logger.error(f"SMTP send error for {to}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    @transaction.atomic
    def send_email_to_user(
        cls,
        user,
        subject: str,
        body: str,
        email_type: str = EmailType.GENERAL_ANNOUNCEMENT,
        click_action_url: str = "",
        reference_type: str = "",
        reference_id: int = None,
        company=None,
        extra_data: dict = None,
        created_by=None,
        template_name: str = "general.html",
    ) -> EmailLog:
        """
        Send email to a specific user.
        Creates a stored EmailLog record and sends via SMTP.
        """
        context = {
            "user": user,
            "full_name": user.full_name,
            "subject": subject,
            "message": body,
            "click_action_url": click_action_url,
            "email_type": email_type,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "site_name": "Sampooran Factory",
            **(extra_data or {}),
        }

        html_body = render_to_string(f"mail/{template_name}", context)

        email_log = EmailLog(
            recipient=user,
            recipient_email=user.email,
            company=company,
            subject=subject,
            body=body,
            email_type=email_type,
            click_action_url=click_action_url,
            reference_type=reference_type,
            reference_id=reference_id,
            template_name=template_name,
            extra_data=extra_data or {},
            created_by=created_by,
        )

        result = cls._send_smtp(
            to=user.email,
            subject=subject,
            html_body=html_body,
        )

        if result["success"]:
            email_log.status = EmailLog.Status.SENT
            logger.info(f"Email sent to {user.email}: '{subject}'")
        else:
            email_log.status = EmailLog.Status.FAILED
            email_log.error_message = result["error"]
            logger.error(
                f"Email failed to {user.email}: '{subject}' - {result['error']}"
            )

        email_log.save()
        return email_log

    @classmethod
    def send_email_to_group(
        cls,
        users,
        subject: str,
        body: str,
        email_type: str = EmailType.GENERAL_ANNOUNCEMENT,
        click_action_url: str = "",
        reference_type: str = "",
        reference_id: int = None,
        company=None,
        extra_data: dict = None,
        created_by=None,
        template_name: str = "general.html",
    ) -> List[EmailLog]:
        """Send email to a list of users."""
        email_logs = []
        for user in users:
            email_log = cls.send_email_to_user(
                user=user,
                subject=subject,
                body=body,
                email_type=email_type,
                click_action_url=click_action_url,
                reference_type=reference_type,
                reference_id=reference_id,
                company=company,
                extra_data=extra_data,
                created_by=created_by,
                template_name=template_name,
            )
            email_logs.append(email_log)
        return email_logs

    @classmethod
    def send_bulk_email(
        cls,
        company,
        subject: str,
        body: str,
        email_type: str = EmailType.GENERAL_ANNOUNCEMENT,
        click_action_url: str = "",
        role_name: str = None,
        created_by=None,
        template_name: str = "general.html",
    ) -> int:
        """
        Send email to all users in a company.
        Optionally filter by role name.
        """
        from company.models import UserCompany

        queryset = UserCompany.objects.filter(
            company=company, is_active=True
        ).select_related("user")

        if role_name:
            queryset = queryset.filter(role__name=role_name)

        users = [uc.user for uc in queryset]
        email_logs = cls.send_email_to_group(
            users=users,
            subject=subject,
            body=body,
            email_type=email_type,
            click_action_url=click_action_url,
            company=company,
            created_by=created_by,
            template_name=template_name,
        )
        return len(email_logs)

    @classmethod
    def send_email_by_permission(
        cls,
        permission_codename: str,
        subject: str,
        body: str,
        email_type: str = EmailType.GENERAL_ANNOUNCEMENT,
        click_action_url: str = "",
        company=None,
        extra_data: dict = None,
        created_by=None,
        template_name: str = "general.html",
    ) -> int:
        """
        Send email to all active users who have a specific permission.
        Permission can be assigned directly or via a group.
        """
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        User = get_user_model()

        users = User.objects.filter(
            Q(user_permissions__codename=permission_codename) |
            Q(groups__permissions__codename=permission_codename),
            is_active=True,
        ).distinct()

        if company:
            from company.models import UserCompany
            company_user_ids = UserCompany.objects.filter(
                company=company, is_active=True
            ).values_list("user_id", flat=True)
            users = users.filter(id__in=company_user_ids)

        email_logs = cls.send_email_to_group(
            users=users,
            subject=subject,
            body=body,
            email_type=email_type,
            click_action_url=click_action_url,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
            template_name=template_name,
        )
        return len(email_logs)

    @classmethod
    def send_email_by_auth_group(
        cls,
        group_name: str,
        subject: str,
        body: str,
        email_type: str = EmailType.GENERAL_ANNOUNCEMENT,
        click_action_url: str = "",
        company=None,
        extra_data: dict = None,
        created_by=None,
        template_name: str = "general.html",
    ) -> int:
        """
        Send email to all active users in a Django auth group.
        """
        from django.contrib.auth import get_user_model

        User = get_user_model()

        users = User.objects.filter(
            groups__name=group_name,
            is_active=True,
        ).distinct()

        if company:
            from company.models import UserCompany
            company_user_ids = UserCompany.objects.filter(
                company=company, is_active=True
            ).values_list("user_id", flat=True)
            users = users.filter(id__in=company_user_ids)

        email_logs = cls.send_email_to_group(
            users=users,
            subject=subject,
            body=body,
            email_type=email_type,
            click_action_url=click_action_url,
            company=company,
            extra_data=extra_data,
            created_by=created_by,
            template_name=template_name,
        )
        return len(email_logs)
