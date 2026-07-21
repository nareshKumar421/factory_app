"""Shared helpers for resolving the SAP item code used for BOM lookups."""
import logging

logger = logging.getLogger(__name__)


def get_run_item_code(run, reader=None):
    """Return the SAP item code for a production run for BOM lookups.

    Runs are not created from SAP production orders, so the BOM is always
    fetched by item code (OITT/ITT1). The run stores ``product`` as the item
    *name*; ``item_code`` is the actual OITM code. When ``item_code`` is not
    yet set, resolve it from the product name once and persist it so later
    lookups don't need to hit SAP again.

    Returns the item code string, or None when it cannot be resolved.
    """
    if run.item_code:
        return run.item_code
    if not run.product:
        return None

    from .sap_reader import ProductionOrderReader
    reader = reader or ProductionOrderReader(run.company.code)
    code = reader.resolve_item_code_by_name(run.product)
    if code:
        run.item_code = code
        run.save(update_fields=['item_code', 'updated_at'])
        logger.info(f"Resolved item code {code} for run {run.id} from name '{run.product}'")
    return code
