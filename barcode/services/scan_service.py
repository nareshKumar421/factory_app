import json
import logging

from ..models import (
    Box, Pallet, ScanLog,
    BoxStatus, PalletStatus,
    ScanType, EntityType, ScanResult,
)

logger = logging.getLogger(__name__)


class ScanService:

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
    # Process a single scan
    # ==================================================================

    def process_scan(self, barcode_raw: str, scan_type: str,
                     context_ref_type: str = '', context_ref_id: int = None,
                     user=None, device_info: str = '') -> dict:
        """
        Parse barcode, look up entity, log scan, return result.
        """
        parsed = self._parse_barcode(barcode_raw)
        entity_type = parsed.get('entity_type', EntityType.UNKNOWN)
        entity_id = ''
        entity_data = None
        result = ScanResult.NOT_FOUND

        if entity_type == EntityType.BOX:
            box = self._lookup_box(parsed.get('barcode', barcode_raw))
            if box:
                entity_id = str(box.id)
                entity_data = {
                    'id': box.id,
                    'box_barcode': box.box_barcode,
                    'item_code': box.item_code,
                    'item_name': box.item_name,
                    'batch_number': box.batch_number,
                    'qty': str(box.qty),
                    'uom': box.uom,
                    'status': box.status,
                    'current_warehouse': box.current_warehouse,
                    'pallet_id': box.pallet.pallet_id if box.pallet else None,
                }
                result = ScanResult.SUCCESS

        elif entity_type == EntityType.PALLET:
            pallet = self._lookup_pallet(parsed.get('barcode', barcode_raw))
            if pallet:
                entity_id = str(pallet.id)
                entity_data = {
                    'id': pallet.id,
                    'pallet_id': pallet.pallet_id,
                    'item_code': pallet.item_code,
                    'item_name': pallet.item_name,
                    'batch_number': pallet.batch_number,
                    'box_count': pallet.box_count,
                    'total_qty': str(pallet.total_qty),
                    'uom': pallet.uom,
                    'status': pallet.status,
                    'current_warehouse': pallet.current_warehouse,
                }
                result = ScanResult.SUCCESS

        else:
            # Try universal lookup
            box = self._lookup_box(barcode_raw)
            if box:
                entity_type = EntityType.BOX
                entity_id = str(box.id)
                entity_data = {
                    'id': box.id, 'box_barcode': box.box_barcode,
                    'item_code': box.item_code, 'item_name': box.item_name,
                    'qty': str(box.qty), 'status': box.status,
                    'current_warehouse': box.current_warehouse,
                }
                result = ScanResult.SUCCESS
            else:
                pallet = self._lookup_pallet(barcode_raw)
                if pallet:
                    entity_type = EntityType.PALLET
                    entity_id = str(pallet.id)
                    entity_data = {
                        'id': pallet.id, 'pallet_id': pallet.pallet_id,
                        'item_code': pallet.item_code, 'item_name': pallet.item_name,
                        'box_count': pallet.box_count, 'total_qty': str(pallet.total_qty),
                        'status': pallet.status, 'current_warehouse': pallet.current_warehouse,
                    }
                    result = ScanResult.SUCCESS

        # Log the scan
        scan_log = ScanLog.objects.create(
            company=self.company,
            scan_type=scan_type,
            barcode_raw=barcode_raw,
            barcode_parsed=parsed,
            entity_type=entity_type,
            entity_id=entity_id,
            scan_result=result,
            context_ref_type=context_ref_type,
            context_ref_id=context_ref_id,
            scanned_by=user,
            device_info=device_info,
        )

        return {
            'scan_id': scan_log.id,
            'result': result,
            'entity_type': entity_type,
            'entity_id': entity_id,
            'entity_data': entity_data,
            'barcode_raw': barcode_raw,
            'barcode_parsed': parsed,
        }

    # ==================================================================
    # Universal lookup (no logging)
    # ==================================================================

    def lookup_barcode(self, barcode_string: str) -> dict:
        """Look up any barcode without logging."""
        box = self._lookup_box(barcode_string)
        if box:
            return {
                'entity_type': EntityType.BOX,
                'entity_id': box.id,
                'entity_data': {
                    'id': box.id, 'box_barcode': box.box_barcode,
                    'item_code': box.item_code, 'item_name': box.item_name,
                    'batch_number': box.batch_number,
                    'qty': str(box.qty), 'uom': box.uom,
                    'status': box.status,
                    'current_warehouse': box.current_warehouse,
                    'pallet_id': box.pallet.pallet_id if box.pallet else None,
                },
            }

        pallet = self._lookup_pallet(barcode_string)
        if pallet:
            return {
                'entity_type': EntityType.PALLET,
                'entity_id': pallet.id,
                'entity_data': {
                    'id': pallet.id, 'pallet_id': pallet.pallet_id,
                    'item_code': pallet.item_code, 'item_name': pallet.item_name,
                    'batch_number': pallet.batch_number,
                    'box_count': pallet.box_count,
                    'total_qty': str(pallet.total_qty), 'uom': pallet.uom,
                    'status': pallet.status,
                    'current_warehouse': pallet.current_warehouse,
                },
            }

        return {'entity_type': EntityType.UNKNOWN, 'entity_id': None, 'entity_data': None}

    # ==================================================================
    # Scan history
    # ==================================================================

    def get_scan_history(self, **filters):
        qs = ScanLog.objects.filter(
            company=self.company
        ).select_related('scanned_by')

        if filters.get('scan_type'):
            qs = qs.filter(scan_type=filters['scan_type'])
        if filters.get('scan_result'):
            qs = qs.filter(scan_result=filters['scan_result'])
        if filters.get('entity_type'):
            qs = qs.filter(entity_type=filters['entity_type'])
        return qs

    # ==================================================================
    # Helpers
    # ==================================================================

    def _parse_barcode(self, raw: str) -> dict:
        """Try to parse QR JSON payload, or detect type from prefix."""
        # Try JSON (QR code)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                barcode_type = data.get('type', '').upper()
                if barcode_type == 'BOX':
                    return {
                        'entity_type': EntityType.BOX,
                        'barcode': data.get('box_barcode', ''),
                        **data,
                    }
                elif barcode_type == 'PALLET':
                    return {
                        'entity_type': EntityType.PALLET,
                        'barcode': data.get('pallet_id', ''),
                        **data,
                    }
        except (json.JSONDecodeError, TypeError):
            pass

        # Detect from prefix
        raw_stripped = raw.strip()
        raw_upper = raw_stripped.upper()

        # Our app's barcode format: BOX-YYYYMMDD-Line-NNNN or PLT-YYYYMMDD-Line-NNN
        if raw_upper.startswith('BOX-'):
            return {'entity_type': EntityType.BOX, 'barcode': raw_stripped}
        elif raw_upper.startswith('PLT-'):
            return {'entity_type': EntityType.PALLET, 'barcode': raw_stripped}

        # 1D barcode format from label printer: B{box_barcode_no_special} or P{pallet_id_no_special}
        # e.g. BBOX20260420Line_20001 → box_barcode = BOX-20260420-Line_2-0001
        if raw_upper.startswith('BBOX'):
            return {'entity_type': EntityType.BOX, 'barcode': raw_stripped}
        elif raw_upper.startswith('PPLT'):
            return {'entity_type': EntityType.PALLET, 'barcode': raw_stripped}

        return {'entity_type': EntityType.UNKNOWN, 'barcode': raw_stripped}

    def _lookup_box(self, barcode: str):
        stripped = barcode.strip()
        # Try exact match first
        try:
            return Box.objects.select_related('pallet').get(
                box_barcode=stripped, company=self.company
            )
        except Box.DoesNotExist:
            pass
        # Try 1D barcode value: B{box_barcode_stripped}
        # The 1D barcode is "B" + box_barcode with special chars removed
        # So if we receive "BBOX20260420Line_20001", the box_barcode is "BOX-20260420-Line_2-0001"
        if stripped.upper().startswith('B'):
            clean = stripped[1:]  # remove the B prefix
            # Search for any box whose barcode matches when special chars are stripped
            boxes = Box.objects.select_related('pallet').filter(company=self.company)
            for box in boxes.iterator():
                if box.box_barcode.replace('-', '').replace(' ', '') == clean.replace('-', '').replace(' ', ''):
                    return box
        return None

    def _lookup_pallet(self, barcode: str):
        stripped = barcode.strip()
        try:
            return Pallet.objects.get(
                pallet_id=stripped, company=self.company
            )
        except Pallet.DoesNotExist:
            pass
        # Try 1D barcode: P{pallet_id_stripped}
        if stripped.upper().startswith('P'):
            clean = stripped[1:]
            pallets = Pallet.objects.filter(company=self.company)
            for pallet in pallets.iterator():
                if pallet.pallet_id.replace('-', '').replace(' ', '') == clean.replace('-', '').replace(' ', ''):
                    return pallet
        return None
