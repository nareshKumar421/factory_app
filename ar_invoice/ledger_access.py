"""Whose ledger a user may open on the Ledger tab.

Two rules, kept here so the picker and the ledger itself cannot drift apart:

* ``ar_invoice.view_all_customer_ledgers`` (accounts, billing — and any
  superuser) opens every customer's ledger.
* Everyone else sees only the customers linked to them in this company
  (``UserCustomer``, active links only). **No link means no customer**, not
  every customer: a user nobody linked yet must not fall through to the
  whole book.
"""
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
