"""Which routing row a voucher posts under.

A voucher's SAP company comes from its branch's routing row, or from a
per-voucher override (Common -> Mart, say). Either way the *row* used for the
header and the dims is then looked up by company: the voucher's own branch's
row when it routes to that company, else the first enabled row of the JI
company that does. That is how an Oil-branch voucher overridden to JIVO_MART
posts with Mart's vendor and branch.
"""

from typing import Optional

from sap_client.registry import COMPANY_SAP_REGISTRY

from .models import CashBranchSapRouting


def home_company_code(company) -> str:
    """The cash book's own SAP company: its code when the registry knows it, else ``""``."""
    code = getattr(company, "code", "") or ""
    return code if code in COMPANY_SAP_REGISTRY else ""


def _rows_for(company, rows=None):
    if rows is None:
        rows = CashBranchSapRouting.objects.filter(branch__company=company).select_related("branch")
    return list(rows)


def routing_for(company, sap_company_code: str, *, preferred_branch=None,
                rows=None) -> Optional[CashBranchSapRouting]:
    """The enabled routing row that posts ``sap_company_code`` for this JI company.

    ``rows`` lets the plan pass the routing rows it already loaded (one query
    per bunch). Ties go to ``branch.sort_order``, then ``branch.id``.
    """
    if not sap_company_code:
        return None
    candidates = [
        r for r in _rows_for(company, rows)
        if r.enabled and r.sap_company_code == sap_company_code
    ]
    if not candidates:
        return None
    if preferred_branch is not None:
        for r in candidates:
            if r.branch_id == getattr(preferred_branch, "id", preferred_branch):
                return r
    candidates.sort(key=lambda r: (r.branch.sort_order, r.branch_id))
    return candidates[0]


def header_tuple(r) -> tuple:
    """What every voucher of one batch must agree on (spec §2.3.5)."""
    return (
        r.mode,
        r.vendor_code,
        r.bpl_id,
        r.warehouse_code,
        bool(r.ap_drafts_enabled),
        bool(r.grpo_needs_attachment),
        r.grpo_grouping,
    )


HEADER_FIELDS = (
    "mode",
    "vendor_code",
    "bpl_id",
    "warehouse_code",
    "ap_drafts_enabled",
    "grpo_needs_attachment",
    "grpo_grouping",
)
