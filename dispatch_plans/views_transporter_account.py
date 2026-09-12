"""
dispatch_plans/views_transporter_account.py

The transporter account: what one company owes its hauliers, and what it paid.

One endpoint, one company per call, keyed off the `Company-Code` header like
every other SAP read in this app -- SAP B1 gives each company its own HANA
schema, so a combined Oil + Mart answer is two calls added up by the caller and
there is no query that can span them.

Read-only. Nothing here writes to SAP or to Postgres, and the reader behind it
runs three aggregates rather than fetching documents, so a wall board polling it
costs the SAP box three GROUP BYs and not a page of invoice rows.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewOpenBiltiesOrPostTransporterAPInvoice
from .transporter_account_reader import (
    DEFAULT_PAYMENT_DAYS,
    HanaTransporterAccountReader,
)

logger = logging.getLogger(__name__)

# A payments window longer than this is not a dashboard question any more, and
# the wider the window the more of the SAP payment table the aggregate walks.
MAX_PAYMENT_DAYS = 365


class TransporterAccountAPI(APIView):
    """
    GET /api/v1/dispatch/transporter-account/?payment_days=30

    Gated on the same grant as the open-bilty queue: whoever is allowed to see
    which freight bills are outstanding is allowed to see what they come to.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewOpenBiltiesOrPostTransporterAPInvoice,
    ]

    def get(self, request):
        company_code = request.company.company.code

        try:
            payment_days = int(request.GET.get("payment_days") or DEFAULT_PAYMENT_DAYS)
        except (TypeError, ValueError):
            payment_days = DEFAULT_PAYMENT_DAYS
        payment_days = max(1, min(payment_days, MAX_PAYMENT_DAYS))

        reader = HanaTransporterAccountReader(CompanyContext(company_code))

        try:
            account = reader.get_account(payment_days=payment_days)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"company_code": company_code, **account})
