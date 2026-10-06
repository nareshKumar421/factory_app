"""
amounts_board/views.py

``GET /api/v1/dashboards/amounts-board/board/``
    The whole board in one read. Always 200 with ``meta.degraded`` naming any
    section SAP could not answer, as on the other boards; 503 only if the
    board itself cannot be composed.

``GET /api/v1/dashboards/amounts-board/godown-items/?company=&category=&warehouse=``
    The stock inside one godown, for one category: the drill's last level.

``GET /api/v1/dashboards/amounts-board/debtors/?debtor=JWPL|MART|BEVERAGES|TOTAL``
    The customers behind one debtor tile, largest balance first; TOTAL is all
    three companies, naming in ``missing`` any it could not read.

``GET /api/v1/dashboards/amounts-board/debtor-bills/?company=&customer=``
    One customer's unpaid bills, oldest first: the debtor drill's last level.

``GET / PUT /api/v1/dashboards/amounts-board/owners/``
    The RM / PM / FG owner of each plant, and who may be picked. PUT takes
    ``{"company", "category", "user"}``; ``"user": null`` clears the owner.

The board is composed across the three company schemas server-side, so the
``Company-Code`` header only proves the reader is staff of some company; it
does not narrow what the board shows.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .constants import CATEGORIES, DEBTOR_COMPANIES, PLANT_COMPANIES, PLANT_LABELS
from .models import StockOwner
from .permissions import CanManageStockOwners, CanViewAmountsBoard
from .services import ALL_DEBTORS, AmountsBoardService, owner_payload, owners_by_company

logger = logging.getLogger(__name__)

CATEGORY_KEYS = [c.value for c in CATEGORIES]
DEBTOR_KEYS = [key for key, _label, _code in DEBTOR_COMPANIES] + [ALL_DEBTORS]
DEBTOR_CODES = [code for _key, _label, code in DEBTOR_COMPANIES]


def _sap_failure(exc):
    """503 when SAP did not answer, 502 when it refused the read."""
    code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if isinstance(exc, SAPConnectionError)
        else status.HTTP_502_BAD_GATEWAY
    )
    return Response({"detail": str(exc)}, status=code)


class AmountsBoardAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewAmountsBoard]

    def get(self, request):
        try:
            board = AmountsBoardService(user=request.user).build()
        except Exception as exc:  # noqa: BLE001 - every section already catches its own
            logger.exception("amounts_board: board could not be composed")
            return Response(
                {"detail": "The amounts board could not be read.", "error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(board, status=status.HTTP_200_OK)


class AmountsGodownItemsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewAmountsBoard]

    def get(self, request):
        company = (request.query_params.get("company") or "").strip().upper()
        category = (request.query_params.get("category") or "").strip().upper()
        warehouse = (request.query_params.get("warehouse") or "").strip().upper()

        if company not in PLANT_COMPANIES:
            return Response(
                {"detail": f"`company` must be one of {', '.join(PLANT_COMPANIES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if category not in CATEGORY_KEYS:
            return Response(
                {"detail": f"`category` must be one of {', '.join(CATEGORY_KEYS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not warehouse:
            return Response(
                {"detail": "`warehouse` is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            payload = AmountsBoardService(user=request.user).godown_items(company, category, warehouse)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_failure(exc)
        return Response(payload, status=status.HTTP_200_OK)


class AmountsDebtorsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewAmountsBoard]

    def get(self, request):
        key = (request.query_params.get("debtor") or "").strip().upper()
        if key not in DEBTOR_KEYS:
            return Response(
                {"detail": f"`debtor` must be one of {', '.join(DEBTOR_KEYS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payload = AmountsBoardService(user=request.user).debtor_drill(key)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_failure(exc)
        return Response(payload, status=status.HTTP_200_OK)


class AmountsDebtorBillsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewAmountsBoard]

    def get(self, request):
        company = (request.query_params.get("company") or "").strip().upper()
        customer = (request.query_params.get("customer") or "").strip().upper()
        if company not in DEBTOR_CODES:
            return Response(
                {"detail": f"`company` must be one of {', '.join(DEBTOR_CODES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not customer:
            return Response(
                {"detail": "`customer` is required."}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            payload = AmountsBoardService(user=request.user).debtor_bills(company, customer)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_failure(exc)
        if payload is None:
            return Response(
                {"detail": f"{customer} is not a customer of {company}."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload, status=status.HTTP_200_OK)


def _candidates(company_code: str):
    """Active staff of the company: whoever may be named its owner."""
    User = get_user_model()
    return (
        User.objects.filter(
            is_active=True,
            usercompany__company__code=company_code,
            usercompany__is_active=True,
        )
        .distinct()
        .order_by("full_name", "email")
    )


class StockOwnersAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageStockOwners]

    def get(self, request):
        owners = owners_by_company(PLANT_COMPANIES)
        plants = []
        for code in PLANT_COMPANIES:
            plants.append(
                {
                    "company_code": code,
                    "label": PLANT_LABELS[code],
                    "owners": {
                        key: owner_payload(owners.get(code, {}).get(key)) for key in CATEGORY_KEYS
                    },
                    "candidates": [
                        {"id": u.id, "name": u.full_name, "email": u.email}
                        for u in _candidates(code)
                    ],
                }
            )
        return Response({"plants": plants}, status=status.HTTP_200_OK)

    def put(self, request):
        company_code = str(request.data.get("company") or "").strip().upper()
        category = str(request.data.get("category") or "").strip().upper()
        user_id = request.data.get("user")

        if company_code not in PLANT_COMPANIES:
            return Response(
                {"detail": f"`company` must be one of {', '.join(PLANT_COMPANIES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if category not in CATEGORY_KEYS:
            return Response(
                {"detail": f"`category` must be one of {', '.join(CATEGORY_KEYS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        company = Company.objects.filter(code=company_code).first()
        if company is None:
            return Response(
                {"detail": f"{company_code} is not set up on this server."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            if user_id in (None, ""):
                StockOwner.objects.filter(company=company, category=category).delete()
                return Response({"owner": None}, status=status.HTTP_200_OK)

            user = _candidates(company_code).filter(pk=user_id).first()
            if user is None:
                return Response(
                    {"detail": f"That user is not active staff of {PLANT_LABELS[company_code]}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            owner, _ = StockOwner.objects.update_or_create(
                company=company,
                category=category,
                defaults={"user": user, "updated_by": request.user},
            )
        return Response({"owner": owner_payload(owner)}, status=status.HTTP_200_OK)
