import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import Department
from notifications.models import NotificationType
from notifications.services import NotificationService
from ..models import LabourVerificationRequest, DepartmentLabourResponse

logger = logging.getLogger(__name__)


class VerificationService:

    @staticmethod
    @transaction.atomic
    def create_request(user, request_obj=None):
        today = timezone.now().date()

        if LabourVerificationRequest.objects.filter(date=today).exists():
            raise ValidationError("Verification request already exists for today.")

        verification_request = LabourVerificationRequest.objects.create(
            date=today,
            created_by=user,
        )

        # Find departments that have active users with the submit permission
        from django.contrib.auth import get_user_model
        User = get_user_model()

        eligible_users = User.objects.filter(
            Q(user_permissions__codename="can_submit_department_labour") |
            Q(groups__permissions__codename="can_submit_department_labour"),
            is_active=True,
            department__isnull=False,
        ).distinct().select_related("department")

        department_ids = set()
        target_users = []
        for u in eligible_users:
            department_ids.add(u.department_id)
            target_users.append(u)

        departments = Department.objects.filter(id__in=department_ids)

        # Pre-create pending responses
        responses = [
            DepartmentLabourResponse(
                verification_request=verification_request,
                department=dept,
                status="PENDING",
            )
            for dept in departments
        ]
        DepartmentLabourResponse.objects.bulk_create(responses)

        # Send notifications
        company = None
        if request_obj and hasattr(request_obj, "company"):
            company = request_obj.company.company

        if target_users:
            NotificationService.send_notification_to_group(
                users=target_users,
                title="Labour Verification Required",
                body=f"Please submit labour count for your department for {today.strftime('%d %b %Y')}.",
                notification_type=NotificationType.LABOUR_VERIFICATION_REQUESTED,
                click_action_url=f"/gate/labour/verification/respond/{verification_request.id}",
                company=company,
                extra_data={
                    "reference_type": "labour_verification_request",
                    "reference_id": str(verification_request.id),
                },
                created_by=user,
            )

        logger.info(
            f"Labour verification request created for {today} by {user.email}. "
            f"Notified {len(target_users)} users across {len(department_ids)} departments."
        )

        return verification_request

    @staticmethod
    @transaction.atomic
    def submit_response(verification_request_id, user, data):
        if not user.department_id:
            raise ValidationError("You are not assigned to any department.")

        try:
            verification_request = LabourVerificationRequest.objects.get(
                id=verification_request_id
            )
        except LabourVerificationRequest.DoesNotExist:
            raise ValidationError("Verification request not found.")

        if verification_request.status == "CLOSED":
            raise ValidationError("This verification request has been closed.")

        try:
            response = DepartmentLabourResponse.objects.get(
                verification_request=verification_request,
                department_id=user.department_id,
            )
        except DepartmentLabourResponse.DoesNotExist:
            raise ValidationError(
                "No response entry found for your department on this request."
            )

        response.labour_count = data["labour_count"]
        response.labour_details = data.get("labour_details", [])
        response.remarks = data.get("remarks", "")
        response.status = "SUBMITTED"
        response.submitted_by = user
        response.submitted_at = timezone.now()
        response.save()

        # Notify the gate person who created the request
        if verification_request.created_by and verification_request.created_by != user:
            NotificationService.send_notification_to_user(
                user=verification_request.created_by,
                title=f"{response.department.name} Submitted",
                body=f"{response.department.name} reported {response.labour_count} labourers.",
                notification_type=NotificationType.LABOUR_VERIFICATION_SUBMITTED,
                click_action_url=f"/gate/labour/verification/{verification_request.id}",
                extra_data={
                    "reference_type": "labour_verification_response",
                    "reference_id": str(response.id),
                },
                created_by=user,
            )

        return response

    @staticmethod
    @transaction.atomic
    def close_request(verification_request_id, user):
        try:
            verification_request = LabourVerificationRequest.objects.get(
                id=verification_request_id
            )
        except LabourVerificationRequest.DoesNotExist:
            raise ValidationError("Verification request not found.")

        if verification_request.status == "CLOSED":
            raise ValidationError("This verification request is already closed.")

        verification_request.status = "CLOSED"
        verification_request.closed_at = timezone.now()
        verification_request.save()

        return verification_request
