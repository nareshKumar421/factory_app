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
        parsed = self._parse_barcode(barcode_string)
        entity_type = parsed.get('entity_type', EntityType.UNKNOWN)
        lookup_value = parsed.get('barcode') or barcode_string

        if entity_type in (EntityType.BOX, EntityType.UNKNOWN):
            box = self._lookup_box(lookup_value)
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

        if entity_type in (EntityType.PALLET, EntityType.UNKNOWN):
            pallet = self._lookup_pallet(lookup_value)
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
    # Miss diagnostics
    # ==================================================================

    def explain_scan_miss(self, barcode_string: str) -> dict:
        """Explain why a scan did not resolve for *this* company.

        Call this only after a company-scoped lookup has already missed. The
        scoped lookups (``_lookup_box`` / ``_lookup_pallet``) filter by company,
        so a box owned by another company, a wrong-state box, and a genuinely
        unknown barcode all collapse into the same bare "not found". This does a
        company-agnostic lookup to tell those apart and returns an actionable
        message. Read-only.

        Returns ``{"code": str, "message": str}`` where ``code`` is one of
        ``EMPTY`` / ``OTHER_COMPANY`` / ``ALREADY_DISPATCHED`` /
        ``INVALID_STATUS`` / ``UNKNOWN_BARCODE``.
        """
        raw = str(barcode_string or '').strip()
        if not raw:
            return {'code': 'EMPTY', 'message': 'Barcode is required.'}

        parsed = self._parse_barcode(raw)
        lookup_value = parsed.get('barcode') or raw

        box = Box.objects.select_related('company').filter(box_barcode=lookup_value).first()
        pallet = None
        if box is None:
            pallet = Pallet.objects.select_related('company').filter(pallet_id=lookup_value).first()
        entity = box or pallet

        if entity is None:
            return {
                'code': 'UNKNOWN_BARCODE',
                'message': (
                    f"Barcode '{raw}' does not exist in the system — "
                    f"check for a typo or a damaged/reprinted label."
                ),
            }

        label = 'box' if box is not None else 'pallet'
        if entity.company_id != self.company.id:
            return {
                'code': 'OTHER_COMPANY',
                'message': (
                    f"This {label} belongs to {entity.company.name}, not "
                    f"{self.company.name}. Switch your company to "
                    f"{entity.company.name} to scan it."
                ),
            }

        # Same company, yet the scoped lookup still missed it — surface the
        # state that excluded it so the operator isn't told it doesn't exist.
        status = getattr(entity, 'status', '') or ''
        if box is not None and (box.dispatched_at or status == BoxStatus.DISPATCHED):
            return {
                'code': 'ALREADY_DISPATCHED',
                'message': f"Box {box.box_barcode} has already been dispatched.",
            }
        if status:
            return {
                'code': 'INVALID_STATUS',
                'message': f"{label.capitalize()} {lookup_value} is {status} and cannot be scanned.",
            }
        return {
            'code': 'UNKNOWN_BARCODE',
            'message': f"'{raw}' could not be scanned for {self.company.name}.",
        }

    def log_scan_miss_if_unknown(self, barcode_raw: str, *, scan_type: str = ScanType.TRANSFER,
                                 context_ref_type: str = '', context_ref_id: int = None,
                                 user=None, device_info: str = '') -> 'ScanLog | None':
        """Record a ``NOT_FOUND`` :class:`ScanLog` when a barcode does not resolve
        for this company.

        Flows that resolve via :meth:`lookup_barcode` (e.g. the BST transfer
        scan) don't otherwise log, so their company-scoped misses are invisible
        — unlike :meth:`process_scan`, which always logs. Call this on the
        failure path only. No-op (returns ``None``) when the barcode actually
        resolves, so a *validation* failure on a found box is never mislabelled
        as NOT_FOUND. Must run outside the caller's rolled-back transaction to
        persist.
        """
        if self.lookup_barcode(barcode_raw).get('entity_id'):
            return None
        return ScanLog.objects.create(
            company=self.company,
            scan_type=scan_type,
            barcode_raw=str(barcode_raw or '').strip(),
            barcode_parsed=self._parse_barcode(barcode_raw),
            entity_type=EntityType.UNKNOWN,
            entity_id='',
            scan_result=ScanResult.NOT_FOUND,
            context_ref_type=context_ref_type,
            context_ref_id=context_ref_id,
            scanned_by=user,
            device_info=device_info,
        )

    def log_rejection(self, barcode_raw: str, *, reject_code: str, reject_message: str,
                      scan_type: str = ScanType.TRANSFER, context_ref_type: str = '',
                      context_ref_id: int = None, user=None, device_info: str = '',
                      scan_log_id: int = None) -> 'ScanLog | None':
        """Record that a scan was refused by a business rule.

        The barcode resolved — it is the rule that said no (bill not on the load,
        over-quantity, box already on another truck). Those refusals used to be
        returned as a 400 and then discarded, which left ``NOT_FOUND`` as the only
        measurable scan failure and hid the rejections that actually hold a truck
        at the dock.

        When ``scan_log_id`` is given the row :meth:`process_scan` already wrote for
        this physical scan is stamped in place, so one scan stays one row and the
        retry count is not double-reported. Otherwise (flows that resolve through
        :meth:`lookup_barcode` and never log, e.g. BST) a fresh row is created.

        Must run *outside* any transaction the caller rolls back on rejection, or
        the row goes away with it.
        """
        code = str(reject_code or '').strip()[:80]
        message = str(reject_message or '').strip()

        if scan_log_id:
            updated = ScanLog.objects.filter(id=scan_log_id).update(
                scan_result=ScanResult.REJECTED,
                reject_code=code,
                reject_message=message,
            )
            if updated:
                return ScanLog.objects.filter(id=scan_log_id).first()

        lookup = self.lookup_barcode(barcode_raw)
        return ScanLog.objects.create(
            company=self.company,
            scan_type=scan_type,
            barcode_raw=str(barcode_raw or '').strip()[:500],
            barcode_parsed=self._parse_barcode(barcode_raw),
            entity_type=lookup.get('entity_type') or EntityType.UNKNOWN,
            entity_id=str(lookup.get('entity_id') or ''),
            scan_result=ScanResult.REJECTED,
            reject_code=code,
            reject_message=message,
            context_ref_type=context_ref_type,
            context_ref_id=context_ref_id,
            scanned_by=user,
            device_info=device_info,
        )

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
        """Parse lightweight reference barcodes, with legacy JSON fallback."""
        raw_stripped = (raw or '').strip()
        if not raw_stripped:
            return {
                'entity_type': EntityType.UNKNOWN,
                'barcode': '',
                'error': 'Empty barcode value.',
            }

        # Legacy QR payloads used JSON with many item details. Keep this only as
        # a compatibility fallback; new labels encode plain BOX-/PLT- references.
        try:
            data = json.loads(raw_stripped)
            if isinstance(data, dict):
                barcode_type = data.get('type', '').upper()
                if barcode_type == 'BOX':
                    return {
                        'entity_type': EntityType.BOX,
                        'barcode': data.get('box_barcode') or data.get('barcode') or '',
                        'legacy_format': True,
                    }
                if barcode_type == 'PALLET':
                    return {
                        'entity_type': EntityType.PALLET,
                        'barcode': data.get('pallet_id') or data.get('barcode') or '',
                        'legacy_format': True,
                    }
                barcode = str(data.get('barcode') or '').strip()
                if barcode:
                    parsed = self._parse_barcode(barcode)
                    parsed['legacy_format'] = True
                    return parsed
                return {
                    'entity_type': EntityType.UNKNOWN,
                    'barcode': raw_stripped,
                    'legacy_format': True,
                    'error': 'Unsupported legacy barcode format.',
                }
        except (json.JSONDecodeError, TypeError):
            pass

        raw_upper = raw_stripped.upper()

        # Current lightweight barcode format: only the unique box/pallet ID.
        if raw_upper.startswith('BOX-'):
            return {'entity_type': EntityType.BOX, 'barcode': raw_stripped}
        if raw_upper.startswith('PLT-'):
            return {'entity_type': EntityType.PALLET, 'barcode': raw_stripped}

        # Explicit compact references are accepted for scanner integrations.
        if raw_upper.startswith('BOX_ID:'):
            return {
                'entity_type': EntityType.BOX,
                'barcode': raw_stripped.split(':', 1)[1].strip(),
            }
        if raw_upper.startswith('PALLET_ID:'):
            return {
                'entity_type': EntityType.PALLET,
                'barcode': raw_stripped.split(':', 1)[1].strip(),
            }

        # Older 1D printer values: B{box_barcode_no_special} / P{pallet_id_no_special}.
        if raw_upper.startswith('BBOX'):
            return {'entity_type': EntityType.BOX, 'barcode': raw_stripped}
        if raw_upper.startswith('PPLT'):
            return {'entity_type': EntityType.PALLET, 'barcode': raw_stripped}

        return {
            'entity_type': EntityType.UNKNOWN,
            'barcode': raw_stripped,
            'error': 'Invalid barcode format.',
        }

    def _lookup_box(self, barcode: str):
        stripped = barcode.strip()
        if stripped.isdigit():
            try:
                return Box.objects.select_related('pallet').get(
                    id=int(stripped), company=self.company
                )
            except Box.DoesNotExist:
                pass
        # Try exact match first
        try:
            return Box.objects.select_related('pallet').get(
                box_barcode=stripped, company=self.company
            )
        except Box.DoesNotExist:
            pass
        # Legacy 1D barcode fallback: 1D labels encode "B" + box_barcode with the
        # "-"/" " separators removed, so a box "BOX-..." prints as "BBOX...". Only a
        # genuine 1D value (starts with "BBOX") needs this reverse scan. A canonical
        # "BOX-..." value (or numeric id) that missed the exact lookups above simply
        # does not exist, so it must NOT fall through here — otherwise every unmatched
        # scan walks the entire box table (tens of thousands of rows) in Python and
        # takes ~20s, serialising the whole dock-scan queue behind it.
        if stripped.upper().startswith('BBOX'):
            clean = stripped[1:].replace('-', '').replace(' ', '')  # drop the "B" prefix
            boxes = Box.objects.select_related('pallet').filter(company=self.company)
            for box in boxes.iterator():
                if box.box_barcode.replace('-', '').replace(' ', '') == clean:
                    return box
        return None

    def _lookup_pallet(self, barcode: str):
        stripped = barcode.strip()
        if stripped.isdigit():
            try:
                return Pallet.objects.get(id=int(stripped), company=self.company)
            except Pallet.DoesNotExist:
                pass
        try:
            return Pallet.objects.get(
                pallet_id=stripped, company=self.company
            )
        except Pallet.DoesNotExist:
            pass
        # Legacy 1D barcode fallback: "P" + pallet_id with "-"/" " removed, so a
        # pallet "PLT-..." prints as "PPLT...". Guard to genuine 1D values only; a
        # canonical "PLT-..." that missed above does not exist and must not trigger a
        # full pallet-table scan (see _lookup_box for the same reasoning).
        if stripped.upper().startswith('PPLT'):
            clean = stripped[1:].replace('-', '').replace(' ', '')
            pallets = Pallet.objects.filter(company=self.company)
            for pallet in pallets.iterator():
                if pallet.pallet_id.replace('-', '').replace(' ', '') == clean:
                    return pallet
        return None
