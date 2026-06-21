"""Cross-company scope helpers for the unified factory flow.

The gate -> dock -> dispatch-out screens span all of a user's companies (their
``UserCompany`` set) rather than the single ``Company-Code`` header. These helpers
resolve that set, detect the opt-in ``?all_companies=1`` flag, and enforce that a
user may only act on a record belonging to one of their companies -- the security
boundary for the aggregated flow (membership of the *record's* company, not the
active header).
"""

from rest_framework.exceptions import PermissionDenied

from company.models import UserCompany


def user_company_ids(request):
    """Active company ids the requesting user belongs to."""
    return list(
        UserCompany.objects.filter(user=request.user, is_active=True).values_list(
            "company_id", flat=True
        )
    )


def wants_all_companies(request):
    """True when the caller opted into the cross-company aggregated view."""
    return request.query_params.get("all_companies") in ("1", "true", "True", "yes")


def assert_company_in_scope(request, company_id, ids=None):
    """Raise PermissionDenied unless ``company_id`` is one of the user's companies.

    Pass ``ids`` to reuse an already-resolved company id list (avoids a re-query).
    """
    if ids is None:
        ids = user_company_ids(request)
    if company_id not in ids:
        raise PermissionDenied("This record belongs to a company you cannot access.")
