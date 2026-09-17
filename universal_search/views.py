"""
universal_search/views.py

Three endpoints behind the search modal.

``GET search/`` takes the number and answers for every company the user may
see. ``GET document/`` opens one SAP hit into its lines. ``GET item-stock/``
does the same for an item. The split is deliberate: the search is one HANA
round trip per company and has to be quick, so the expensive part is only
paid for the one result the user actually opens.

Every endpoint needs ``can_use_universal_search``. The ``Company-Code`` header
is still required -- not to scope the search, which deliberately spans
companies, but because it says which company to put first, and because it is
what proves the caller belongs to a company at all.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from .documents import DOC_TYPES_BY_KIND
from .permissions import CanUseUniversalSearch
from .services import search as search_service

logger = logging.getLogger(__name__)

SAP_UNAVAILABLE = "SAP is not answering right now. Try again in a moment."


class UniversalSearchBaseAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanUseUniversalSearch]

    @property
    def company_code(self) -> str:
        return self.request.company.company.code

    def handle_exception(self, exc):
        """SAP being unreachable is an ordinary event here, not a 500."""
        if isinstance(exc, SAPConnectionError):
            return Response(
                {"detail": SAP_UNAVAILABLE}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        if isinstance(exc, SAPDataError):
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if isinstance(exc, (SAPValidationError, search_service.SearchTermError)):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


class UniversalSearchAPI(UniversalSearchBaseAPI):
    """Look one number up everywhere at once."""

    def get(self, request):
        try:
            term = search_service.clean_term(request.query_params.get("q"))
        except search_service.SearchTermError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            search_service.search(
                term, user=request.user, current_company_code=self.company_code
            )
        )


class UniversalSearchDocumentAPI(UniversalSearchBaseAPI):
    """One SAP document's header and lines."""

    def get(self, request):
        company_code = request.query_params.get("company") or self.company_code
        kind = request.query_params.get("kind") or ""
        doc_entry = request.query_params.get("doc_entry") or ""

        if kind not in DOC_TYPES_BY_KIND:
            return Response(
                {"detail": "Unknown document type."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not str(doc_entry).isdigit():
            return Response(
                {"detail": "A document key is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._may_read(request.user, company_code):
            return Response(
                {"detail": "You do not have access to that company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        document = search_service.document_detail(company_code, kind, int(doc_entry))
        if document is None:
            return Response(
                {"detail": "That document is no longer in SAP."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"company_code": company_code, "document": document})

    def _may_read(self, user, company_code: str) -> bool:
        # The search itself spans companies, so the detail must check the one
        # it was handed rather than trust the header it came in under.
        return any(
            company.code == company_code
            for company in search_service.searchable_companies(user, None)
        )


class UniversalSearchItemStockAPI(UniversalSearchBaseAPI):
    """Where one item's stock is standing, warehouse by warehouse."""

    def get(self, request):
        company_code = request.query_params.get("company") or self.company_code
        item_code = (request.query_params.get("item_code") or "").strip()

        if not item_code:
            return Response(
                {"detail": "An item code is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not any(
            company.code == company_code
            for company in search_service.searchable_companies(request.user, None)
        ):
            return Response(
                {"detail": "You do not have access to that company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return Response(
            {
                "company_code": company_code,
                "item_code": item_code,
                "warehouses": search_service.item_detail(company_code, item_code),
            }
        )
