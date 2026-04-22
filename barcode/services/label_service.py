import json
import logging

from ..models import (
    Box, Pallet, LabelPrintLog,
    LabelType, PrintType, BoxStatus, PalletStatus,
)

logger = logging.getLogger(__name__)


class LabelService:

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
    # Generate label data (returned to frontend for rendering)
    # ==================================================================

    def get_box_label_data(self, box_id: int) -> dict:
        try:
            box = Box.objects.get(id=box_id, company=self.company)
        except Box.DoesNotExist:
            raise ValueError(f"Box {box_id} not found.")

        qr_payload = box.barcode_data or self._build_box_qr(box)

        return {
            'type': 'BOX',
            'id': box.id,
            'barcode': box.box_barcode,
            'qr_payload': json.dumps(qr_payload),
            'item_code': box.item_code,
            'item_name': box.item_name,
            'batch_number': box.batch_number,
            'qty': str(box.qty),
            'uom': box.uom,
            'mfg_date': str(box.mfg_date),
            'exp_date': str(box.exp_date),
            'production_line': box.production_line,
            'warehouse': box.current_warehouse,
            'g_weight': str(box.g_weight) if box.g_weight is not None else '',
            'n_weight': str(box.n_weight) if box.n_weight is not None else '',
        }

    def get_pallet_label_data(self, pallet_id: int) -> dict:
        try:
            pallet = Pallet.objects.get(id=pallet_id, company=self.company)
        except Pallet.DoesNotExist:
            raise ValueError(f"Pallet {pallet_id} not found.")

        qr_payload = pallet.barcode_data or self._build_pallet_qr(pallet)

        return {
            'type': 'PALLET',
            'id': pallet.id,
            'barcode': pallet.pallet_id,
            'qr_payload': json.dumps(qr_payload),
            'item_code': pallet.item_code,
            'item_name': pallet.item_name,
            'batch_number': pallet.batch_number,
            'box_count': pallet.box_count,
            'total_qty': str(pallet.total_qty),
            'uom': pallet.uom,
            'mfg_date': str(pallet.mfg_date),
            'exp_date': str(pallet.exp_date),
            'production_line': pallet.production_line,
            'warehouse': pallet.current_warehouse,
        }

    def get_bulk_label_data(self, items: list[dict]) -> list[dict]:
        results = []
        for item in items:
            label_type = item.get('label_type', '')
            ref_id = item.get('id')
            try:
                if label_type == 'BOX':
                    results.append(self.get_box_label_data(ref_id))
                elif label_type == 'PALLET':
                    results.append(self.get_pallet_label_data(ref_id))
                else:
                    results.append({'error': f"Unknown label type: {label_type}", 'id': ref_id})
            except ValueError as e:
                results.append({'error': str(e), 'id': ref_id})
        return results

    # ==================================================================
    # Log prints
    # ==================================================================

    def log_print(self, label_type: str, reference_id: int, reference_code: str,
                  print_type: str, user, reprint_reason: str = '',
                  printer_name: str = '') -> LabelPrintLog:

        log_entry = LabelPrintLog.objects.create(
            company=self.company,
            label_type=label_type,
            reference_id=str(reference_id),
            reference_code=reference_code,
            print_type=print_type,
            reprint_reason=reprint_reason,
            printed_by=user,
            printer_name=printer_name,
        )
        logger.info(
            f"Label {print_type} logged: {label_type} {reference_code} by {user}"
        )
        return log_entry

    def get_print_history(self, **filters):
        qs = LabelPrintLog.objects.filter(
            company=self.company
        ).select_related('printed_by')

        if filters.get('label_type'):
            qs = qs.filter(label_type=filters['label_type'])
        if filters.get('print_type'):
            qs = qs.filter(print_type=filters['print_type'])
        if filters.get('reference_code'):
            qs = qs.filter(reference_code__icontains=filters['reference_code'])
        return qs

    # ==================================================================
    # QR Payload builders
    # ==================================================================

    def _build_box_qr(self, box: Box) -> dict:
        return {
            'type': 'BOX',
            'box_barcode': box.box_barcode,
            'item_code': box.item_code,
            'batch': box.batch_number,
            'qty': str(box.qty),
            'uom': box.uom,
            'mfg_date': str(box.mfg_date),
            'exp_date': str(box.exp_date),
        }

    def _build_pallet_qr(self, pallet: Pallet) -> dict:
        return {
            'type': 'PALLET',
            'pallet_id': pallet.pallet_id,
            'item_code': pallet.item_code,
            'batch': pallet.batch_number,
            'box_count': pallet.box_count,
            'total_qty': str(pallet.total_qty),
            'uom': pallet.uom,
            'mfg_date': str(pallet.mfg_date),
            'exp_date': str(pallet.exp_date),
        }
