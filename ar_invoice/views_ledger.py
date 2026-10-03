"""The customer ledger on the A/R Invoices page.

Read straight from SAP's journal, so it covers every posting to the customer —
including the bills and receipts the counter enters in SAP directly, which this
app has no record of. Read-only.

Whose ledger a user may open is ``ledger_access``'s rule: every customer's
with the all-ledgers right, otherwise only the customers linked to them. The
rule is enforced here, not just in the picker — a linked user who edits the
request for another customer's code is refused before SAP is read.
"""
from rest_framework import status
from rest_framework.response import Response

from sap_client.context import CompanyContext
from sap_client.hana.customer_ledger_reader import HanaCustomerLedgerReader

from .ledger_access import linked_customers, may_view_ledger, sees_all_ledgers
from .serializers import CustomerLedgerQuerySerializer
from .views import ARInvoiceBaseView


class CustomerLedgerAccessView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/customer-ledger/customers/

    Which customers the Ledger tab offers this user: ``all_customers`` true
    means any (search the customer master), otherwise exactly ``customers`` —
    possibly none.
    """

    def get(self, request):
        company = request.company.company
        all_customers = sees_all_ledgers(request.user)
        return Response(
            {
                "all_customers": all_customers,
                "customers": [] if all_customers else linked_customers(request.user, company),
            }
        )


class CustomerLedgerView(ARInvoiceBaseView):
    """GET /api/v1/ar-invoices/customer-ledger/?customer_code=CUSTA000123
    &date_from=2026-04-01&date_to=2026-10-03"""

    def get(self, request):
        query = CustomerLedgerQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        company = request.company.company
        if not may_view_ledger(request.user, company, params["customer_code"]):
            return Response(
                {"detail": "This customer's ledger is not linked to your login."},
                status=status.HTTP_403_FORBIDDEN,
            )
        reader = HanaCustomerLedgerReader(CompanyContext(company.code))
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
