"""
universal_search/services/search.py

One number in, every company's answer out.

A ``DocNum`` is unique only inside one company database, so the same 1001 can
be a purchase order in Oil and nothing at all in Mart. Rather than make the
user guess which company to look in -- the complaint that started this -- the
search asks all of the companies they have access to and groups the answers
by company, the one they are currently working in first.

Each company is asked independently. A HANA that is down for one of them
returns that company with an ``error`` and no rows; the other two still
answer. A search that reported nothing because the third company timed out
would be worse than useless -- it would be wrong.
"""

from __future__ import annotations

import logging
from typing import Any

from company.models import Company, UserCompany
from sap_client.context import CompanyContext
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from .app_lookup import search_app_records
from .sap_lookup import UniversalSapReader, is_doc_num

logger = logging.getLogger(__name__)

#: Shortest term worth a round trip. One character matches half the item master.
MIN_TERM_LENGTH = 2

#: Longest term accepted. Nothing we search for is longer; anything that is,
#: is a paste accident.
MAX_TERM_LENGTH = 60


class SearchTermError(ValueError):
    """The term cannot be searched for, and the user needs to know why."""


def clean_term(raw: Any) -> str:
    """The term as it will be searched for, or an explanation of why it cannot be."""
    term = str(raw or "").strip()
    if len(term) < MIN_TERM_LENGTH:
        raise SearchTermError(
            f"Type at least {MIN_TERM_LENGTH} characters to search."
        )
    if len(term) > MAX_TERM_LENGTH:
        raise SearchTermError("That is too long to be a document or item number.")
    return term


def searchable_companies(user, current_code: str | None) -> list[Company]:
    """The companies this user may search, the one they are in first.

    Membership is the same ``UserCompany`` list that gates every other screen,
    so the search can never reach a company the user could not switch to.
    """
    companies = list(
        Company.objects.filter(
            id__in=UserCompany.objects.filter(user=user, is_active=True).values(
                "company_id"
            ),
            is_active=True,
        ).order_by("code")
    )
    companies.sort(key=lambda company: (company.code != current_code, company.code))
    return companies


def search(term: str, *, user, current_company_code: str | None) -> dict[str, Any]:
    """Run the whole search and shape it for the modal."""
    companies = searchable_companies(user, current_company_code)
    results = [_search_one_company(term, company, user) for company in companies]
    return {
        "term": term,
        "companies": results,
        "total": sum(company["total"] for company in results),
    }


def _search_one_company(term: str, company: Company, user) -> dict[str, Any]:
    documents: list[dict] = []
    items: list[dict] = []
    batches: list[dict] = []
    error = ""

    # The app's own tables first: they are local, they never fail the way a
    # HANA round trip can, and they are the half of the answer this app is
    # uniquely able to give.
    app_records = search_app_records(term, company=company, user=user)

    try:
        reader = UniversalSapReader(CompanyContext(company.code))
        # Three questions, one connection: opening one costs about half as much
        # as the query it carries.
        with reader.session():
            if is_doc_num(term):
                documents = reader.search_documents(int(term))
            items = reader.search_items(term)
            batches = reader.search_batches(term)
    except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
        logger.warning("Universal search: %s unavailable: %s", company.code, exc)
        error = "SAP did not answer for this company."
    except Exception:
        logger.exception("Universal search failed for %s", company.code)
        error = "SAP did not answer for this company."

    return {
        "company_code": company.code,
        "company_name": company.name,
        "documents": documents,
        "app_records": app_records,
        "items": items,
        "batches": batches,
        "total": len(documents) + len(app_records) + len(items) + len(batches),
        "error": error,
    }


def document_detail(company_code: str, kind: str, doc_entry: int) -> dict | None:
    """One SAP document with its lines, for the pane behind a search hit."""
    reader = UniversalSapReader(CompanyContext(company_code))
    return reader.document_detail(kind, doc_entry)


def item_detail(company_code: str, item_code: str) -> list[dict]:
    """Where one item's stock is standing."""
    reader = UniversalSapReader(CompanyContext(company_code))
    return reader.item_stock(item_code)
