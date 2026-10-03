"""The customer ledger on the A/R Invoices page.

Read straight from SAP's journal, so it covers every posting to the customer —
including the bills and receipts the counter enters in SAP directly, which this
app has no record of. Read-only, and open to anyone who can view A/R invoices:
it is the same customer the page already shows a credit position for.
"""
from rest_framework import status
from rest_framework.response import Response

from sap_client.context import CompanyContext
from sap_client.hana.customer_ledger_reader import HanaCustomerLedgerReader

from .serializers import CustomerLedgerQuerySerializer
from .views import ARInvoiceBaseView


class CustomerLedgerView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/customer-ledger/?customer_code=CUSTA000123
    &date_from=2026-04-01&date_to=2026-10-03"""

    def get(self, request):
        query = CustomerLedgerQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        reader = HanaCustomerLedgerReader(CompanyContext(request.company.company.code))
        ledger = reader.ledger(
            params["customer_code"],
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
        )
        if ledger is None:
            return Response(
                {"detail": "No such customer in SAP for this company."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(ledger)
