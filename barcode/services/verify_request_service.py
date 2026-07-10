import logging

from django.db import transaction
from django.utils import timezone

from notifications.models import NotificationType
from notifications.services import NotificationService

from ..models import (
    Pallet, PalletVerifyRequest,
    PalletVerifyRequestStatus, PalletVerifyRequestSource,
)
from .barcode_service import BarcodeService

logger = logging.getLogger(__name__)

# Barcode-team membership = access to the barcode module (the view_pallet perm).
BARCODE_TEAM_PERMISSION = 'view_pallet'

_CLOSED = (PalletVerifyRequestStatus.RESOLVED, PalletVerifyRequestStatus.CANCELLED)


class PalletVerifyRequestService:
    """Ticket workflow: non-team users raise pallet-verify requests, the barcode
    team resolves them (with reconcile powers). Notifications flow both ways."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._company = None

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @transaction.atomic
    def create_request(self, pallet_id: int, *, reason: str = '',
                       source: str = PalletVerifyRequestSource.MANUAL,
                       source_reference: str = '',
                       scanned_barcodes: list[str] | None = None,
                       unlabeled_count: int = 0, user=None) -> PalletVerifyRequest:
        try:
            pallet = Pallet.objects.get(id=pallet_id, company=self.company)
        except Pallet.DoesNotExist:
            raise ValueError(f"Pallet {pallet_id} not found.")

        # Snapshot the requester's findings via a read-only reconcile.
        findings = BarcodeService(self.company_code).reconcile_pallet(
            pallet_id,
            scanned_barcodes=scanned_barcodes or [],
            unlabeled_count=unlabeled_count,
        )

        request = PalletVerifyRequest.objects.create(
            company=self.company,
            pallet=pallet,
            status=PalletVerifyRequestStatus.OPEN,
            source=(source if source in PalletVerifyRequestSource.values
                    else PalletVerifyRequestSource.MANUAL),
            source_reference=source_reference or '',
            reason=reason or '',
            findings=findings,
            requested_by=user,
        )
        self._notify_team_of_new_request(request, user)
        logger.info(
            f"Pallet verify request #{request.id} raised for {pallet.pallet_id} by {user}"
        )
        return request

    # ------------------------------------------------------------------
    # List / detail
    # ------------------------------------------------------------------

    def list_requests(self, *, user, is_team: bool, **filters):
        qs = (
            PalletVerifyRequest.objects
            .filter(company=self.company)
            .select_related('pallet', 'requested_by', 'resolved_by')
        )
        # Requesters only see their own tickets; the team sees everything.
        if not is_team:
            qs = qs.filter(requested_by=user)

        status = filters.get('status')
        if status:
            statuses = [s.strip() for s in str(status).split(',') if s.strip()]
            qs = qs.filter(status__in=statuses) if len(statuses) > 1 else qs.filter(status=statuses[0])
        if filters.get('pallet_id'):
            qs = qs.filter(pallet_id=filters['pallet_id'])
        return qs

    def get_request(self, request_id: int) -> PalletVerifyRequest:
        try:
            return (
                PalletVerifyRequest.objects
                .select_related('pallet', 'requested_by', 'resolved_by')
                .get(id=request_id, company=self.company)
            )
        except PalletVerifyRequest.DoesNotExist:
            raise ValueError(f"Verify request {request_id} not found.")

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    @transaction.atomic
    def mark_in_progress(self, request_id: int, user) -> PalletVerifyRequest:
        request = self.get_request(request_id)
        if request.status == PalletVerifyRequestStatus.OPEN:
            request.status = PalletVerifyRequestStatus.IN_PROGRESS
            request.save(update_fields=['status', 'updated_at'])
        return request

    @transaction.atomic
    def resolve_request(self, request_id: int, resolution_note: str, user) -> PalletVerifyRequest:
        request = self.get_request(request_id)
        if request.status in _CLOSED:
            raise ValueError("This request is already closed.")
        request.status = PalletVerifyRequestStatus.RESOLVED
        request.resolution_note = resolution_note or ''
        request.resolved_by = user
        request.resolved_at = timezone.now()
        request.save(update_fields=[
            'status', 'resolution_note', 'resolved_by', 'resolved_at', 'updated_at',
        ])
        self._notify_requester_of_close(request, 'resolved', user)
        logger.info(f"Pallet verify request #{request.id} resolved by {user}")
        return request

    @transaction.atomic
    def cancel_request(self, request_id: int, reason: str, user) -> PalletVerifyRequest:
        request = self.get_request(request_id)
        if request.status in _CLOSED:
            raise ValueError("This request is already closed.")
        request.status = PalletVerifyRequestStatus.CANCELLED
        request.resolution_note = reason or ''
        request.resolved_by = user
        request.resolved_at = timezone.now()
        request.save(update_fields=[
            'status', 'resolution_note', 'resolved_by', 'resolved_at', 'updated_at',
        ])
        self._notify_requester_of_close(request, 'cancelled', user)
        logger.info(f"Pallet verify request #{request.id} cancelled by {user}")
        return request

    # ------------------------------------------------------------------
    # Notifications (best-effort — never block the workflow)
    # ------------------------------------------------------------------

    def _notify_team_of_new_request(self, request, user):
        try:
            NotificationService.send_notification_by_permission(
                permission_codename=BARCODE_TEAM_PERMISSION,
                title="Pallet verify requested",
                body=(
                    f"A verify/reconcile request was raised for pallet "
                    f"{request.pallet.pallet_id}."
                    + (f" Reason: {request.reason}" if request.reason else "")
                ),
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                click_action_url=f"/barcode/verify/{request.id}",
                reference_type="pallet_verify_request",
                reference_id=request.id,
                company=self.company,
                created_by=user,
            )
        except Exception as exc:  # best-effort: never block the request
            logger.error(f"Failed to notify barcode team of verify request {request.id}: {exc}")

    def _notify_requester_of_close(self, request, outcome, user):
        recipient = request.requested_by
        if not recipient:
            return
        try:
            NotificationService.send_notification_to_user(
                user=recipient,
                title=f"Pallet verify {outcome}",
                body=(
                    f"Your verify request for pallet {request.pallet.pallet_id} was {outcome}."
                    + (f" Note: {request.resolution_note}" if request.resolution_note else "")
                ),
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                click_action_url=f"/barcode/verify/{request.id}",
                reference_type="pallet_verify_request",
                reference_id=request.id,
                company=self.company,
                created_by=user,
            )
        except Exception as exc:  # best-effort: never block the transition
            logger.error(f"Failed to notify requester of verify request {request.id}: {exc}")
