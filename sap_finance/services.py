"""Budget writes: SAP first, then the audit row.

A budget lives only in SAP's ``BUDGET`` UDO, so there is no local row to lock
and nothing here to roll back if SAP refuses. The audit row is written after
SAP accepted the change — a refusal leaves no trace here, and the operator
sees SAP's own words.

SAP Portal accepted whatever body the page posted and forwarded it to SAP
unvalidated; the serializer in front of this service checks it first.
"""

import logging

from sap_client.client import SAPClient

from .constants import BudgetAction
from .models import SapBudgetChange

logger = logging.getLogger(__name__)


def _record(company, user, action, doc_entry, payload):
    return SapBudgetChange.objects.create(
        company=company,
        action=action,
        doc_entry=doc_entry,
        budget_code=(payload or {}).get("U_BUDGET") or "",
        sub_budget_code=(payload or {}).get("U_SUB_BUDGET") or "",
        line_count=len((payload or {}).get("BUDGET1Collection") or []),
        payload=payload or {},
        created_by=user,
    )


def create_budget(company, user, payload: dict) -> dict:
    document = SAPClient(company_code=company.code).create_budget(payload)
    _record(company, user, BudgetAction.CREATE, document.get("DocEntry"), payload)
    return document


def update_budget(company, user, doc_entry: int, payload: dict) -> None:
    SAPClient(company_code=company.code).update_budget(doc_entry, payload)
    _record(company, user, BudgetAction.UPDATE, doc_entry, payload)


def delete_budget(company, user, doc_entry: int, current: dict | None) -> None:
    SAPClient(company_code=company.code).delete_budget(doc_entry)
    # Keep what was deleted, so the log can show the budget that disappeared.
    snapshot = {
        "U_BUDGET": (current or {}).get("U_BUDGET"),
        "U_SUB_BUDGET": (current or {}).get("U_SUB_BUDGET"),
        "BUDGET1Collection": (current or {}).get("BUDGET1Collection") or [],
    }
    _record(company, user, BudgetAction.DELETE, doc_entry, snapshot)
