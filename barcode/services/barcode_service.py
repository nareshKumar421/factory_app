import logging
import re
from decimal import Decimal as D
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    BarcodeAuditLog, BarcodeAuditTransactionType,
    BarcodeSequence, Pallet, Box, PalletMovement, BoxMovement, LooseStock,
    LooseStockConsumption, PalletBoxHistory,
    PalletStatus, BoxStatus, LooseStockStatus,
    PalletMovementType, BoxMovementType, DismantleReason,
)

logger = logging.getLogger(__name__)


class BarcodeService:
    LINE_KEY_MAX_LENGTH = 32
    SEQUENCE_BOX = 'BOX'
    SEQUENCE_PALLET = 'PALLET'

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
        """Sanitize line name for use in barcode IDs."""
        if not line:
            return 'XX'
        line_key = re.sub(r'[^A-Za-z0-9_-]+', '_', str(line).strip())
        line_key = re.sub(r'_+', '_', line_key).strip('_-')
        if not line_key:
            return 'XX'
        return line_key[:BarcodeService.LINE_KEY_MAX_LENGTH]

    @staticmethod
    def _clean_production_line(line: str) -> str:
        """Keep stored line labels within the model field limit."""
        return str(line or '').strip()[:50]

    def _existing_next_value(self, sequence_type: str, date_str: str, line_key: str) -> int:
        """Find the next value from existing records when a sequence row is created."""
        if sequence_type == self.SEQUENCE_BOX:
            model = Box
            field_name = 'box_barcode'
            prefix = f"BOX-{date_str}-{line_key}-"
        else:
            model = Pallet
            field_name = 'pallet_id'
            prefix = f"PLT-{date_str}-{line_key}-"

        last = (
            model.objects
            .filter(**{f'{field_name}__startswith': prefix})
            .order_by(f'-{field_name}')
            .values_list(field_name, flat=True)
            .first()
        )
        if last:
            return int(last.split('-')[-1]) + 1
        return 1

    def _reserve_sequence(self, sequence_type: str, date_str: str,
                          line_key: str, count: int = 1) -> int:
        """
        Reserve a contiguous range for barcode IDs.

        The sequence row is locked for the current transaction, so parallel
        label generation cannot receive the same number range.
        """
        sequence, _ = (
            BarcodeSequence.objects
            .select_for_update()
            .get_or_create(
                company=self.company,
                sequence_type=sequence_type,
                date_str=date_str,
                line_key=line_key,
                defaults={
                    'next_value': self._existing_next_value(
                        sequence_type, date_str, line_key
                    )
                },
            )
        )
        # The sequence row is company-scoped, but barcode fields are globally
        # unique. Keep stale/recreated sequences ahead of any existing global
        # barcode with the same date and line prefix.
        start = max(
            sequence.next_value,
            self._existing_next_value(sequence_type, date_str, line_key),
        )
        sequence.next_value = start + count
        sequence.save(update_fields=['next_value', 'updated_at'])
        return start

    def _next_box_seq(self, date_str: str, line_key: str, count: int = 1) -> int:
        """Get the next available sequence number for box barcodes."""
        return self._reserve_sequence(self.SEQUENCE_BOX, date_str, line_key, count)

    def _next_pallet_id(self, date_str: str, line: str) -> str:
        """Generate next pallet ID: PLT-YYYYMMDD-LINE-NNN"""
        line_key = self._sanitize_line(line)
        seq = self._reserve_sequence(self.SEQUENCE_PALLET, date_str, line_key)
        return f"PLT-{date_str}-{line_key}-{seq:03d}"

    def _legacy_next_box_seq(self, date_str: str, line_key: str) -> int:
        """Fallback helper kept for troubleshooting old data."""
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

    def _build_box_barcode_data(self, box):
        """Store only the unique box reference used by printed/scanned barcodes."""
        return {"barcode": box.box_barcode}

    def _build_pallet_barcode_data(self, pallet):
        """Store only the unique pallet reference used by printed/scanned barcodes."""
        return {"barcode": pallet.pallet_id}

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
        line = self._clean_production_line(data.get('production_line', ''))
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

        # Reserve a contiguous sequence range once, then increment for each box.
        start_seq = self._next_box_seq(date_str, line_key, box_count)
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
        BarcodeAuditLog.objects.bulk_create([
            BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.MANUFACTURED,
                to_company=self.company,
                user=user,
                notes=f"Manufactured in {self.company.code}",
            )
            for box in created_boxes
        ])

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
            # Accept a single status or a comma-separated list (e.g. "ACTIVE,PARTIAL")
            statuses = [s.strip() for s in str(filters['status']).split(',') if s.strip()]
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)
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
            notes=reason,
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
        Create a generic empty pallet for the QR workflow.

        Boxes are attached later by the pallet QR print flow or the explicit
        add-boxes endpoint. Pallet creation itself must not connect item,
        batch, quantity, or box records.
        """
        box_ids = data.get('box_ids') or []
        if box_ids:
            raise ValueError(
                "Create the pallet first, then attach boxes from the pallet QR print workflow."
            )

        data['production_line'] = self._clean_production_line(
            data.get('production_line', '')
        )
        return self._create_empty_pallet(data, user)

    def _create_empty_pallet(self, data: dict, user) -> Pallet:
        line = self._clean_production_line(data.get('production_line', ''))
        mfg_date = data.get('mfg_date') or timezone.now().date()
        exp_date = data.get('exp_date') or mfg_date
        date_str = mfg_date.strftime('%Y%m%d') if hasattr(mfg_date, 'strftime') else str(mfg_date).replace('-', '')
        pallet_id = self._next_pallet_id(date_str, line or 'XX')

        production_run = None
        run_id = data.get('production_run_id')
        if run_id:
            from production_execution.models import ProductionRun
            try:
                production_run = ProductionRun.objects.get(
                    id=run_id, company=self.company
                )
            except ProductionRun.DoesNotExist:
                raise ValueError(f"Production run {run_id} not found.")

        pallet = Pallet.objects.create(
            company=self.company,
            pallet_id=pallet_id,
            item_code='',
            item_name='',
            batch_number='',
            box_count=0,
            max_box_count=int(data.get('max_box_count') or 0),
            total_qty=D('0'),
            uom='',
            mfg_date=mfg_date,
            exp_date=exp_date,
            production_run=production_run,
            production_line=line,
            current_warehouse=data.get('warehouse', ''),
            status=PalletStatus.ACTIVE,
            created_by=user,
        )
        pallet.barcode_data = self._build_pallet_barcode_data(pallet)
        pallet.save(update_fields=['barcode_data'])

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.CREATE,
            to_warehouse=data.get('warehouse', ''),
            quantity=pallet.total_qty,
            performed_by=user,
            notes='Empty pallet created for QR workflow',
        )
        logger.info(f"Empty pallet {pallet_id} created by {user}")
        return pallet

    @transaction.atomic
    def ensure_pallet_boxes(self, pallet_id: int, target_box_count: int, user) -> list[Box]:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot print boxes for pallet with status {pallet.status}.")
        if target_box_count < 1:
            raise ValueError("Box count must be greater than zero.")

        active_boxes = list(
            pallet.boxes
            .filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
            .order_by('box_barcode')
        )
        if len(active_boxes) > target_box_count:
            raise ValueError(
                f"Pallet already has {len(active_boxes)} boxes; box count cannot be lower."
            )

        if pallet.max_box_count != target_box_count:
            pallet.max_box_count = target_box_count
            pallet.save(update_fields=['max_box_count', 'updated_at'])

        missing_count = target_box_count - len(active_boxes)
        if missing_count:
            date_str = pallet.mfg_date.strftime('%Y%m%d')
            line_key = self._sanitize_line(pallet.production_line or 'XX')
            start_seq = self._next_box_seq(date_str, line_key, missing_count)
            qty_per_box = (
                pallet.total_qty / D(str(target_box_count))
                if pallet.total_qty and target_box_count
                else D('0')
            )

            new_boxes = []
            for i in range(missing_count):
                box = Box(
                    company=self.company,
                    box_barcode=f"BOX-{date_str}-{line_key}-{start_seq + i:04d}",
                    item_code=pallet.item_code,
                    item_name=pallet.item_name,
                    batch_number=pallet.batch_number,
                    qty=qty_per_box,
                    uom=pallet.uom,
                    mfg_date=pallet.mfg_date,
                    exp_date=pallet.exp_date,
                    pallet=pallet,
                    production_run=pallet.production_run,
                    production_line=pallet.production_line,
                    current_warehouse=pallet.current_warehouse,
                    status=BoxStatus.ACTIVE,
                    created_by=user,
                )
                new_boxes.append(box)

            Box.objects.bulk_create(new_boxes)
            created_boxes = list(
                Box.objects
                .filter(company=self.company, box_barcode__in=[b.box_barcode for b in new_boxes])
                .select_related('pallet')
                .order_by('box_barcode')
            )
            BoxMovement.objects.bulk_create([
                BoxMovement(
                    company=self.company,
                    box=box,
                    movement_type=BoxMovementType.CREATE,
                    to_warehouse=pallet.current_warehouse,
                    to_pallet=pallet,
                    performed_by=user,
                )
                for box in created_boxes
            ])
            active_boxes.extend(created_boxes)

        self._recalculate_pallet(pallet)
        boxes = list(
            pallet.boxes
            .filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
            .select_related('pallet')
            .order_by('box_barcode')
        )
        for box in boxes:
            box.barcode_data = self._build_box_barcode_data(box)
        Box.objects.bulk_update(boxes, ['barcode_data'])

        pallet.refresh_from_db()
        pallet.barcode_data = self._build_pallet_barcode_data(pallet)
        pallet.save(update_fields=['barcode_data', 'updated_at'])
        return boxes

    # ==================================================================
    # PALLET — List / Detail / Void
    # ==================================================================

    def list_pallets(self, **filters):
        qs = Pallet.objects.filter(
            company=self.company
        ).select_related('created_by')

        if filters.get('status'):
            # Accept a single status or a comma-separated list (e.g. "ACTIVE,CLEARED")
            statuses = [s.strip() for s in str(filters['status']).split(',') if s.strip()]
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)
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
    def delete_empty_pallet(self, pallet_id: int) -> str:
        pallet = self.get_pallet(pallet_id)
        pallet_code = pallet.pallet_id

        if pallet.boxes.exists():
            raise ValueError("Only empty pallets can be deleted. This pallet has boxes attached.")
        if pallet.dispatch_session_id or pallet.dispatched_at:
            raise ValueError("This pallet cannot be deleted because it is linked with dispatch.")
        if pallet.dispatch_scanned_units.exists():
            raise ValueError("This pallet cannot be deleted because it has dispatch scan history.")

        pallet.delete()
        logger.info(f"Empty pallet {pallet_code} deleted")
        return pallet_code

    @transaction.atomic
    def void_pallet(self, pallet_id: int, reason: str, user,
                    box_ids: list[int] | None = None) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status == PalletStatus.VOID:
            raise ValueError("Pallet is already void.")

        pallet.status = PalletStatus.VOID
        pallet.save(update_fields=['status', 'updated_at'])

        # Boxes selected in box_ids are VOIDed along with the pallet; the rest
        # are only disassociated and survive as ACTIVE loose boxes.
        void_ids = set(box_ids or [])
        active_boxes = list(pallet.boxes.filter(status=BoxStatus.ACTIVE))
        box_movements = []
        for box in active_boxes:
            box.pallet = None
            if box.id in void_ids:
                box.status = BoxStatus.VOID
                movement_type = BoxMovementType.VOID
            else:
                movement_type = BoxMovementType.DEPALLETIZE
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=movement_type,
                from_warehouse=box.current_warehouse,
                from_pallet=pallet,
                performed_by=user,
                notes=reason if box.id in void_ids else '',
            ))
        Box.objects.bulk_update(active_boxes, ['status', 'pallet', 'updated_at'])
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
                    notes: str, user, to_bin: str = '') -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot move pallet with status {pallet.status}.")

        from_warehouse = pallet.current_warehouse
        from_bin = pallet.current_bin
        # A move must change the destination — either the warehouse, or the bin
        # within the same (own) warehouse.
        if from_warehouse == to_warehouse and (from_bin or '') == (to_bin or ''):
            raise ValueError("Source and destination are the same.")

        pallet.current_warehouse = to_warehouse
        pallet.current_bin = to_bin
        pallet.save(update_fields=['current_warehouse', 'current_bin', 'updated_at'])

        # Move all active boxes on this pallet too
        active_boxes = list(pallet.boxes.filter(
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        box_movements = []
        for box in active_boxes:
            box.current_warehouse = to_warehouse
            box.current_bin = to_bin
            box_movements.append(BoxMovement(
                company=self.company,
                box=box,
                movement_type=BoxMovementType.MOVE,
                from_warehouse=from_warehouse,
                to_warehouse=to_warehouse,
                from_bin=from_bin,
                to_bin=to_bin,
                performed_by=user,
            ))
        Box.objects.bulk_update(active_boxes, ['current_warehouse', 'current_bin', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        PalletMovement.objects.create(
            company=self.company,
            pallet=pallet,
            movement_type=PalletMovementType.MOVE,
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            from_bin=from_bin,
            to_bin=to_bin,
            quantity=pallet.total_qty,
            performed_by=user,
            notes=notes,
        )

        # Mirror into the Warehouse Ops (wms) inventory so the moved pallet shows
        # on the WMS map when it lands in an own-warehouse bin.
        self._sync_wms_pallet_inventory(pallet)

        dest = f"{to_warehouse}/{to_bin}" if to_bin else to_warehouse
        logger.info(f"Pallet {pallet.pallet_id} moved {from_warehouse} → {dest} by {user}")
        return pallet

    def _sync_wms_pallet_inventory(self, pallet) -> None:
        """Reflect a barcode pallet into the Warehouse Ops (`wms`) inventory domain.

        Warehouse Ops renders its own `inventory` collection, which is separate
        from barcode Box/Pallet stock. So a pallet moved through the barcode module
        would not appear on the WMS map. This upserts a mirror inventory record
        (keyed by the barcode pallet) at the chosen internal location when the
        pallet sits in an OWN warehouse bin, and removes the mirror when it leaves
        WMS-tracked space (moved to a SAP warehouse, emptied, or no matching bin).
        """
        from django.utils import timezone
        from wms.models import (
            Inventory as WmsInventory,
            Location as WmsLocation,
            Warehouse as WmsWarehouse,
        )

        record_id = f"bc-pallet-{pallet.id}"

        def drop():
            WmsInventory.objects.filter(
                company=self.company, record_id=record_id
            ).delete()

        # Only mirror an active pallet that has a bin in an own WMS warehouse.
        if (
            pallet.status != PalletStatus.ACTIVE
            or not pallet.current_bin
            or (pallet.box_count or 0) <= 0
        ):
            return drop()

        warehouse = WmsWarehouse.objects.filter(
            company=self.company, data__sapWarehouseCode=pallet.current_warehouse
        ).first()
        if not warehouse:
            return drop()
        location = WmsLocation.objects.filter(
            company=self.company,
            data__warehouseId=warehouse.record_id,
            data__code=pallet.current_bin,
        ).first()
        if not location:
            return drop()

        now = timezone.now().isoformat()
        existing = WmsInventory.objects.filter(
            company=self.company, record_id=record_id
        ).first()
        created_at = (existing.data.get('createdAt') if existing and existing.data else None) or now
        WmsInventory.objects.update_or_create(
            company=self.company,
            record_id=record_id,
            defaults={'data': {
                'id': record_id,
                'locationId': location.record_id,
                'itemCode': pallet.item_code or '',
                'itemName': pallet.item_name or '',
                'palletId': None,
                'lotNumber': pallet.batch_number or '',
                'serialNumber': '',
                'quantity': float(pallet.total_qty or 0),
                'uom': pallet.uom or '',
                'boxCount': pallet.box_count or 0,
                'weight': None,
                'volume': None,
                'expiryDate': pallet.exp_date.isoformat() if pallet.exp_date else None,
                'createdAt': created_at,
                'updatedAt': now,
                # Traceability back to the barcode pallet that owns this mirror.
                'sourcePalletId': pallet.pallet_id,
            }},
        )

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
        pallet.item_code = ''
        pallet.item_name = ''
        pallet.batch_number = ''
        pallet.box_count = 0
        pallet.max_box_count = 0
        pallet.total_qty = D('0')
        pallet.uom = ''
        pallet.production_run = None
        pallet.barcode_data = self._build_pallet_barcode_data(pallet)
        pallet.save(update_fields=[
            'status', 'item_code', 'item_name', 'batch_number',
            'box_count', 'max_box_count', 'total_qty', 'uom',
            'production_run', 'barcode_data', 'updated_at',
        ])

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
    # PALLET — Split (move some boxes to an existing empty pallet)
    # ==================================================================

    @transaction.atomic
    def split_pallet(self, pallet_id: int, box_ids: list[int],
                     target_pallet_id: int, user) -> Pallet:
        """Split selected boxes off into an existing empty pallet. Returns the target pallet."""
        pallet = self.get_pallet(pallet_id)
        if pallet.status != PalletStatus.ACTIVE:
            raise ValueError(f"Cannot split pallet with status {pallet.status}.")

        target_pallet = self.get_pallet(target_pallet_id)
        if target_pallet.id == pallet.id:
            raise ValueError("Target pallet must be different from source pallet.")
        if target_pallet.status not in (PalletStatus.ACTIVE, PalletStatus.CLEARED):
            raise ValueError(f"Cannot split into pallet with status {target_pallet.status}.")
        if target_pallet.box_count != 0 or target_pallet.boxes.exists():
            raise ValueError("Target pallet must be empty.")

        boxes = list(pallet.boxes.filter(
            id__in=box_ids, status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found or not active on this pallet.")
        if len(boxes) == pallet.box_count:
            raise ValueError("Cannot split all boxes — use move pallet instead.")

        self._prepare_pallet_for_receiving_boxes(target_pallet, boxes)

        split_qty = sum(b.qty for b in boxes)
        target_warehouse = target_pallet.current_warehouse

        # Move boxes to target pallet
        box_movements = []
        for box in boxes:
            box.pallet = target_pallet
            box.current_warehouse = target_warehouse
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
                to_warehouse=target_warehouse,
                to_pallet=target_pallet,
                performed_by=user,
            ))
        Box.objects.bulk_update(boxes, ['pallet', 'current_warehouse', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        # Update original pallet counts
        self._recalculate_pallet(pallet)
        self._recalculate_pallet(target_pallet)

        # Log on both pallets
        PalletMovement.objects.create(
            company=self.company, pallet=pallet,
            movement_type=PalletMovementType.SPLIT,
            from_warehouse=pallet.current_warehouse,
            quantity=split_qty, performed_by=user,
            notes=f"Split {len(boxes)} boxes to {target_pallet.pallet_id}",
        )
        PalletMovement.objects.create(
            company=self.company, pallet=target_pallet,
            movement_type=PalletMovementType.SPLIT,
            to_warehouse=target_warehouse,
            quantity=split_qty, performed_by=user,
            notes=f"Received {len(boxes)} boxes from split of {pallet.pallet_id}",
        )

        logger.info(f"Pallet {pallet.pallet_id} split: {len(boxes)} boxes → {target_pallet.pallet_id} by {user}")
        return target_pallet

    # ==================================================================
    # PALLET — Add / Remove boxes
    # ==================================================================

    @transaction.atomic
    def add_boxes_to_pallet(self, pallet_id: int, box_ids: list[int], user) -> Pallet:
        pallet = self.get_pallet(pallet_id)

        boxes = list(Box.objects.filter(
            id__in=box_ids, company=self.company,
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL],
            pallet__isnull=True,
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found, not active, or already on a pallet.")
        self._prepare_pallet_for_receiving_boxes(pallet, boxes)

        box_movements = []
        history_rows = []
        for box in boxes:
            from_warehouse = box.current_warehouse
            from_bin = box.current_bin
            box.pallet = pallet
            box.current_warehouse = pallet.current_warehouse
            box.current_bin = pallet.current_bin
            box.removed_from_pallet_at = None
            box.removed_from_pallet_reason = ''
            box_movements.append(BoxMovement(
                company=self.company, box=box,
                movement_type=BoxMovementType.PALLETIZE,
                from_warehouse=from_warehouse,
                to_warehouse=pallet.current_warehouse,
                from_bin=from_bin,
                to_bin=pallet.current_bin,
                to_pallet=pallet, performed_by=user,
            ))
            history_rows.append(PalletBoxHistory(
                company=self.company, pallet=pallet, box=box,
                action="BOX_ADDED_TO_PALLET",
                old_status=box.status, new_status=box.status,
                created_by=user,
            ))
        Box.objects.bulk_update(boxes, [
            'pallet', 'current_warehouse', 'current_bin',
            'removed_from_pallet_at', 'removed_from_pallet_reason', 'updated_at',
        ])
        BoxMovement.objects.bulk_create(box_movements)
        PalletBoxHistory.objects.bulk_create(history_rows)

        self._recalculate_pallet(pallet)

        PalletMovement.objects.create(
            company=self.company, pallet=pallet,
            movement_type=PalletMovementType.TRANSFER,
            to_warehouse=pallet.current_warehouse,
            to_bin=pallet.current_bin,
            quantity=sum(b.qty for b in boxes), performed_by=user,
            notes=f"Received {len(boxes)} boxes",
        )

        # Keep the WMS map mirror in step with the new box count/quantity.
        self._sync_wms_pallet_inventory(pallet)

        logger.info(f"Added {len(boxes)} boxes to pallet {pallet.pallet_id} by {user}")
        return pallet

    @transaction.atomic
    def remove_boxes_from_pallet(self, pallet_id: int, box_ids: list[int], user) -> Pallet:
        pallet = self.get_pallet(pallet_id)
        if pallet.status not in (PalletStatus.ACTIVE, PalletStatus.PARTIAL):
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
    # PALLET — Verify / Reconcile
    # ==================================================================

    @staticmethod
    def _reconcile_box_brief(box) -> dict:
        return {
            "id": box.id,
            "box_barcode": box.box_barcode,
            "item_code": box.item_code,
            "item_name": box.item_name,
            "batch_number": box.batch_number,
            "qty": str(box.qty),
            "uom": box.uom,
            "status": box.status,
            "current_warehouse": box.current_warehouse,
            "pallet_id": box.pallet.pallet_id if box.pallet_id else None,
        }

    def _compute_pallet_reconcile(self, pallet, scanned_barcodes: list[str]) -> dict:
        """Sort scanned barcodes against a pallet's expected boxes (no mutation)."""
        from .scan_service import ScanService  # local import avoids import cycle

        expected = list(
            pallet.boxes
            .filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
            .select_related('pallet')
            .order_by('box_barcode')
        )
        expected_ids = {b.id for b in expected}

        scan_lookup = ScanService(self.company_code)
        matched_ids: set[int] = set()
        foreign: list[dict] = []
        seen_raw: set[str] = set()

        for raw in scanned_barcodes:
            key = (raw or '').strip()
            if not key or key in seen_raw:
                continue
            seen_raw.add(key)

            # A scanned pallet label is neither a matched nor a foreign box.
            if key.upper().startswith('PLT-') or key.upper().startswith('PPLT'):
                foreign.append({'barcode': key, 'box_obj': None,
                                'reason': 'PALLET_LABEL', 'item_matches': False})
                continue

            box = scan_lookup._lookup_box(key)
            if not box:
                foreign.append({'barcode': key, 'box_obj': None,
                                'reason': 'NOT_FOUND', 'item_matches': False})
                continue

            if box.id in expected_ids:
                matched_ids.add(box.id)
                continue

            # Box exists but is not an active member of this pallet.
            if box.status == BoxStatus.VOID:
                reason = 'VOID'
            elif box.status == BoxStatus.DISPATCHED or box.dispatch_session_id:
                reason = 'DISPATCHED'
            elif box.pallet_id is None:
                reason = 'UNPALLETIZED'
            else:
                reason = 'ON_OTHER_PALLET'
            foreign.append({
                'barcode': key,
                'box_obj': box,
                'reason': reason,
                'item_matches': (
                    box.item_code == pallet.item_code
                    and box.batch_number == pallet.batch_number
                    and box.uom == pallet.uom
                ),
            })

        return {
            'expected': expected,
            'matched': [b for b in expected if b.id in matched_ids],
            'missing': [b for b in expected if b.id not in matched_ids],
            'foreign': foreign,
        }

    def _serialize_reconcile(self, pallet, compute: dict, unlabeled_count: int) -> dict:
        missing = compute['missing']

        # Deterministic label recovery: when the operator confirms exactly as many
        # physically-present-but-unlabeled boxes as there are missing records — and
        # those records share one item/batch/UOM — the missing records ARE the
        # unlabeled boxes, so they are safe to relabel without minting duplicates.
        unlabeled = max(int(unlabeled_count or 0), 0)
        recover_eligible = bool(
            unlabeled > 0
            and unlabeled == len(missing)
            and len({(b.item_code, b.batch_number, b.uom) for b in missing}) <= 1
        )

        foreign = [
            {
                'barcode': f['barcode'],
                'box': self._reconcile_box_brief(f['box_obj']) if f['box_obj'] else None,
                'current_pallet_code': (
                    f['box_obj'].pallet.pallet_id
                    if f['box_obj'] and f['box_obj'].pallet_id else None
                ),
                'reason': f['reason'],
                'item_matches_pallet': f['item_matches'],
            }
            for f in compute['foreign']
        ]

        return {
            'pallet': {
                'id': pallet.id,
                'pallet_id': pallet.pallet_id,
                'item_code': pallet.item_code,
                'item_name': pallet.item_name,
                'batch_number': pallet.batch_number,
                'status': pallet.status,
                'box_count': pallet.box_count,
            },
            'expected_count': len(compute['expected']),
            'scanned_count': len(compute['matched']) + len(foreign),
            'matched': [self._reconcile_box_brief(b) for b in compute['matched']],
            'missing': [self._reconcile_box_brief(b) for b in missing],
            'foreign': foreign,
            'unlabeled_count': unlabeled,
            'is_fully_reconciled': len(missing) == 0 and len(foreign) == 0,
            'recover': {
                'unlabeled_count': unlabeled,
                'eligible': recover_eligible,
                'boxes': [self._reconcile_box_brief(b) for b in missing] if recover_eligible else [],
            },
        }

    @transaction.atomic
    def _apply_pallet_reconcile(self, pallet, compute: dict, pull_foreign: bool,
                                drop_missing: bool, reason: str, user) -> dict:
        """Heal drift in one transaction: pull matching foreign boxes onto the
        pallet, drop missing boxes off it. Anything not safe to move is skipped
        and reported rather than silently ignored."""
        pulled: list[str] = []
        dropped: list[str] = []
        skipped: list[dict] = []

        if pull_foreign:
            pull_ids: list[int] = []
            for f in compute['foreign']:
                box = f['box_obj']
                # Only pull a live, same-item box that is loose or on another
                # pallet. Dispatched / void / wrong-item / unknown are left alone.
                if (
                    box is not None
                    and f['reason'] in ('ON_OTHER_PALLET', 'UNPALLETIZED')
                    and f['item_matches']
                ):
                    pull_ids.append(box.id)
                    pulled.append(box.box_barcode)
                else:
                    skipped.append({'barcode': f['barcode'], 'reason': f['reason']})
            if pull_ids:
                self.transfer_boxes(pull_ids, pallet.current_warehouse, pallet.id, user)

        if drop_missing and compute['missing']:
            drop_ids = [b.id for b in compute['missing']]
            self.remove_boxes_from_pallet(pallet.id, drop_ids, user)
            dropped = [b.box_barcode for b in compute['missing']]

        return {'pulled': pulled, 'dropped': dropped, 'skipped': skipped, 'reason': reason}

    def reconcile_pallet(self, pallet_id: int, scanned_barcodes: list[str],
                         unlabeled_count: int = 0, *, apply: bool = False,
                         pull_foreign: bool = False, drop_missing: bool = False,
                         reason: str = '', user=None) -> dict:
        """
        Compare a pallet's system-expected boxes against physically scanned
        barcodes, sorting into matched / missing / foreign buckets.

        With ``apply=False`` this is read-only and also flags which missing
        records are safe to relabel (see ``recover``). With ``apply=True`` it
        atomically heals drift — pulling matching foreign boxes on and/or
        dropping missing boxes off — then returns the fresh reconciliation.
        """
        pallet = self.get_pallet(pallet_id)
        applied = None

        if apply:
            if user is None:
                raise ValueError("A user is required to apply reconciliation.")
            if not (pull_foreign or drop_missing):
                raise ValueError("Select at least one reconcile action to apply.")
            compute = self._compute_pallet_reconcile(pallet, scanned_barcodes)
            applied = self._apply_pallet_reconcile(
                pallet, compute, pull_foreign, drop_missing, reason, user,
            )
            pallet = self.get_pallet(pallet_id)  # refresh post-mutation state

        compute = self._compute_pallet_reconcile(pallet, scanned_barcodes)
        result = self._serialize_reconcile(pallet, compute, unlabeled_count)
        if applied is not None:
            result['applied'] = applied
        return result

    # ==================================================================
    # BOX — Transfer (move boxes between warehouses/pallets)
    # ==================================================================

    @transaction.atomic
    def transfer_boxes(self, box_ids: list[int], to_warehouse: str,
                       to_pallet_id: int | None, user, to_bin: str = '') -> list[Box]:
        boxes = list(Box.objects.filter(
            id__in=box_ids, company=self.company,
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL],
        ))
        if len(boxes) != len(box_ids):
            raise ValueError("Some boxes not found or not active.")

        to_pallet = None
        if to_pallet_id:
            to_pallet = self.get_pallet(to_pallet_id)
            # Transfers may consolidate boxes onto a pallet past its nominal capacity.
            self._prepare_pallet_for_receiving_boxes(to_pallet, boxes, enforce_capacity=False)

        # Boxes adopt the destination location: an explicit bin if given, else the
        # target pallet's bin when consolidating onto a pallet.
        effective_bin = to_bin or (to_pallet.current_bin if to_pallet else '')

        affected_pallets = set()
        box_movements = []

        for box in boxes:
            from_warehouse = box.current_warehouse
            from_bin = box.current_bin
            old_pallet = box.pallet

            box.current_warehouse = to_warehouse
            box.current_bin = effective_bin
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
                from_bin=from_bin,
                to_bin=effective_bin,
                from_pallet=old_pallet,
                to_pallet=to_pallet,
                performed_by=user,
            ))

        Box.objects.bulk_update(boxes, ['current_warehouse', 'current_bin', 'pallet', 'updated_at'])
        BoxMovement.objects.bulk_create(box_movements)

        # Recalculate affected pallets, then keep their Warehouse Ops mirrors in
        # sync (box counts/qty changed, and source pallets may now be empty).
        for p in affected_pallets:
            self._recalculate_pallet(p)
            p.refresh_from_db()
            self._sync_wms_pallet_inventory(p)
        if to_pallet:
            self._recalculate_pallet(to_pallet)
            to_pallet.refresh_from_db()
            self._sync_wms_pallet_inventory(to_pallet)

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
        if pallet.status not in (PalletStatus.ACTIVE, PalletStatus.PARTIAL):
            raise ValueError(f"Cannot dismantle pallet with status {pallet.status}.")

        live = [BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        if box_ids:
            boxes = list(pallet.boxes.filter(id__in=box_ids, status__in=live))
            if len(boxes) != len(box_ids):
                raise ValueError("Some boxes not found or not active on this pallet.")
        else:
            boxes = list(pallet.boxes.filter(status__in=live))

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
            pallet.item_code = ''
            pallet.item_name = ''
            pallet.batch_number = ''
            pallet.max_box_count = 0
            pallet.total_qty = D('0')
            pallet.uom = ''
            pallet.production_run = None
            pallet.barcode_data = self._build_pallet_barcode_data(pallet)
            pallet.save(update_fields=[
                'status', 'item_code', 'item_name', 'batch_number',
                'max_box_count', 'total_qty', 'uom', 'production_run',
                'barcode_data', 'updated_at',
            ])

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
    def repack(self, item_code: str, qty, warehouse: str, user,
               batch_number: str = '', loose_ids: list[int] | None = None) -> Box:
        """
        Repack any quantity of an item's loose stock into a new box.

        The pool is all ACTIVE loose records of the item (any batch, any
        source box); consumption is FIFO by dismantle time. loose_ids
        optionally restricts the pool to chosen records (drawn FIFO among
        themselves). batch_number for the new box is operator-supplied;
        when blank it falls back to the single source batch, or 'MIXED'
        when sources span batches.
        """
        qty = D(str(qty))
        if qty <= 0:
            raise ValueError("Repack quantity must be positive.")

        pool_qs = LooseStock.objects.select_for_update().filter(
            company=self.company,
            item_code=item_code,
            status=LooseStockStatus.ACTIVE,
        )
        if loose_ids:
            pool_qs = pool_qs.filter(id__in=loose_ids)
        pool = list(pool_qs.order_by('created_at', 'id'))

        if loose_ids and len(pool) != len(set(loose_ids)):
            raise ValueError(
                "Some selected loose records were not found, are not active, "
                f"or belong to a different item than {item_code}."
            )
        if not pool:
            raise ValueError(f"No active loose stock for item {item_code}.")

        available = sum((ls.qty for ls in pool), D('0'))
        if qty > available:
            scope = "selected" if loose_ids else "available"
            raise ValueError(
                f"Repack quantity ({qty}) exceeds {scope} loose stock "
                f"({available}) for item {item_code}."
            )

        # Walk the pool FIFO and work out each record's draw
        consumed: list[tuple[LooseStock, D]] = []
        remaining = qty
        for ls in pool:
            if remaining <= 0:
                break
            use_qty = min(ls.qty, remaining)
            consumed.append((ls, use_qty))
            remaining -= use_qty

        first = consumed[0][0]
        batch_number = (batch_number or '').strip()
        if not batch_number:
            source_batches = {ls.batch_number for ls, _ in consumed}
            batch_number = source_batches.pop() if len(source_batches) == 1 else 'MIXED'

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
            batch_number=batch_number,
            qty=qty,
            uom=first.uom,
            mfg_date=timezone.now().date(),
            exp_date=timezone.now().date(),  # Will be overridden if source box has dates
            current_warehouse=warehouse,
            production_line='RP',
            status=BoxStatus.ACTIVE,
            created_by=user,
        )

        # Try to get real dates from the oldest consumed record's source box
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
        for ls, use_qty in consumed:
            LooseStockConsumption.objects.create(
                company=self.company,
                loose_stock=ls,
                box=new_box,
                qty=use_qty,
                created_by=user,
            )
            ls.qty -= use_qty
            if ls.qty <= 0:
                ls.qty = D('0')
                ls.status = LooseStockStatus.REPACKED
            ls.repacked_into_box = new_box
            ls.save(update_fields=['qty', 'status', 'repacked_into_box', 'updated_at'])

        logger.info(
            f"Repacked {qty} {first.uom} of {item_code} from {len(consumed)} loose "
            f"records into box {barcode} (batch {batch_number}) by {user}"
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

    def loose_stock_summary(self, search: str = '') -> list[dict]:
        """Pooled view of ACTIVE loose stock: one entry per item_code."""
        qs = LooseStock.objects.filter(
            company=self.company, status=LooseStockStatus.ACTIVE,
        )
        if search:
            qs = qs.filter(item_code__icontains=search)

        pools: dict[str, dict] = {}
        for ls in qs.order_by('created_at', 'id'):
            pool = pools.setdefault(ls.item_code, {
                'item_code': ls.item_code,
                'item_name': ls.item_name,
                'uom': ls.uom,
                'total_qty': D('0'),
                'record_count': 0,
                'batches': [],
                'warehouses': [],
            })
            pool['total_qty'] += ls.qty
            pool['record_count'] += 1
            if ls.batch_number and ls.batch_number not in pool['batches']:
                pool['batches'].append(ls.batch_number)
            if ls.current_warehouse and ls.current_warehouse not in pool['warehouses']:
                pool['warehouses'].append(ls.current_warehouse)
            if not pool['item_name'] and ls.item_name:
                pool['item_name'] = ls.item_name

        return sorted(pools.values(), key=lambda p: p['item_code'])

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

    def _prepare_pallet_for_receiving_boxes(
        self, pallet: Pallet, boxes: list[Box], *, enforce_capacity: bool = True
    ):
        """Validate and stamp pallet context before boxes are linked to it.

        ``enforce_capacity`` gates the ``max_box_count`` check. Palletization keeps it
        on; box transfers pass it off so an operator can consolidate boxes onto a
        pallet past its nominal capacity (the SAP-sourced count is often conservative).
        """
        if not boxes:
            raise ValueError("Select at least one box.")
        if pallet.status not in (PalletStatus.ACTIVE, PalletStatus.CLEARED):
            raise ValueError(f"Cannot add to pallet with status {pallet.status}.")

        first_box = boxes[0]
        for box in boxes:
            if (
                box.item_code != first_box.item_code or
                box.batch_number != first_box.batch_number or
                box.uom != first_box.uom
            ):
                raise ValueError(
                    "All boxes added to a pallet must have the same item, batch, and UOM."
                )

        current_boxes_exist = pallet.boxes.filter(
            status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL]
        ).exists()
        pallet_is_empty = pallet.box_count == 0 and not current_boxes_exist
        if pallet.status == PalletStatus.CLEARED and not pallet_is_empty:
            raise ValueError("Cleared pallet must be empty before it can be reused.")

        incoming_count = sum(1 for box in boxes if box.pallet_id != pallet.id)
        if (
            enforce_capacity
            and pallet.max_box_count
            and pallet.box_count + incoming_count > pallet.max_box_count
        ):
            raise ValueError(
                f"Pallet capacity exceeded. Maximum boxes allowed: {pallet.max_box_count}."
            )

        reuse_empty_pallet = pallet_is_empty and pallet.status in (
            PalletStatus.ACTIVE,
            PalletStatus.CLEARED,
        )
        pallet_has_item_context = bool(pallet.item_code or pallet.batch_number or pallet.uom)
        if pallet_has_item_context and not reuse_empty_pallet:
            for box in boxes:
                if (
                    box.item_code != pallet.item_code or
                    box.batch_number != pallet.batch_number or
                    box.uom != pallet.uom
                ):
                    raise ValueError(
                        "Box item, batch, or UOM does not match the target pallet."
                    )
            return

        if pallet_is_empty:
            pallet.item_code = first_box.item_code
            pallet.item_name = first_box.item_name
            pallet.batch_number = first_box.batch_number
            pallet.uom = first_box.uom
            pallet.mfg_date = first_box.mfg_date
            pallet.exp_date = first_box.exp_date
            if not pallet.production_line:
                pallet.production_line = first_box.production_line
            if not pallet.production_run:
                pallet.production_run = first_box.production_run
            if reuse_empty_pallet and pallet.status == PalletStatus.CLEARED:
                pallet.status = PalletStatus.ACTIVE
            pallet.barcode_data = self._build_pallet_barcode_data(pallet)
            pallet.save(update_fields=[
                'status', 'item_code', 'item_name', 'batch_number', 'uom',
                'mfg_date', 'exp_date', 'production_line',
                'production_run', 'barcode_data', 'updated_at',
            ])

    def _recalculate_pallet(self, pallet: Pallet):
        """Recalculate pallet counts and quantity from current box state."""
        active_boxes = pallet.boxes.filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
        dispatched_boxes = pallet.boxes.filter(status=BoxStatus.DISPATCHED)
        total_boxes = pallet.boxes.exclude(status=BoxStatus.VOID).count()
        pallet.box_count = active_boxes.count()
        pallet.total_boxes = total_boxes
        pallet.available_boxes = pallet.box_count
        pallet.dispatched_boxes = dispatched_boxes.count()
        agg = active_boxes.aggregate(total=Sum('qty'))
        pallet.total_qty = agg['total'] or D('0')
        if pallet.status != PalletStatus.DISPATCHED:
            if pallet.available_boxes and pallet.dispatched_boxes:
                pallet.status = PalletStatus.PARTIAL
            elif not pallet.available_boxes and total_boxes:
                pallet.status = PalletStatus.EMPTY
            elif pallet.available_boxes:
                pallet.status = PalletStatus.ACTIVE
        pallet.barcode_data = self._build_pallet_barcode_data(pallet)
        pallet.save(update_fields=[
            'status', 'box_count', 'total_boxes', 'available_boxes',
            'dispatched_boxes', 'total_qty', 'barcode_data', 'updated_at',
        ])

    @staticmethod
    def _get_box_number(box: Box) -> int | None:
        if not box.pallet_id:
            return None
        box_ids = list(
            box.pallet.boxes
            .filter(status__in=[BoxStatus.ACTIVE, BoxStatus.PARTIAL])
            .order_by('box_barcode')
            .values_list('id', flat=True)
        )
        try:
            return box_ids.index(box.id) + 1
        except ValueError:
            return None
