import logging
from django.utils import timezone

from .barcode_service import BarcodeService

logger = logging.getLogger(__name__)


class ProductionBarcodeIntegration:
    """Bridge between production runs and barcode labeling."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self._company = None

    @property
    def company(self):
        if self._company is None:
            from company.models import Company
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    def generate_labels_for_run(self, run_id: int, qty_per_box, box_count: int,
                                batch_number: str, warehouse: str, user):
        """
        Generate box labels for a production run.
        Pulls item info from the run, creates boxes via BarcodeService.
        """
        from production_execution.models import ProductionRun

        try:
            run = ProductionRun.objects.get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

        svc = BarcodeService(company_code=self.company_code)

        # Get line name
        line_name = run.line.name if run.line else ''

        # Determine dates
        mfg_date = run.date or timezone.now().date()
        # Default expiry 2 years from mfg (can be overridden)
        from datetime import timedelta
        exp_date = mfg_date + timedelta(days=730)

        boxes = svc.generate_boxes({
            'item_code': run.product or '',
            'item_name': run.product or '',
            'batch_number': batch_number,
            'qty': qty_per_box,
            'box_count': box_count,
            'uom': 'PCS',
            'mfg_date': mfg_date,
            'exp_date': exp_date,
            'warehouse': warehouse,
            'production_line': line_name,
            'production_run_id': run.id,
        }, user)

        logger.info(
            f"Generated {len(boxes)} labels for run #{run.run_number} by {user}"
        )
        return boxes

    def create_pallet_for_run(self, run_id: int, box_ids: list[int],
                              warehouse: str, user):
        """Create a pallet from boxes linked to a production run."""
        from production_execution.models import ProductionRun

        try:
            run = ProductionRun.objects.get(id=run_id, company=self.company)
        except ProductionRun.DoesNotExist:
            raise ValueError(f"Production run {run_id} not found.")

        svc = BarcodeService(company_code=self.company_code)
        line_name = run.line.name if run.line else ''

        pallet = svc.create_pallet({
            'box_ids': box_ids,
            'warehouse': warehouse,
            'production_line': line_name,
            'production_run_id': run.id,
        }, user)

        logger.info(
            f"Created pallet {pallet.pallet_id} for run #{run.run_number} by {user}"
        )
        return pallet
