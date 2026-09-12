"""Activation — turning a printed label into trusted stock.

Labels are printed on the production floor and pasted as the line runs. Labels
that never got pasted used to stay in the app as live stock in the finished-goods
godown, inflating box counts, dispatch expectations and the warehouse map. So a
printed label is now born ``PENDING`` and only two things can activate it:

1. **A receive scan at the godown gate** — the receiver picks a warehouse they
   manage and scans each pallet/box coming in. A box is activated only if it was
   printed *for that warehouse*; anything else is refused and logged.
2. **An approval** — raised from the label-printing page for labels that
   legitimately cannot be scanned, decided by a supervisor.

Every activation goes through this module. Nothing else in the codebase may move
a row out of ``PENDING`` — that single-writer rule is what makes the audit trail
(``activation_source`` on each row) trustworthy enough to answer "what is in this
godown that nobody ever scanned in?".

A pending box is refused by every stock-consuming flow already, because they all
gate on ``status in (ACTIVE, PARTIAL)``. This module only adds the *reason*, so
the operator is told what to do instead of just "wrong status".
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import (
    ActivationSource,
    BarcodeActivationSettings,
    BarcodeAuditLog,
    BarcodeAuditTransactionType,
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    Pallet,
    PalletMovement,
    PalletMovementType,
    PalletStatus,
    PalletVerifyRequestSource,
    ScanResult,
    ScanType,
)
from .pallet_state import recalculate_pallet_state
from .scan_service import ScanService

logger = logging.getLogger(__name__)


# Machine-readable refusals, carried on the outcome and stored on the ScanLog so
# the rejection reports can group them (grouping free text was not possible).
REJECT_NOT_FOUND = "BARCODE_NOT_FOUND"
REJECT_OTHER_COMPANY = "OTHER_COMPANY"
REJECT_NOT_PENDING = "NOT_PENDING"
REJECT_WAREHOUSE_MISMATCH = "WAREHOUSE_MISMATCH"
REJECT_COUNT_MISMATCH = "COUNT_MISMATCH"
REJECT_COUNT_REQUIRED = "COUNT_REQUIRED"
REJECT_NOTHING_PENDING = "NOTHING_PENDING"

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
NEEDS_COUNT = "NEEDS_COUNT"
NEEDS_BOX_SCAN = "NEEDS_BOX_SCAN"


def normalize_warehouse(value: str) -> str:
    return (value or "").strip().upper()


def not_activated_detail(label: str, warehouse: str) -> str:
    """Why a stock flow refused a pending unit, and what to do about it.

    Shared by every flow that can meet one (dock scan, barcode dispatch, BST,
    intercompany) so the operator reads the same instruction wherever they hit
    it, instead of a bare "wrong status" that gives them nowhere to go.
    """
    where = warehouse or "this warehouse"
    return (
        f"{label} was printed for {where} but never received there. "
        f"Scan it in on the warehouse Receive page, or get an activation approval."
    )


def receive_context(warehouse: str) -> str:
    """``ScanLog.context_ref_type`` for a receive scan, warehouse included.

    The warehouse is part of the key (not a separate column) so "what was
    received at BH-PF today" is an indexed lookup on the existing
    ``(context_ref_type, context_ref_id)`` index. Fits the 50-char column.
    """
    return f"RECEIVE:{normalize_warehouse(warehouse)}"[:50]


@dataclass
class ActivationOutcome:
    """Result of one receive scan. Business refusals are returned, not raised,
    so a receiver scanning a trolley of boxes is never interrupted mid-run."""

    status: str
    detail: str = ""
    code: str = ""
    entity_type: str = ""
    barcode: str = ""
    activated_boxes: list = field(default_factory=list)
    pallet: object = None
    pending_box_count: int = 0
    verify_request_id: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @property
    def activated_count(self) -> int:
        return len(self.activated_boxes)


class ActivationService:
    """Company-scoped activation. Construct with the acting company's code."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._company = None

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    # ==================================================================
    # Settings — is the rule on for this warehouse?
    # ==================================================================

    def get_settings(self) -> BarcodeActivationSettings:
        row, _ = BarcodeActivationSettings.objects.get_or_create(company=self.company)
        return row

    def requires_activation(self, warehouse: str) -> bool:
        """True when a label printed for ``warehouse`` must start inactive.

        Read at *generation* time. Turning the setting on does not retro-flip
        already-printed labels — that would strand stock that is physically in
        the godown with nobody able to prove it arrived.
        """
        try:
            return self.get_settings().requires_activation(warehouse)
        except Exception:  # noqa: BLE001 - a settings read must never block printing
            logger.exception("Activation settings lookup failed for %s", self.company_code)
            return False

    def initial_box_status(self, warehouse: str) -> str:
        return (
            BoxStatus.PENDING
            if self.requires_activation(warehouse)
            else BoxStatus.ACTIVE
        )

    def initial_pallet_status(self, warehouse: str) -> str:
        return (
            PalletStatus.PENDING
            if self.requires_activation(warehouse)
            else PalletStatus.ACTIVE
        )

    # ==================================================================
    # The single writer
    # ==================================================================

    @transaction.atomic
    def activate_boxes(self, boxes, *, warehouse: str, user,
                       source: str, reference: str = "",
                       enforce_warehouse: bool = True) -> list[Box]:
        """Activate ``boxes`` into ``warehouse``. Returns the boxes activated.

        Skips (rather than fails) a box that is no longer ``PENDING``: two
        receivers scanning the same pallet, or a gate scan landing between an
        approval request and its approval, are ordinary races and must not
        abort the whole batch.

        ``enforce_warehouse`` is only ever relaxed for the approval route, which
        activates each box into its own printed warehouse — there is no scanned
        warehouse to compare against.
        """
        target = normalize_warehouse(warehouse)
        activated = []
        movements = []
        audit_rows = []
        now = timezone.now()

        # Re-read under a row lock rather than trusting the caller's copies. Two
        # receivers working the same trolley, or a gate scan landing while an
        # approval is being decided, would otherwise both see PENDING and write
        # two activations for one box -- same end state, but a movement trail
        # that says the box was received twice.
        boxes = list(
            Box.objects
            .select_for_update()
            .select_related('pallet')
            .filter(id__in=[box.id for box in boxes], company=self.company)
        )

        for box in boxes:
            if box.status != BoxStatus.PENDING:
                continue
            box_warehouse = normalize_warehouse(box.current_warehouse)
            if enforce_warehouse and box_warehouse != target:
                continue
            box.status = BoxStatus.ACTIVE
            box.activated_at = now
            box.activated_by = user
            box.activation_source = source
            box.activation_warehouse = box_warehouse or target
            activated.append(box)
            movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.ACTIVATE,
                to_warehouse=box.activation_warehouse,
                to_pallet=box.pallet,
                performed_by=user,
                notes=reference or f"Activated ({source})",
            ))
            audit_rows.append(BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.ACTIVATED,
                to_company=self.company,
                user=user,
                notes=reference or f"Activated in {box.activation_warehouse} ({source})",
            ))

        if not activated:
            return []

        Box.objects.bulk_update(activated, [
            'status', 'activated_at', 'activated_by',
            'activation_source', 'activation_warehouse', 'updated_at',
        ])
        BoxMovement.objects.bulk_create(movements)
        BarcodeAuditLog.objects.bulk_create(audit_rows)

        # Settle every pallet the activated boxes belong to: a pallet whose boxes
        # are now (partly) real leaves PENDING and starts counting as stock.
        pallet_ids = {box.pallet_id for box in activated if box.pallet_id}
        for pallet in Pallet.objects.filter(id__in=pallet_ids):
            self._activate_pallet_row(
                pallet, warehouse=target, user=user, source=source, reference=reference
            )
            recalculate_pallet_state(
                self.company, pallet, user=user, note=reference or "Activated"
            )

        logger.info(
            "Activated %s boxes in %s via %s by %s",
            len(activated), target or "-", source, user,
        )
        return activated

    def _activate_pallet_row(self, pallet, *, warehouse, user, source, reference):
        """Stamp the pallet's own activation trail once its first box is real."""
        if pallet.activated_at:
            return
        pallet.activated_at = timezone.now()
        pallet.activated_by = user
        pallet.activation_source = source
        pallet.activation_warehouse = (
            normalize_warehouse(pallet.current_warehouse) or warehouse
        )
        # Leave `status` to recalculate_pallet_state -- it owns the transition and
        # knows about PARTIAL/EMPTY/DISPATCHED; setting it here would fight it.
        pallet.save(update_fields=[
            'activated_at', 'activated_by', 'activation_source',
            'activation_warehouse', 'updated_at',
        ])
        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.ACTIVATE,
            to_warehouse=pallet.activation_warehouse,
            to_bin=pallet.current_bin,
            performed_by=user,
            notes=reference or f"Activated ({source})",
        )

    # ==================================================================
    # Receive scan (the godown gate)
    # ==================================================================

    def scan_for_activation(self, barcode_raw: str, *, warehouse: str, user,
                            confirmed_box_count: int | None = None,
                            device_info: str = "") -> ActivationOutcome:
        """Resolve one scanned barcode and activate what it stands for."""
        target = normalize_warehouse(warehouse)
        raw = str(barcode_raw or '').strip()
        scan_service = ScanService(self.company_code)

        if not raw:
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_NOT_FOUND, detail="Barcode is required.",
            )

        resolved = scan_service.lookup_barcode(raw)
        entity_type = resolved.get('entity_type')
        entity_id = resolved.get('entity_id')

        if entity_type == 'BOX' and entity_id:
            return self._receive_box(
                Box.objects.select_related('pallet').get(id=entity_id),
                scan_service, raw, target, user, device_info,
            )
        if entity_type == 'PALLET' and entity_id:
            return self._receive_pallet(
                Pallet.objects.get(id=entity_id),
                scan_service, raw, target, user, device_info,
                confirmed_box_count=confirmed_box_count,
            )

        # Company-scoped lookup missed. Say *why* -- another company's barcode and
        # a typo are the same "not found" without this.
        miss = scan_service.explain_scan_miss(raw)
        code = (
            REJECT_OTHER_COMPANY
            if miss.get('code') == 'OTHER_COMPANY'
            else REJECT_NOT_FOUND
        )
        return self._log_and_reject(
            scan_service, raw, target, user, device_info,
            code=code, detail=miss.get('message', 'Barcode not recognised.'),
        )

    def _receive_box(self, box, scan_service, raw, target, user,
                     device_info) -> ActivationOutcome:
        if box.status != BoxStatus.PENDING:
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_NOT_PENDING,
                detail=self._already_active_detail(box),
                entity_type='BOX',
            )
        box_warehouse = normalize_warehouse(box.current_warehouse)
        if box_warehouse != target:
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_WAREHOUSE_MISMATCH,
                detail=(
                    f"Box {box.box_barcode} was printed for {box_warehouse or '(none)'}, "
                    f"but you are receiving into {target}."
                ),
                entity_type='BOX',
            )

        activated = self.activate_boxes(
            [box], warehouse=target, user=user,
            source=ActivationSource.GATE_SCAN,
            reference=f"Received at {target}",
        )
        self._log_success(scan_service, raw, target, user, device_info, 'BOX', box.id)
        return ActivationOutcome(
            status=ACCEPTED, entity_type='BOX', barcode=box.box_barcode,
            activated_boxes=activated, pallet=box.pallet,
            detail=f"Box {box.box_barcode} activated in {target}.",
        )

    def _receive_pallet(self, pallet, scan_service, raw, target, user, device_info,
                        *, confirmed_box_count) -> ActivationOutcome:
        pallet_warehouse = normalize_warehouse(pallet.current_warehouse)
        if pallet_warehouse != target:
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_WAREHOUSE_MISMATCH,
                detail=(
                    f"Pallet {pallet.pallet_id} was printed for "
                    f"{pallet_warehouse or '(none)'}, but you are receiving into {target}."
                ),
                entity_type='PALLET',
            )

        pending = list(
            pallet.boxes.filter(status=BoxStatus.PENDING, company=self.company)
        )
        if not pending:
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_NOTHING_PENDING,
                detail=(
                    f"Pallet {pallet.pallet_id} has no boxes waiting for activation "
                    f"(status {pallet.status})."
                ),
                entity_type='PALLET',
            )

        # The pallet's box rows were created when the labels were printed --
        # including labels that were never pasted onto a box. So the receiver's
        # physical count is the only thing that can tell a full pallet from a
        # short one, and a bare pallet scan must not be trusted on its own.
        if confirmed_box_count is None:
            return ActivationOutcome(
                status=NEEDS_COUNT, code=REJECT_COUNT_REQUIRED,
                entity_type='PALLET', barcode=pallet.pallet_id,
                pallet=pallet, pending_box_count=len(pending),
                detail=(
                    f"Pallet {pallet.pallet_id} carries {len(pending)} label(s). "
                    "Confirm how many boxes are physically on it."
                ),
            )

        confirmed = int(confirmed_box_count)
        if confirmed > len(pending):
            return self._log_and_reject(
                scan_service, raw, target, user, device_info,
                code=REJECT_COUNT_MISMATCH,
                detail=(
                    f"You counted {confirmed} boxes but pallet {pallet.pallet_id} "
                    f"only has {len(pending)} label(s) waiting. Scan the extra "
                    "boxes individually."
                ),
                entity_type='PALLET',
            )
        if confirmed < len(pending):
            # Short: some printed label on this pallet is not on a real box, and
            # nothing here can say which. Activate nothing, drop the pallet into
            # box-by-box scanning, and leave a ticket for the barcode team.
            verify_id = self._open_gate_verify_request(
                pallet, confirmed=confirmed, pending=len(pending), user=user
            )
            self._log_rejection(
                scan_service, raw, target, user, device_info,
                code=REJECT_COUNT_MISMATCH, entity_type='PALLET',
            )
            return ActivationOutcome(
                status=NEEDS_BOX_SCAN, code=REJECT_COUNT_MISMATCH,
                entity_type='PALLET', barcode=pallet.pallet_id,
                pallet=pallet, pending_box_count=len(pending),
                verify_request_id=verify_id,
                detail=(
                    f"Counted {confirmed} of {len(pending)} labels on pallet "
                    f"{pallet.pallet_id}. Scan each box on this pallet individually "
                    "so only the boxes that exist are activated."
                ),
            )

        activated = self.activate_boxes(
            pending, warehouse=target, user=user,
            source=ActivationSource.GATE_SCAN,
            reference=f"Received at {target} (pallet {pallet.pallet_id})",
        )
        self._log_success(
            scan_service, raw, target, user, device_info, 'PALLET', pallet.id
        )
        pallet.refresh_from_db()
        return ActivationOutcome(
            status=ACCEPTED, entity_type='PALLET', barcode=pallet.pallet_id,
            activated_boxes=activated, pallet=pallet,
            pending_box_count=len(pending),
            detail=(
                f"Pallet {pallet.pallet_id} activated in {target} "
                f"({len(activated)} boxes)."
            ),
        )

    # ==================================================================
    # Approval route
    # ==================================================================

    @transaction.atomic
    def activate_by_approval(self, boxes, *, user, reference: str = "") -> list[Box]:
        """Activate without a scan. Each box lands in its own printed warehouse.

        Grouped by warehouse so the trail records where each box actually went,
        rather than attributing them all to whichever warehouse came first.
        """
        by_warehouse: dict[str, list] = {}
        for box in boxes:
            by_warehouse.setdefault(
                normalize_warehouse(box.current_warehouse), []
            ).append(box)

        activated = []
        for warehouse, group in by_warehouse.items():
            activated.extend(self.activate_boxes(
                group, warehouse=warehouse, user=user,
                source=ActivationSource.APPROVAL,
                reference=reference or "Activated by approval",
                enforce_warehouse=False,
            ))
        return activated

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _already_active_detail(box) -> str:
        if box.status == BoxStatus.ACTIVE and box.activated_at:
            who = getattr(box.activated_by, 'full_name', None) or 'someone'
            when = box.activated_at.strftime('%d %b %H:%M')
            return (
                f"Box {box.box_barcode} was already activated on {when} by {who}."
            )
        return (
            f"Box {box.box_barcode} is {box.status} and is not waiting for activation."
        )

    def _open_gate_verify_request(self, pallet, *, confirmed, pending, user):
        """Park a short-count pallet in the queue the barcode team already watches."""
        try:
            from .verify_request_service import PalletVerifyRequestService
            request = PalletVerifyRequestService(self.company_code).create_request(
                pallet.id,
                reason=(
                    f"Godown receive: counted {confirmed} boxes against {pending} "
                    f"pending labels on {pallet.pallet_id}. Boxes were scanned "
                    "individually; the remaining labels were never received."
                ),
                source=PalletVerifyRequestSource.GATE,
                source_reference=f"Receive {normalize_warehouse(pallet.current_warehouse)}",
                user=user,
            )
            return request.id
        except Exception:  # noqa: BLE001 - a ticket must never block receiving
            logger.exception(
                "Could not open a gate verify request for pallet %s", pallet.pallet_id
            )
            return None

    def _log_success(self, scan_service, raw, warehouse, user, device_info,
                     entity_type, entity_id):
        """Audit an accepted receive scan.

        Best-effort: an audit write must not lose a receive the operator has
        already performed physically.
        """
        from ..models import ScanLog
        try:
            ScanLog.objects.create(
                company=self.company,
                scan_type=ScanType.ACTIVATE,
                barcode_raw=str(raw or '')[:500],
                barcode_parsed=scan_service.parse_barcode(raw),
                entity_type=entity_type,
                entity_id=str(entity_id),
                scan_result=ScanResult.SUCCESS,
                context_ref_type=receive_context(warehouse),
                scanned_by=user,
                device_info=device_info,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to log activation scan for %s", raw)

    def _log_rejection(self, scan_service, raw, warehouse, user, device_info,
                       *, code, entity_type='UNKNOWN', detail=''):
        """Audit a refused receive scan through the shared rejection logger, so
        receive failures group in the same report as dock and BST failures."""
        try:
            scan_service.log_rejection(
                raw,
                reject_code=code,
                reject_message=detail,
                scan_type=ScanType.ACTIVATE,
                context_ref_type=receive_context(warehouse),
                user=user,
                device_info=device_info,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to log activation rejection for %s", raw)

    def _log_and_reject(self, scan_service, raw, warehouse, user, device_info,
                        *, code, detail, entity_type='UNKNOWN') -> ActivationOutcome:
        self._log_rejection(
            scan_service, raw, warehouse, user, device_info,
            code=code, entity_type=entity_type, detail=detail,
        )
        return ActivationOutcome(
            status=REJECTED, code=code, detail=detail,
            entity_type=entity_type, barcode=raw,
        )

    # ==================================================================
    # Pending report — what was printed but never arrived
    # ==================================================================

    def pending_boxes(self, *, warehouse: str = '', search: str = '',
                      min_age_days: int | None = None):
        """Every label still waiting for activation, oldest print first."""
        qs = Box.objects.filter(
            company=self.company, status=BoxStatus.PENDING
        ).select_related('pallet')
        if warehouse:
            qs = qs.filter(current_warehouse__iexact=normalize_warehouse(warehouse))
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(box_barcode__icontains=search)
                | Q(item_code__icontains=search)
                | Q(item_name__icontains=search)
                | Q(batch_number__icontains=search)
                | Q(pallet__pallet_id__icontains=search)
            )
        if min_age_days is not None:
            cutoff = timezone.now() - timedelta(days=int(min_age_days))
            qs = qs.filter(created_at__lte=cutoff)
        return qs.order_by('created_at', 'box_barcode')

    def pending_groups(self, **filters) -> list[dict]:
        """The report rows: one per print batch (pallet, or item+batch+line).

        Grouped rather than listed flat because a shift prints thousands of
        labels and the question being asked is "which run never arrived?", not
        "which barcode".
        """
        from django.db.models import Count, Max, Min

        rows = (
            self.pending_boxes(**filters)
            .values(
                'pallet_id', 'pallet__pallet_id', 'item_code', 'item_name',
                'batch_number', 'current_warehouse', 'production_line',
            )
            .annotate(
                box_count=Count('id'),
                printed_at=Min('created_at'),
                first_barcode=Min('box_barcode'),
                last_barcode=Max('box_barcode'),
            )
            .order_by('printed_at')
        )
        now = timezone.now()
        out = []
        for row in rows:
            printed_at = row['printed_at']
            age_days = (now - printed_at).days if printed_at else 0
            out.append({
                'pallet_id': row['pallet_id'],
                'pallet_code': row['pallet__pallet_id'] or '',
                'item_code': row['item_code'],
                'item_name': row['item_name'],
                'batch_number': row['batch_number'],
                'warehouse': row['current_warehouse'],
                'production_line': row['production_line'],
                'box_count': row['box_count'],
                'printed_at': printed_at,
                'age_days': age_days,
                'first_barcode': row['first_barcode'],
                'last_barcode': row['last_barcode'],
            })
        return out

    @staticmethod
    def age_buckets(groups: list[dict]) -> list[dict]:
        """Aging summary over the report rows — the pressure to act on them."""
        buckets = [
            ('0-2 days', 0, 2),
            ('3-7 days', 3, 7),
            ('8-30 days', 8, 30),
            ('over 30 days', 31, None),
        ]
        out = []
        for label, low, high in buckets:
            count = sum(
                group['box_count'] for group in groups
                if group['age_days'] >= low and (high is None or group['age_days'] <= high)
            )
            out.append({'label': label, 'box_count': count})
        return out

    @transaction.atomic
    def void_pending(self, box_ids: list[int], *, reason: str, user) -> list[Box]:
        """Void labels that were printed but never arrived.

        Only ``PENDING`` boxes are touched — deliberately narrow, so a mis-typed
        id list on this screen can never void live stock. Boxes that have since
        been activated are skipped and reported back rather than failing the
        whole batch.
        """
        reason = (reason or '').strip()
        if not reason:
            raise ValueError("A reason is required to void pending labels.")

        boxes = list(
            Box.objects
            .select_related('pallet')
            .filter(id__in=box_ids, company=self.company, status=BoxStatus.PENDING)
        )
        if not boxes:
            return []

        pallets = {}
        movements = []
        for box in boxes:
            if box.pallet_id:
                pallets[box.pallet_id] = box.pallet
            movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.VOID,
                from_warehouse=box.current_warehouse,
                from_pallet=box.pallet,
                performed_by=user,
                notes=f"Never received: {reason}",
            ))
            box.status = BoxStatus.VOID
            box.pallet = None

        Box.objects.bulk_update(boxes, ['status', 'pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(movements)
        for pallet in pallets.values():
            recalculate_pallet_state(
                self.company, pallet, user=user, note="Pending labels voided"
            )
        logger.info(
            "Voided %s pending boxes by %s: %s", len(boxes), user, reason
        )
        return boxes

    # ==================================================================
    # Receive-session tally (the running count on the receive screen)
    # ==================================================================

    def receive_activity(self, *, warehouse: str, since=None) -> dict:
        """What this warehouse has received today: accepted, rejected, boxes."""
        from django.db.models import Count, Q as _Q
        from ..models import ScanLog

        since = since or timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        scans = ScanLog.objects.filter(
            company=self.company,
            scan_type=ScanType.ACTIVATE,
            context_ref_type=receive_context(warehouse),
            scanned_at__gte=since,
        )
        totals = scans.aggregate(
            accepted=Count('id', filter=_Q(scan_result=ScanResult.SUCCESS)),
            rejected=Count('id', filter=_Q(scan_result=ScanResult.REJECTED)),
        )
        boxes_activated = Box.objects.filter(
            company=self.company,
            activation_warehouse=normalize_warehouse(warehouse),
            activation_source=ActivationSource.GATE_SCAN,
            activated_at__gte=since,
        ).count()
        return {
            'warehouse': normalize_warehouse(warehouse),
            'since': since,
            'scans_accepted': totals['accepted'] or 0,
            'scans_rejected': totals['rejected'] or 0,
            'boxes_activated': boxes_activated,
        }
