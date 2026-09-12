"""The approval route to activation: no scan, a supervisor's decision instead.

Raised from the label-printing page for labels that legitimately cannot be
received at the gate — the scanner is down, the pallet went straight to a truck,
a label was reprinted after the trolley had already passed. Without this the only
way in is a physical scan, and one broken scanner would stop a shift's output
being dispatchable.

It is also the route that can put phantom stock back, because nothing physical is
proven. So the mitigation is visibility rather than friction: a reason is
required, the decision is attributed, and every box it activates is stamped
``activation_source=APPROVAL`` — which makes "what is in this godown that nobody
ever scanned in?" a one-filter question.

Mirrors ``PalletVerifyRequestService`` (ticket shape, notifications both ways) so
the frontend reuses the same components.
"""
import logging

from django.db import transaction
from django.utils import timezone

from notifications.models import NotificationType
from notifications.services import NotificationService

from ..models import (
    BarcodeActivationRequest,
    BarcodeActivationRequestLine,
    BarcodeActivationRequestStatus,
    Box,
    BoxStatus,
    Pallet,
)
from .activation_service import ActivationService

logger = logging.getLogger(__name__)

# Who is told a request is waiting. The approve permission, not the barcode
# module at large: a notification everyone gets is a notification nobody reads.
APPROVER_PERMISSION = 'can_approve_barcode_activation'

_CLOSED = (
    BarcodeActivationRequestStatus.APPROVED,
    BarcodeActivationRequestStatus.REJECTED,
    BarcodeActivationRequestStatus.CANCELLED,
)


