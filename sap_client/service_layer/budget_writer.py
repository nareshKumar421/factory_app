"""SAP's ``BUDGET`` user-defined object: list, read, create, update, delete.

Ported from SAP Portal's ``/api/sap/budget*`` routes (``backend_v1/routes/sap.js``).
``BUDGET`` is a UDO this estate added to SAP — not SAP's own budget module
(``OBGT``) and not JI's ``budget_approvals`` dashboard, which only reads draft
lines against budget heads. A document carries a budget head (``U_BUDGET``,
cost dimension 3), an optional sub-budget (``U_SUB_BUDGET``, dimension 4) and
lines in ``BUDGET1Collection``.

An update sends ``B1S-ReplaceCollectionsOnPatch`` so lines removed on screen are
removed in SAP, exactly as the portal did.
"""

import logging

from .entity_client import ServiceLayerEntityClient

logger = logging.getLogger(__name__)

ENTITY = "BUDGET"
LIST_FIELDS = "DocEntry,DocNum,U_BUDGET,U_SUB_BUDGET,CreateDate"


class BudgetWriter:
    """CRUD on /b1s/v2/BUDGET."""

    def __init__(self, context):
        self.client = ServiceLayerEntityClient(context)

    def list(self, max_pages: int = 20) -> list[dict]:
        return self.client.get_all(
            ENTITY, select=LIST_FIELDS, orderby="DocEntry desc", top=100, max_pages=max_pages
        )

    def get(self, doc_entry: int) -> dict | None:
        return self.client.get(f"{ENTITY}({int(doc_entry)})", not_found_ok=True)

    def create(self, payload: dict) -> dict:
        data = self.client.post(ENTITY, payload, label="create the budget")
        logger.info("Budget %s created in SAP", data.get("DocEntry"))
        return data

    def update(self, doc_entry: int, payload: dict) -> None:
        self.client.patch(
            f"{ENTITY}({int(doc_entry)})",
            payload,
            headers={"B1S-ReplaceCollectionsOnPatch": "true"},
            label=f"update budget {int(doc_entry)}",
        )
        logger.info("Budget %s updated in SAP", doc_entry)

    def delete(self, doc_entry: int) -> None:
        self.client.delete(f"{ENTITY}({int(doc_entry)})", label=f"delete budget {int(doc_entry)}")
        logger.info("Budget %s deleted in SAP", doc_entry)
