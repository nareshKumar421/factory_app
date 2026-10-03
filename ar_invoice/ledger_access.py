"""Whose ledger a user may open on the Ledger tab.

Two rules, kept here so the picker and the ledger itself cannot drift apart:

* ``ar_invoice.view_all_customer_ledgers`` (accounts, billing — and any
  superuser) opens every customer's ledger.
* Everyone else sees only the customers linked to them in this company
  (``UserCustomer``, active links only). **No link means no customer**, not
  every customer: a user nobody linked yet must not fall through to the
  whole book.

Links are made on Admin › Customer Ledger Links, which names a customer only
once SAP confirms it (``resolve_sap_customer``).
"""
from sap_client.context import CompanyContext
from sap_client.hana.customer_reader import HanaCustomerReader

from .models import UserCustomer

VIEW_ALL_LEDGERS = "ar_invoice.view_all_customer_ledgers"


def sees_all_ledgers(user) -> bool:
    return user.has_perm(VIEW_ALL_LEDGERS)


def linked_customers(user, company) -> list[dict]:
    """The customers this user is, in this company, as the picker lists them."""
    links = UserCustomer.objects.filter(user=user, company=company, is_active=True)
    return [
        {
            "customer_code": link.customer_code,
            "customer_name": link.customer_name or link.customer_code,
        }
        for link in links.order_by("customer_name", "customer_code")
    ]


def may_view_ledger(user, company, customer_code: str) -> bool:
    if sees_all_ledgers(user):
        return True
    return UserCustomer.objects.filter(
        user=user,
        company=company,
        customer_code=(customer_code or "").strip(),
        is_active=True,
    ).exists()


def resolve_sap_customer(company, customer_code: str) -> dict:
    """The customer as that company's SAP holds it — code as SAP spells it,
    and its name — or ``ValueError`` when SAP has no such customer.

    A mistyped code would link the user to nobody (or to somebody else), and
    their ledger would then say "not linked" with no hint why. SAP matches
    codes case-sensitively and ours are upper case, so a code typed in lower
    case is tried again in upper case. SAP being unreachable raises its own
    error: a link is never made blind.
    """
    code = (customer_code or "").strip()
    if not code:
        raise ValueError("Name the customer's SAP code.")
    reader = HanaCustomerReader(CompanyContext(company.code))
    customer = reader.get_customer(code)
    if customer is None and code != code.upper():
        customer = reader.get_customer(code.upper())
    if customer is None:
        raise ValueError(f"{code} is not a customer in {company.name}'s SAP.")
    return customer
