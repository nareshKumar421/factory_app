"""The customer ledger on the A/R Invoices page.

Read straight from SAP's journal, so it covers every posting to the customer —
including the bills and receipts the counter enters in SAP directly, which this
app has no record of. Read-only.

Whose ledger a user may open is ``ledger_access``'s rule: every customer's
with the all-ledgers right, otherwise only the customers linked to them. The
rule is enforced here, not just in the picker — a linked user who edits the
request for another customer's code is refused before SAP is read.

The links themselves are kept on Admin › Customer Ledger Links
(``customer-links/``), under their own right.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from company.models import UserCompany
from sap_client.context import CompanyContext
from sap_client.hana.customer_ledger_reader import HanaCustomerLedgerReader

from . import permissions as ar_perms
from .ledger_access import (
    linked_customers,
    may_view_ledger,
    resolve_sap_customer,
    sees_all_ledgers,
)
from .models import UserCustomer
from .serializers import (
    CustomerLedgerQuerySerializer,
    UserCustomerCreateSerializer,
    UserCustomerSerializer,
)
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


class CustomerLinkBaseView(ARInvoiceBaseView):
    read_perms = [ar_perms.CanManageCustomerLedgerLinks]
    write_perms = [ar_perms.CanManageCustomerLedgerLinks]

    def links(self):
        return UserCustomer.objects.filter(company=self.request.company.company).select_related(
            "user", "created_by"
        )


class CustomerLinkListView(CustomerLinkBaseView):
    """GET/POST /api/v1/ar-invoices/customer-links/ — the active company's links."""

    def get(self, request):
        rows = self.links().order_by("user__full_name", "customer_name", "customer_code")
        return Response(UserCustomerSerializer(rows, many=True).data)

    def post(self, request):
        serializer = UserCustomerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = request.company.company

        user = get_user_model().objects.filter(pk=serializer.validated_data["user"]).first()
        if user is None:
            return Response({"detail": "No such user."}, status=status.HTTP_400_BAD_REQUEST)
        # A link in a company the user cannot enter would never be read.
        if not UserCompany.objects.filter(user=user, company=company, is_active=True).exists():
            return Response(
                {"detail": f"{user.full_name or user.email} has no access to {company.name}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        customer = resolve_sap_customer(company, serializer.validated_data["customer_code"])

        with transaction.atomic():
            # Re-linking a customer that was unlinked brings the old row back
            # rather than tripping the unique constraint.
            link, created = UserCustomer.objects.get_or_create(
                user=user,
                company=company,
                customer_code=customer["customer_code"],
                defaults={"customer_name": customer["customer_name"], "created_by": request.user},
            )
            if not created:
                link.is_active = True
                link.customer_name = customer["customer_name"]
                link.save(update_fields=["is_active", "customer_name", "updated_at"])
        return Response(
            UserCustomerSerializer(link).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CustomerLinkDetailView(CustomerLinkBaseView):
    """DELETE /api/v1/ar-invoices/customer-links/<id>/ — unlink.

    Switches the link off rather than deleting it, so the record of who could
    read the account survives.
    """

    def delete(self, request, pk):
        link = self.links().filter(pk=pk).first()
        if link is None:
            return Response({"detail": "Link not found."}, status=status.HTTP_404_NOT_FOUND)
        link.is_active = False
        link.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
