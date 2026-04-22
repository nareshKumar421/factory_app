import logging
from decimal import Decimal as D
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    Pallet, Box, PalletMovement, BoxMovement, LooseStock,
    PalletStatus, BoxStatus, LooseStockStatus,
    PalletMovementType, BoxMovementType, DismantleReason,
)

logger = logging.getLogger(__name__)


class BarcodeService:

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
    # ID generation helpers
    # ==================================================================

    @staticmethod
    def _sanitize_line(line: str) -> str:
        """Sanitize line name for use in barcode IDs (replace spaces with _)."""
        return line.replace(' ', '_') if line else 'XX'

    def _next_box_seq(self, date_str: str, line_key: str) -> int:
        """Get the next available sequence number for box barcodes."""
        prefix = f"BOX-{date_str}-{line_key}-"
        last = (
            Box.objects
            .filter(company=self.company, box_barcode__startswith=prefix)
            .order_by('-box_barcode')
            .values_list('box_barcode', flat=True)
            .first()
        )
        if last:
            return int(last.split('-')[-1]) + 1
        return 1

    def _next_pallet_id(self, date_str: str, line: str) -> str:
        """Generate next pallet ID: PLT-YYYYMMDD-LINE-NNN"""
        line_key = self._sanitize_line(line)
        prefix = f"PLT-{date_str}-{line_key}-"
        last = (
            Pallet.objects
            .filter(company=self.company, pallet_id__startswith=prefix)
            .order_by('-pallet_id')
            .values_list('pallet_id', flat=True)
            .first()
        )
        if last:
            seq = int(last.split('-')[-1]) + 1
        else:
            seq = 1
        return f"{prefix}{seq:03d}"

    def _build_box_barcode_data(self, box):
        """Build the JSON payload that gets encoded in the QR code."""
        return {
            "type": "BOX",
            "box_barcode": box.box_barcode,
            "item_code": box.item_code,
            "batch": box.batch_number,
            "qty": str(box.qty),
            "uom": box.uom,
            "mfg_date": str(box.mfg_date),
            "exp_date": str(box.exp_date),
            "line": box.production_line,
            "warehouse": box.current_warehouse,
        }

    def _build_pallet_barcode_data(self, pallet):
        return {
            "type": "PALLET",
            "pallet_id": pallet.pallet_id,
            "item_code": pallet.item_code,
            "batch": pallet.batch_number,
            "box_count": pallet.box_count,
            "total_qty": str(pallet.total_qty),
            "uom": pallet.uom,
            "mfg_date": str(pallet.mfg_date),
            "exp_date": str(pallet.exp_date),
            "line": pallet.production_line,
            "warehouse": pallet.current_warehouse,
        }

    # ==================================================================
    # BOX — Generate
    # ==================================================================

    @transaction.atomic
    def generate_boxes(self, data: dict, user) -> list[Box]:
        """
        Bulk-generate box records for a given item + batch.
        Each box gets a unique barcode and a CREATE movement entry.
        """
        item_code = data['item_code']
        batch_number = data['batch_number']
        qty_per_box = D(str(data['qty']))
        box_count = int(data['box_count'])
        warehouse = data['warehouse']
        line = data.get('production_line', '')
        mfg_date = data['mfg_date']
        exp_date = data.get('exp_date') or mfg_date
        uom = data.get('uom', '')
        g_weight = data.get('g_weight')
        n_weight = data.get('n_weight')
        item_name = data.get('item_name', '')
        run_id = data.get('production_run_id')

        date_str = mfg_date.strftime('%Y%m%d') if hasattr(mfg_date, 'strftime') else str(mfg_date).replace('-', '')
        line_key = self._sanitize_line(line)

        production_run = None
        if run_id:
            from production_execution.models import ProductionRun
            try:
                production_run = ProductionRun.objects.get(
                    id=run_id, company=self.company
                )
            except ProductionRun.DoesNotExist:
                raise ValueError(f"Production run {run_id} not found.")

        # Get the starting sequence once, then increment for each box
        start_seq = self._next_box_seq(date_str, line_key)
        prefix = f"BOX-{date_str}-{line_key}-"

        boxes = []
        for i in range(box_count):
            barcode = f"{prefix}{start_seq + i:04d}"
            box = Box(
                company=self.company,
                box_barcode=barcode,
                item_code=item_code,
                item_name=item_name,
                batch_number=batch_number,
                qty=qty_per_box,
                uom=uom,
                g_weight=g_weight,
                n_weight=n_weight,
                mfg_date=mfg_date,
                exp_date=exp_date,
                production_run=production_run,
                production_line=line,
                current_warehouse=warehouse,
                status=BoxStatus.ACTIVE,
                created_by=user,
            )
            boxes.append(box)

        Box.objects.bulk_create(boxes)

        # Refresh to get IDs, then set barcode_data and create movements
        created_boxes = Box.objects.filter(
            company=self.company,
            box_barcode__in=[b.box_barcode for b in boxes]
        ).order_by('box_barcode')

        movements = []
        for box in created_boxes:
            box.barcode_data = self._build_box_barcode_data(box)
            movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.CREATE,
                to_warehouse=warehouse,
                performed_by=user,
            ))

        Box.objects.bulk_update(created_boxes, ['barcode_data'])
        BoxMovement.objects.bulk_create(movements)

        logger.info(
            f"Generated {len(created_boxes)} boxes for {item_code} "
            f"batch={batch_number} by {user}"
        )
        return list(created_boxes)

    # ==================================================================
    # BOX — List / Detail / Void
    # ==================================================================

    def list_boxes(self, **filters):
        qs = Box.objects.filter(
            company=self.company
        ).select_related('pallet', 'created_by')

        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        if filters.get('item_code'):
            qs = qs.filter(item_code=filters['item_code'])
        if filters.get('batch_number'):
            qs = qs.filter(batch_number=filters['batch_number'])
        if filters.get('warehouse'):
            qs = qs.filter(current_warehouse=filters['warehouse'])
        if filters.get('pallet_id'):
            qs = qs.filter(pallet_id=filters['pallet_id'])
        if filters.get('unpalletized'):
            qs = qs.filter(pallet__isnull=True)
        if filters.get('search'):
            from django.db.models import Q
            s = filters['search']
            qs = qs.filter(
                Q(box_barcode__icontains=s) |
                Q(item_code__icontains=s) |
                Q(item_name__icontains=s) |
                Q(batch_number__icontains=s)
            )
        return qs

    def get_box(self, box_id: int) -> Box:
        try:
            return (
                Box.objects
                .select_related('pallet', 'created_by', 'production_run')
                .prefetch_related('movements', 'movements__performed_by')
                .get(id=box_id, company=self.company)
            )
        except Box.DoesNotExist:
            raise ValueError(f"Box {box_id} not found.")

    @transaction.atomic
    def void_box(self, box_id: int, reason: str, user) -> Box:
        box = self.get_box(box_id)
        if box.status == BoxStatus.VOID:
            raise ValueError("Box is already void.")

        old_pallet = box.pallet
        box.status = BoxStatus.VOID
        box.pallet = None
        box.save(update_fields=['status', 'pallet', 'updated_at'])

        BoxMovement.objects.create(
            company=self.company,
            box=box,
            movement_type=BoxMovementType.VOID,
            from_warehouse=box.current_warehouse,
            from_pallet=old_pallet,
            performed_by=user,
        )

        # Update pallet counts if box was on a pallet
        if old_pallet and old_pallet.status == PalletStatus.ACTIVE:
            self._recalculate_pallet(old_pallet)

        logger.info(f"Box {box.box_barcode} voided by {user}: {reason}")
        return box

    # ==================================================================
    # PALLET — Create
    # ==================================================================

    @transaction.atomic
    def create_pallet(self, data: dict, user) -> Pallet:
        """
        Create a pallet by linking existing boxes to it.
        All boxes must be ACTIVE, unpalletized, and same item+batch.
        """
        box_ids = data['box_ids']
        warehouse = data['warehouse']
        line = data.get('production_line', '')
        run_id = data.get('production_run_id')

        boxes = list(
            Box.objects.filter(
                id__in=box_ids,
                company=self.company,
                status=BoxStatus.ACTIVE,
                pallet__isnull=True,
            )
        )

        if len(boxes) != len(box_ids):
            raise ValueError(
                "Some boxes not found, already on a pallet, or not active."
            )

        # Validate all boxes are same item + batch
        items = set((b.item_code, b.batch_number) for b in boxes)
        if len(items) > 1:
            raise ValueError("All boxes must be for the same item and batch.")

        first_box = boxes[0]
        total_qty = sum(b.qty for b in boxes)
        date_str = first_box.mfg_date.strftime('%Y%m%d')

        production_run = None
        if run_id:
            from production_execution.models import ProductionRun
            try:
                production_run = ProductionRun.objects.get(
                    id=run_id, company=self.company
                )
            except ProductionRun.DoesNotExist:
                raise ValueError(f"Production run {run_id} not found.")

        pallet_id = self._next_pallet_id(date_str, line or 'XX')
        pallet = Pallet.objects.create(
            company=self.company,
            pallet_id=pallet_id,
            item_code=first_box.item_code,
            item_name=first_box.item_name,
            batch_number=first_box.batch_number,
            box_count=len(boxes),
            total_qty=total_qty,
            uom=first_box.uom,
            mfg_date=first_box.mfg_date,
            exp_date=first_box.exp_date,
            production_run=production_run or first_box.production_run,
            production_line=line or first_box.production_line,
            current_warehouse=warehouse,
            status=PalletStatus.ACTIVE,
            created_by=user,
        )
        pallet.barcode_data = self._build_pallet_barcode_data(pallet)
        pallet.save(update_fields=['barcode_data'])

        # Link boxes to pallet
        box_movements = []
        for box in boxes:
            box.pallet = pallet
            box.current_warehouse = warehouse
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.PALLETIZE,
                to_warehouse=warehouse,
                to_pallet=pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'current_warehouse', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.CREATE,
            to_warehouse=warehouse,
            quantity=total_qty,
            performed_by=user,
        )

        logger.info(
            f"Pallet {pallet_id} created with {len(boxes)} boxes "
            f"by {user}"
        )
        return pallet

    # ==================================================================
    # PALLET — List / Detail / Void
    # ==================================================================

    def list_pallets(self, **filters):
        qs = Pallet.objects.filter(
            company=self.company
        ).select_related('created_by')

        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        if filters.get('item_code'):
            qs = qs.filter(item_code=filters['item_code'])
        if filters.get('batch_number'):
            qs = qs.filter(batch_number=filters['batch_number'])
        if filters.get('warehouse'):
            qs = qs.filter(current_warehouse=filters['warehouse'])
        if filters.get('search'):
            from django.db.models import Q
            s = filters['search']
            qs = qs.filter(
                Q(pallet_id__icontains=s) |
                Q(item_code__icontains=s) |
                Q(item_name__icontains=s) |
                Q(batch_number__icontains=s)
            )
        return qs

    def get_pallet(self, pallet_id: int) -> Pallet:
        try:
            return (
                Pallet.objects
                .select_related('created_by', 'production_run')
                .prefetch_related(
                    'boxes', 'boxes__created_by',
                    'movements', 'movements__performed_by',
                )
                .get(id=pallet_id, company=self.company)
            )
        except Pallet.DoesNotExist:
            raise ValueError(f"Pallet {pallet_id} not found.")

    @transaction.atomic
    def void_pallet(self, pallet_id: int, reason: str, user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status == PalletStatus.VOID:
            raise ValueError("Pallet is already void.")

        pallet.status = PalletStatus.VOID
        pallet.save(update_fields=['status', 'updated_at'])

        # Disassociate all active boxes
        active_boxes = list(pallet.boxes.filter(status=BoxStatus.ACTIVE))
        box_movements = []
        for box in active_boxes:
            box.pallet = None
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.DEPALLETIZE,
                from_warehouse=box.current_warehouse,
                from_pallet=pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(active_boxes, ['pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.VOID,
            from_warehouse=pallet.current_warehouse,
            quantity=pallet.total_qty,
            performed_by=user,
            notes=reason,
        )

        logger.info(f"Pallet {pallet.pallet_id} voided by {user}: {reason}")
        return pallet

    # ==================================================================
    # PALLET — Move (change warehouse)
    # ==================================================================

    @transaction.atomic
    def move_pallet(self, pallet_id: int, to_warehouse: str,
                    notes: str, user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot move pallet with status {pallet.status}.")

        from_warehouse = pallet.current_warehouse
        if from_warehouse == to_warehouse:
            raise ValueError("Source and destination warehouse are the same.")

        pallet.current_warehouse = to_warehouse
        pallet.save(update_fields=['current_warehouse', 'updated_at'])

        # Move all active boxes on this pallet too
        active_boxes = list(pallet.boxes.filter(
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        box_movements = []
        for box in active_boxes:
            box.current_warehouse = to_warehouse
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.MOVE,
                from_warehouse=from_warehouse,
                to_warehouse=to_warehouse,
                performed_by=user,
            ))
        Box.objects.bulk_update(active_boxes, ['current_warehouse', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.MOVE,
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            quantity=pallet.total_qty,
            performed_by=user,
            notes=notes,
        )

        logger.info(f"Pallet {pallet.pallet_id} moved {from_warehouse} → {to_warehouse} by {user}")
        return pallet

    # ==================================================================
    # PALLET — Clear (remove all boxes)
    # ==================================================================

    @transaction.atomic
    def clear_pallet(self, pallet_id: int, notes: str, user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot clear pallet with status {pallet.status}.")

        active_boxes = list(pallet.boxes.filter(
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        if not active_boxes:
            raise ValueError("Pallet has no active boxes to clear.")

        box_movements = []
        for box in active_boxes:
            box.pallet = None
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.DEPALLETIZE,
                from_warehouse=box.current_warehouse,
                from_pallet=pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(active_boxes, ['pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        cleared_qty = pallet.total_qty
        pallet.status = PalletStatus.CLEARED
        pallet.box_count = 0
        pallet.total_qty = D('0')
        pallet.save(update_fields=['status', 'box_count', 'total_qty', 'updated_at'])

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.CLEAR,
            from_warehouse=pallet.current_warehouse,
            quantity=cleared_qty,
            performed_by=user,
            notes=f"Cleared all {len(active_boxes)} boxes. {notes}".strip(),
        )

        logger.info(f"Pallet {pallet.pallet_id} cleared ({len(active_boxes)} boxes) by {user}")
        return pallet

    # ==================================================================
    # PALLET — Split (move some boxes to a new pallet)
    # ==================================================================

    @transaction.atomic
    def split_pallet(self, pallet_id: int, box_ids: list[int],
                     warehouse: str, user) -> Pallet:
        """Split selected boxes off into a new pallet. Returns the NEW pallet."""
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot split pallet with status {pallet.status}.")

        boxes = list(pallet.boxes.filter(
            id__in=box_ids, status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found or not active on this pallet.")
        if len(boxes) == pallet.box_count:
            raise ValueError("Cannot split all boxes — use move pallet instead.")

        # Create new pallet
        first = boxes[0]
        date_str = first.mfg_date.strftime('%Y%m%d')
        line_key = self._sanitize_line(pallet.production_line)
        new_pallet_id = self._next_pallet_id(date_str, line_key)
        split_qty = sum(b.qty for b in boxes)

        new_pallet = Pallet.objects.create(
            company=self.company,
            pallet_id=new_pallet_id,
            item_code=pallet.item_code,
            item_name=pallet.item_name,
            batch_number=pallet.batch_number,
            box_count=len(boxes),
            total_qty=split_qty,
            uom=pallet.uom,
            mfg_date=pallet.mfg_date,
            exp_date=pallet.exp_date,
            production_run=pallet.production_run,
            production_line=pallet.production_line,
            current_warehouse=warehouse,
            status=PalletStatus.ACTIVE,
            created_by=user,
        )
        new_pallet.barcode_data = self._build_pallet_barcode_data(new_pallet)
        new_pallet.save(update_fields=['barcode_data'])

        # Move boxes to new pallet
        box_movements = []
        for box in boxes:
            box.pallet = new_pallet
            box.current_warehouse = warehouse
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.DEPALLETIZE,
                from_warehouse=pallet.current_warehouse,
                from_pallet=pallet,
                performed_by=user,
            ))
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.PALLETIZE,
                to_warehouse=warehouse,
                to_pallet=new_pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'current_warehouse', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        # Update original pallet counts
        self._recalculate_pallet(pallet)

        # Log on both pallets
        PalletMovement.objects.create(
            company=self.company, pallet=pallet,
            movement_type=PalletMovementType.SPLIT,
            from_warehouse=pallet.current_warehouse,
            quantity=split_qty, performed_by=user,
            notes=f"Split {len(boxes)} boxes to {new_pallet_id}",
        )
        PalletMovement.objects.create(
            company=self.company, pallet=new_pallet,
            movement_type=PalletMovementType.CREATE,
            to_warehouse=warehouse,
            quantity=split_qty, performed_by=user,
            notes=f"Created from split of {pallet.pallet_id}",
        )

        logger.info(f"Pallet {pallet.pallet_id} split: {len(boxes)} boxes → {new_pallet_id} by {user}")
        return new_pallet

    # ==================================================================
    # PALLET — Add / Remove boxes
    # ==================================================================

    @transaction.atomic
    def add_boxes_to_pallet(self, pallet_id: int, box_ids: list[int], user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot add to pallet with status {pallet.status}.")

        boxes = list(Box.objects.filter(
            id__in=box_ids, company=self.company,
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL],
            pallet__isnull=True,
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found, not active, or already on a pallet.")

        # Validate same item + batch
        for box in boxes:
            if box.item_code != pallet.item_code or box.batch_number != pallet.batch_number:
                raise ValueError(
                    f"Box {box.box_barcode} has different item/batch than pallet."
                )

        box_movements = []
        for box in boxes:
            box.pallet = pallet
            box.current_warehouse = pallet.current_warehouse
            box_movements.append(BoxMovement(
                company=self.company, box=box,
                movement_type=BoxMovementType.PALLETIZE,
                to_warehouse=pallet.current_warehouse,
                to_pallet=pallet, performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'current_warehouse', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        self._recalculate_pallet(pallet)
        logger.info(f"Added {len(boxes)} boxes to pallet {pallet.pallet_id} by {user}")
        return pallet

    @transaction.atomic
    def remove_boxes_from_pallet(self, pallet_id: int, box_ids: list[int], user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot remove from pallet with status {pallet.status}.")

        boxes = list(pallet.boxes.filter(
            id__in=box_ids, status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found or not active on this pallet.")

        box_movements = []
        for box in boxes:
            box.pallet = None
            box_movements.append(BoxMovement(
                company=self.company, box=box,
                movement_type=BoxMovementType.DEPALLETIZE,
                from_warehouse=box.current_warehouse,
                from_pallet=pallet, performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        self._recalculate_pallet(pallet)

        PalletMovement.objects.create(
            company=self.company, pallet=pallet,
            movement_type=PalletMovementType.DISMANTLE,
            from_warehouse=pallet.current_warehouse,
            quantity=sum(b.qty for b in boxes), performed_by=user,
            notes=f"Removed {len(boxes)} boxes",
        )

        logger.info(f"Removed {len(boxes)} boxes from pallet {pallet.pallet_id} by {user}")
        return pallet

    # ==================================================================
    # BOX — Transfer (move boxes between warehouses/pallets)
    # ==================================================================

    @transaction.atomic
    def transfer_boxes(self, box_ids: list[int], to_warehouse: str,
                       to_pallet_id: int | None, user) -> list[Box]:
        boxes = list(Box.objects.filter(
            id__in=box_ids, company=self.company,
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL],
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found or not active.")

        to_pallet = None
        if to_pallet_id:
            to_pallet = Pallet.objects.get(id=to_pallet_id, company=self.company)
            if to_pallet.status != PalletStatus.ACTIVE:
                raise ValueError(f"Target pallet {to_pallet.pallet_id} is not active.")

        affected_pallets = set()
        box_movements = []

        for box in boxes:
            from_warehouse = box.current_warehouse
            old_pallet = box.pallet

            box.current_warehouse = to_warehouse
            if to_pallet:
                box.pallet = to_pallet
            elif box.pallet:
                # If moving to a different warehouse without target pallet, depalletize
                affected_pallets.add(box.pallet)
                box.pallet = None

            if old_pallet:
                affected_pallets.add(old_pallet)

            box_movements.append(BoxMovement(
                company=self.company, box=box,
                movement_type=BoxMovementType.TRANSFER,
                from_warehouse=from_warehouse,
                to_warehouse=to_warehouse,
                from_pallet=old_pallet,
                to_pallet=to_pallet,
                performed_by=user,
            ))

        Box.objects.bulk_update(boxes, ['current_warehouse', 'pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        # Recalculate affected pallets
        for p in affected_pallets:
            self._recalculate_pallet(p)
        if to_pallet:
            self._recalculate_pallet(to_pallet)

        logger.info(f"Transferred {len(boxes)} boxes to {to_warehouse} by {user}")
        return boxes

    # ==================================================================
    # DISMANTLE — Pallet → Loose Boxes
    # ==================================================================

    @transaction.atomic
    def dismantle_pallet(self, pallet_id: int, box_ids: list[int] | None,
                         reason: str, reason_notes: str, user) -> Pallet:
        """
        Dismantle a pallet — remove all or selected boxes.
        box_ids=None means dismantle ALL boxes.
        """
        pallet = self.get_pallet(pallet_id)
        if pallet.status not in (PalletStatus.ACTIVE,):
            raise ValueError(f"Cannot dismantle pallet with status {pallet.status}.")

        if box_ids:
            boxes = list(pallet.boxes.filter(id__in=box_ids, status=BoxStatus.ACTIVE))
            if len(boxes) != len(box_ids):
                raise ValueError("Some boxes not found or not active on this pallet.")
        else:
            boxes = list(pallet.boxes.filter(status=BoxStatus.ACTIVE))

        if not boxes:
            raise ValueError("No active boxes to dismantle.")

        box_movements = []
        for box in boxes:
            box.pallet = None
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.DEPALLETIZE,
                from_warehouse=box.current_warehouse,
                from_pallet=pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        self._recalculate_pallet(pallet)

        # If no active boxes left, clear the pallet; otherwise it's a partial dismantle
        is_fully_cleared = pallet.box_count == 0
        if is_fully_cleared:
            pallet.status = PalletStatus.CLEARED
            pallet.save(update_fields=['status', 'updated_at'])

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.CLEAR if is_fully_cleared else PalletMovementType.DISMANTLE,
            from_warehouse=pallet.current_warehouse,
            quantity=sum(b.qty for b in boxes),
            performed_by=user,
            notes=f"Removed {len(boxes)} boxes. {reason}: {reason_notes}".strip() if reason_notes else f"Removed {len(boxes)} boxes. {reason}",
        )

        logger.info(
            f"Pallet {pallet.pallet_id} dismantled ({len(boxes)} boxes) by {user}"
        )
        return pallet

    # ==================================================================
    # DISMANTLE — Box → Loose Items
    # ==================================================================

    @transaction.atomic
    def dismantle_box(self, box_id: int, loose_qty, reason: str,
                      reason_notes: str, user) -> LooseStock:
        """
        Dismantle a box fully or partially into loose stock.
        loose_qty = None or equal to box.qty → full dismantle.
        loose_qty < box.qty → partial (box qty reduced, status PARTIAL).
        """
        box = self.get_box(box_id)
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            raise ValueError(f"Cannot dismantle box with status {box.status}.")

        loose_qty = D(str(loose_qty)) if loose_qty is not None else box.qty

        if loose_qty <= 0:
            raise ValueError("Loose quantity must be positive.")
        if loose_qty > box.qty:
            raise ValueError(
                f"Loose quantity ({loose_qty}) exceeds box quantity ({box.qty})."
            )

        is_full = (loose_qty == box.qty)

        # Create loose stock record
        loose = LooseStock.objects.create(
            company=self.company,
            item_code=box.item_code,
            item_name=box.item_name,
            batch_number=box.batch_number,
            qty=loose_qty,
            original_qty=loose_qty,
            uom=box.uom,
            source_box=box,
            source_pallet=box.pallet,
            reason=reason,
            reason_notes=reason_notes,
            current_warehouse=box.current_warehouse,
            status=LooseStockStatus.ACTIVE,
            created_by=user,
        )

        # Update box
        if is_full:
            box.qty = D('0')
            box.status = BoxStatus.DISMANTLED
        else:
            box.qty -= loose_qty
            box.status = BoxStatus.PARTIAL
        box.save(update_fields=['qty', 'status', 'updated_at'])

        BoxMovement.objects.create(
            company=self.company,
            box=box,
            movement_type=BoxMovementType.DISMANTLE,
            from_warehouse=box.current_warehouse,
            performed_by=user,
        )

        # Update pallet counts if box was on a pallet
        if box.pallet and box.pallet.status == PalletStatus.ACTIVE:
            self._recalculate_pallet(box.pallet)

        logger.info(
            f"Box {box.box_barcode} dismantled: {loose_qty} {box.uom} → "
            f"loose #{loose.id} by {user}"
        )
        return loose

    # ==================================================================
    # REPACK — Loose Items → New Box
    # ==================================================================

    @transaction.atomic
    def repack(self, loose_ids: list[int], qty_per_loose: dict[int, str] | None,
               warehouse: str, user) -> Box:
        """
        Repack loose stock items into a new box.
        All loose items must be same item_code + batch.
        qty_per_loose: {loose_id: qty_to_use} — if None, uses full qty from each.
        """
        loose_items = list(
            LooseStock.objects.filter(
                id__in=loose_ids,
                company=self.company,
                status=LooseStockStatus.ACTIVE,
            )
        )
        if len(loose_items) != len(loose_ids):
            raise ValueError("Some loose stock records not found or not active.")

        # Validate same item + batch
        combos = set((ls.item_code, ls.batch_number) for ls in loose_items)
        if len(combos) > 1:
            raise ValueError("All loose stock must be the same item and batch.")

        first = loose_items[0]
        total_repack_qty = D('0')

        for ls in loose_items:
            use_qty = D(str(qty_per_loose.get(ls.id, str(ls.qty)))) if qty_per_loose else ls.qty
            if use_qty <= 0:
                raise ValueError(f"Qty for loose #{ls.id} must be positive.")
            if use_qty > ls.qty:
                raise ValueError(
                    f"Qty ({use_qty}) exceeds available loose qty ({ls.qty}) for #{ls.id}."
                )
            total_repack_qty += use_qty

        # Create the new box
        date_str = timezone.now().strftime('%Y%m%d')
        line_key = self._sanitize_line('RP')  # RP = repack
        start_seq = self._next_box_seq(date_str, line_key)
        barcode = f"BOX-{date_str}-{line_key}-{start_seq:04d}"

        new_box = Box.objects.create(
            company=self.company,
            box_barcode=barcode,
            item_code=first.item_code,
            item_name=first.item_name,
            batch_number=first.batch_number,
            qty=total_repack_qty,
            uom=first.uom,
            mfg_date=timezone.now().date(),
            exp_date=timezone.now().date(),  # Will be overridden if source box has dates
            current_warehouse=warehouse,
            production_line='RP',
            status=BoxStatus.ACTIVE,
            created_by=user,
        )

        # Try to get real dates from source box
        if first.source_box:
            new_box.mfg_date = first.source_box.mfg_date
            new_box.exp_date = first.source_box.exp_date
            new_box.save(update_fields=['mfg_date', 'exp_date'])

        new_box.barcode_data = self._build_box_barcode_data(new_box)
        new_box.save(update_fields=['barcode_data'])

        BoxMovement.objects.create(
            company=self.company,
            box=new_box,
            movement_type=BoxMovementType.CREATE,
            to_warehouse=warehouse,
            performed_by=user,
        )

        # Consume the loose stock
        for ls in loose_items:
            use_qty = D(str(qty_per_loose.get(ls.id, str(ls.qty)))) if qty_per_loose else ls.qty
            ls.qty -= use_qty
            if ls.qty <= 0:
                ls.qty = D('0')
                ls.status = LooseStockStatus.REPACKED
            ls.repacked_into_box = new_box
            ls.save(update_fields=['qty', 'status', 'repacked_into_box', 'updated_at'])

        logger.info(
            f"Repacked {total_repack_qty} {first.uom} from {len(loose_items)} loose "
            f"records into box {barcode} by {user}"
        )
        return new_box

    # ==================================================================
    # LOOSE STOCK — List / Detail
    # ==================================================================

    def list_loose_stock(self, **filters):
        qs = LooseStock.objects.filter(
            company=self.company
        ).select_related('source_box', 'source_pallet', 'repacked_into_box', 'created_by')

        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        if filters.get('item_code'):
            qs = qs.filter(item_code=filters['item_code'])
        if filters.get('warehouse'):
            qs = qs.filter(current_warehouse=filters['warehouse'])
        if filters.get('reason'):
            qs = qs.filter(reason=filters['reason'])
        if filters.get('search'):
            qs = qs.filter(item_code__icontains=filters['search'])
        return qs

    def get_loose_stock(self, loose_id: int) -> LooseStock:
        try:
            return LooseStock.objects.select_related(
                'source_box', 'source_pallet', 'repacked_into_box', 'created_by'
            ).get(id=loose_id, company=self.company)
        except LooseStock.DoesNotExist:
            raise ValueError(f"Loose stock {loose_id} not found.")

    # ==================================================================
    # Helpers
    # ==================================================================

    def _recalculate_pallet(self, pallet: Pallet):
        """Recalculate pallet box_count and total_qty from active/partial boxes."""
        active_boxes = pallet.boxes.filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
        pallet.box_count = active_boxes.count()
        agg = active_boxes.aggregate(total=Sum('qty'))
        pallet.total_qty = agg['total'] or D('0')
        pallet.save(update_fields=['box_count', 'total_qty', 'updated_at'])
