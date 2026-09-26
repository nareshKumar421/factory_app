"""Master-data pickers, ported from SAP Portal's ``/api/sap/lookup/*``.

Read-only lists of SAP master data for form pickers: items, tax and SAC codes,
cost dimensions, accounts, partners, groups, terms, states, banks. Like
``/api/v1/po/vendors/`` and ``/po/warehouses/`` they need a login and the
company context but no module right — they reveal only names and codes, and
several modules' forms (partner registration, BOM changes, production orders)
use the same pickers.

Every lookup reads the company in the ``Company-Code`` header. The portal read
the default company for its partner lookups whatever partner was on screen.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .client import SAPClient
from .exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)


def _search(params) -> str:
    return (params.get("search") or params.get("q") or "").strip()


def _card_type(params) -> str | None:
    value = (params.get("type") or "").strip().upper()
    return value or None


def _limit(params, default: int) -> int:
    try:
        return int(params.get("limit", default))
    except (TypeError, ValueError):
        return default


def _batches(client: SAPClient, params) -> list[dict]:
    item_code = (params.get("item_code") or "").strip()
    warehouse = (params.get("warehouse") or "").strip()
    if not item_code or not warehouse:
        raise SAPValidationError("item_code and warehouse are required.")
    return client.available_batches(item_code, warehouse)


def _warehouses(client: SAPClient, params) -> list[dict]:
    return [{"code": w.warehouse_code, "name": w.warehouse_name} for w in client.get_active_warehouses()]


def _user_table(key):
    def fetch(client: SAPClient, params) -> list[dict]:
        rows, warning = client.lookup_user_table(key)
        if warning:
            logger.info("Lookup %s: %s", key, warning)
        return rows

    return fetch


def _next_card_code(client: SAPClient, params) -> dict:
    card_type = _card_type(params) or ""
    default_prefix = {"C": "CUSTA", "S": "VENDA"}.get(card_type, "")
    prefix = (params.get("prefix") or default_prefix).strip()
    return {"card_code": client.next_card_code(prefix, card_type)}


# name in the URL → how to answer it. The URL name is ``sap-lookup-<name>``.
LOOKUPS = {
    "items": lambda c, p: c.lookup_items(_search(p), limit=_limit(p, 20)),
    "sac-codes": lambda c, p: c.lookup_sac_codes(_search(p), limit=_limit(p, 50)),
    "locations": lambda c, p: c.lookup_locations(_search(p), limit=_limit(p, 40)),
    "warehouses": _warehouses,
    "tax-codes": lambda c, p: c.lookup_tax_codes(),
    "costing-codes": lambda c, p: c.lookup_costing_codes(
        p.get("dimension") or p.get("dim") or 1, _search(p), limit=_limit(p, 100)
    ),
    "branches": lambda c, p: c.lookup_branches(),
    "resources": lambda c, p: c.lookup_resources(_search(p), limit=_limit(p, 50)),
    "batches": _batches,
    "gl-accounts": lambda c, p: c.lookup_gl_accounts(_search(p), limit=_limit(p, 30)),
    "ar-accounts": lambda c, p: c.lookup_ar_accounts(),
    "ap-accounts": lambda c, p: c.lookup_ap_accounts(),
    "business-partners": lambda c, p: c.lookup_business_partners(
        _search(p), card_type=_card_type(p), limit=_limit(p, 30)
    ),
    "bp-groups": lambda c, p: c.lookup_bp_groups(_card_type(p) or ""),
    "sales-employees": lambda c, p: c.lookup_sales_employees(),
    "payment-terms": lambda c, p: c.lookup_payment_terms(),
    "states": lambda c, p: c.lookup_states(p.get("country") or "IN"),
    "banks": lambda c, p: c.lookup_banks(p.get("country") or "IN"),
    "main-group": _user_table("main-group"),
    "chain": _user_table("chain"),
    "next-card-code": _next_card_code,
}


class SapLookupView(APIView):
    """GET one master-data list; which one is fixed per URL (``lookup`` kwarg)."""

    permission_classes = [IsAuthenticated, HasCompanyContext]
    lookup = ""

    def get(self, request):
        fetch = LOOKUPS[self.lookup]
        try:
            client = SAPClient(company_code=request.company.company.code)
            data = fetch(client, request.query_params)
        except SAPValidationError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError as e:
            logger.error("SAP connection error in lookup %s: %s", self.lookup, e)
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            logger.error("SAP data error in lookup %s: %s", self.lookup, e)
            return Response(
                {"detail": "Failed to retrieve this list from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(data)
