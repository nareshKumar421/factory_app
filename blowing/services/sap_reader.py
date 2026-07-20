"""
Read-only SAP item pickers for the blowing module (v1 has no postings).

Reuses production_execution's ProductionOrderReader, which is already keyed by
company_code and hits the correct company DB (JIVO_OIL / JIVO_BEVERAGES).
"""
import logging

from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError

logger = logging.getLogger(__name__)


class BlowingItemReader:
    def __init__(self, company_code: str):
        self.company_code = company_code
        self._reader = ProductionOrderReader(company_code)

    def search_preform_items(self, search: str = '', limit: int = 50) -> list:
        """Preform is a raw material — search all items (produced_only=False)."""
        return self._reader.search_items(search=search, limit=limit, produced_only=False)

    def search_bottle_items(self, search: str = '', limit: int = 50) -> list:
        """Blown bottles are manufactured — restrict to items with a BOM."""
        return self._reader.search_items(search=search, limit=limit, produced_only=True)


__all__ = ['BlowingItemReader', 'SAPReadError']
