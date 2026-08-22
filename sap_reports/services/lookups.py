"""
sap_reports/services/lookups.py

The picklists behind a report's filters.

SAP prompts the user with a bare text box and trusts them to know that
``BH-FG`` is a warehouse. Once the report moves into the app the same prompt has
to become a real field, so each parameter kind that can be chosen from a list
gets its options from the company's own master data.
"""

import logging
from typing import Dict, List

from sap_client.context import CompanyContext

from ..hana_reader import HanaSapReportReader
from ..parameters import ParameterKind

logger = logging.getLogger(__name__)


class SapReportLookupService:
    """Master-data options for one company, keyed by parameter kind."""

    def __init__(self, company):
        self.company = company
        self.context = CompanyContext(company.code)
        self.reader = HanaSapReportReader(self.context)

    def options_for(self, kind: str, search: str = "") -> List[Dict[str, str]]:
        """
        Options for a parameter of ``kind``, narrowed by ``search``.

        Kinds with no list (a free-text or numeric prompt) return nothing rather
        than an error: the frontend simply renders an input box for them.
        """
        readers = {
            ParameterKind.WAREHOUSE: self.reader.list_warehouses,
            ParameterKind.ITEM_GROUP: self.reader.list_item_groups,
            ParameterKind.PERIOD: self.reader.list_periods,
            ParameterKind.ITEM: self.reader.search_items,
            ParameterKind.BUSINESS_PARTNER: self.reader.search_business_partners,
        }

        read = readers.get(kind)
        if read is None:
            return []
        return read(search)