class BarcodeActivationRequestService:

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
    def create_request(self, *, reason: str, pallet_id: int | None = None,
                       box_ids: list[int] | None = None, user=None
                       ) -> BarcodeActivationRequest:
        """Ask for boxes to be activated without a receive scan.

        Scope is either a pallet (everything still pending on it) or an explicit
        list of boxes — the label page knows exactly what it just printed.
        """
        reason = (reason or '').strip()
        if not reason:
            raise ValueError("A reason is required to request activation.")

        pallet = None
        if pallet_id:
            try:
                pallet = Pallet.objects.get(id=pallet_id, company=self.company)
            except Pallet.DoesNotExist:
                raise ValueError(f"Pallet {pallet_id} not found.")

        boxes = self._resolve_scope(pallet, box_ids)
        if not boxes:
            raise ValueError(
                "Nothing to activate — these labels are already active or voided."
            )

        # One open request per box: two requests for the same labels would make
        # the second approval a silent no-op and read as a broken button.
        already = (
            BarcodeActivationRequestLine.objects
            .filter(
                box__in=boxes,
                request__company=self.company,
                request__status=BarcodeActivationRequestStatus.OPEN,
            )
            .select_related('box')
        )
        if already.exists():
            open_ids = sorted({row.request_id for row in already})
            raise ValueError(
                "Some of these labels are already awaiting approval "
                f"(request #{open_ids[0]})."
            )

        warehouses = {(b.current_warehouse or '').strip().upper() for b in boxes}
        request = BarcodeActivationRequest.objects.create(
            company=self.company,
            status=BarcodeActivationRequestStatus.OPEN,
            warehouse=', '.join(sorted(w for w in warehouses if w))[:20],
            pallet=pallet,
            reason=reason,
            requested_by=user,
        )
        BarcodeActivationRequestLine.objects.bulk_create([
            BarcodeActivationRequestLine(request=request, box=box) for box in boxes
        ])
        self._notify_approvers(request, len(boxes), user)
        logger.info(
            "Activation request #%s raised for %s boxes by %s",
            request.id, len(boxes), user,
        )
        return request

    def _resolve_scope(self, pallet, box_ids) -> list[Box]:
        qs = Box.objects.filter(company=self.company, status=BoxStatus.PENDING)
        if pallet is not None:
            return list(qs.filter(pallet=pallet).order_by('box_barcode'))
        if box_ids:
            return list(qs.filter(id__in=box_ids).order_by('box_barcode'))
        raise ValueError("Select a pallet or at least one box.")

    # ------------------------------------------------------------------
    # List / detail
    # ------------------------------------------------------------------

    def list_requests(self, *, user, is_approver: bool, **filters):
        qs = (
            BarcodeActivationRequest.objects
            .filter(company=self.company)
            .select_related('pallet', 'requested_by', 'decided_by')
            .prefetch_related('lines')
        )
        # A requester sees their own tickets; an approver sees the queue.
        if not is_approver:
            qs = qs.filter(requested_by=user)

        status = filters.get('status')
        if status:
            statuses = [s.strip() for s in str(status).split(',') if s.strip()]
            qs = qs.filter(status__in=statuses) if len(statuses) > 1 else qs.filter(status=statuses[0])
        if filters.get('warehouse'):
            qs = qs.filter(warehouse__icontains=filters['warehouse'])
        return qs

    def get_request(self, request_id: int) -> BarcodeActivationRequest:
        try:
            return (
                BarcodeActivationRequest.objects
                .select_related('pallet', 'requested_by', 'decided_by')
                .prefetch_related('lines__box')
                .get(id=request_id, company=self.company)
            )
        except BarcodeActivationRequest.DoesNotExist:
            raise ValueError(f"Activation request {request_id} not found.")

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    @transaction.atomic
    def approve(self, request_id: int, *, note: str = '', user=None
                ) -> tuple[BarcodeActivationRequest, list[Box]]:
        request = self.get_request(request_id)
        if request.status in _CLOSED:
            raise ValueError("This request is already closed.")

        lines = list(request.lines.select_related('box'))
        boxes = [line.box for line in lines if line.box.status == BoxStatus.PENDING]
        activated = ActivationService(self.company_code).activate_by_approval(
            boxes, user=user,
            reference=f"Approved activation request #{request.id}",
        )

        activated_ids = {box.id for box in activated}
        for line in lines:
            line.activated = line.box_id in activated_ids
        BarcodeActivationRequestLine.objects.bulk_update(lines, ['activated'])

        request.status = BarcodeActivationRequestStatus.APPROVED
        request.decision_note = note or ''
        request.decided_by = user
        request.decided_at = timezone.now()
        request.save(update_fields=[
            'status', 'decision_note', 'decided_by', 'decided_at', 'updated_at',
        ])
        self._notify_requester(request, 'approved', user, count=len(activated))
        logger.info(
            "Activation request #%s approved by %s (%s boxes)",
            request.id, user, len(activated),
        )
        return request, activated

    @transaction.atomic
    def reject(self, request_id: int, *, note: str = '', user=None
               ) -> BarcodeActivationRequest:
        request = self.get_request(request_id)
        if request.status in _CLOSED:
            raise ValueError("This request is already closed.")
        request.status = BarcodeActivationRequestStatus.REJECTED
        request.decision_note = note or ''
        request.decided_by = user
        request.decided_at = timezone.now()
        request.save(update_fields=[
            'status', 'decision_note', 'decided_by', 'decided_at', 'updated_at',
        ])
        self._notify_requester(request, 'rejected', user)
        return request

    @transaction.atomic
    def cancel(self, request_id: int, *, note: str = '', user=None
               ) -> BarcodeActivationRequest:
        """Withdrawn by the requester — e.g. the boxes got scanned in after all."""
        request = self.get_request(request_id)
        if request.status in _CLOSED:
            raise ValueError("This request is already closed.")
        request.status = BarcodeActivationRequestStatus.CANCELLED
        request.decision_note = note or ''
        request.decided_by = user
        request.decided_at = timezone.now()
        request.save(update_fields=[
            'status', 'decision_note', 'decided_by', 'decided_at', 'updated_at',
        ])
        return request

    # ------------------------------------------------------------------
    # Notifications (best-effort — never block the workflow)
    # ------------------------------------------------------------------

    def _notify_approvers(self, request, box_count, user):
        try:
            NotificationService.send_notification_by_permission(
                permission_codename=APPROVER_PERMISSION,
                title="Barcode activation requested",
                body=(
                    f"{box_count} printed label(s) for "
                    f"{request.warehouse or 'a warehouse'} are waiting for activation. "
                    f"Reason: {request.reason}"
                ),
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                click_action_url=f"/barcode/activation-approvals/{request.id}",
                reference_type="barcode_activation_request",
                reference_id=request.id,
                company=self.company,
                created_by=user,
            )
        except Exception as exc:  # noqa: BLE001 - never block the request
            logger.error(
                "Failed to notify approvers of activation request %s: %s",
                request.id, exc,
            )

    def _notify_requester(self, request, outcome, user, count=0):
        recipient = request.requested_by
        if not recipient:
            return
        try:
            NotificationService.send_notification_to_user(
                user=recipient,
                title=f"Activation {outcome}",
                body=(
                    f"Your activation request was {outcome}."
                    + (f" {count} label(s) are now active." if count else "")
                    + (f" Note: {request.decision_note}" if request.decision_note else "")
                ),
                notification_type=NotificationType.GENERAL_ANNOUNCEMENT,
                click_action_url=f"/barcode/activation-approvals/{request.id}",
                reference_type="barcode_activation_request",
                reference_id=request.id,
                company=self.company,
                created_by=user,
            )
        except Exception as exc:  # noqa: BLE001 - never block the transition
            logger.error(
                "Failed to notify requester of activation request %s: %s",
                request.id, exc,
            )
