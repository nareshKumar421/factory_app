"""Who is allowed to move stock out of, or into, which warehouse.

Every caller goes through here rather than querying `UserWarehouse` directly,
because two rules are easy to forget and both fail dangerously:

**No assignment means no access.** An unassigned user cannot raise or accept
anything. That is the stricter of the two possible readings and it was chosen
deliberately, so the restriction cannot be bypassed by simply never configuring
somebody. The cost is that this has to be configured before it ships — see the
`report_warehouse_scope_gaps` command, which lists exactly who would be locked
out.

**Superusers are exempt.** Without this the first deploy locks out the very
people who would configure it, including from the page that does the
configuring. It is a deadlock guard, not a convenience.

Errors are DRF `PermissionDenied` (403), matching `goods_return.services`, and
they always name the warehouses the user *does* manage — "you can't do that" with
no second half is what makes a permission error feel like a bug.
"""

from typing import Iterable, Optional

from rest_framework.exceptions import PermissionDenied

from ..models_manager import UserWarehouse


def is_unrestricted(user) -> bool:
    """True for users the warehouse rules do not apply to."""
    return bool(user and getattr(user, "is_superuser", False))


def managed_warehouses(user, company_code: str) -> frozenset:
    """Upper-cased warehouse codes this user manages in this company.

    Empty for an unassigned user. Callers must not read an empty set as
    "unrestricted" — that is exactly backwards here — which is why the assert
    helpers below exist and should be preferred.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return frozenset()
    rows = UserWarehouse.objects.filter(
        user=user,
        company__code=company_code,
        is_active=True,
    ).values_list("warehouse_code", flat=True)
    return frozenset(code.strip().upper() for code in rows if code)


def manages(user, company_code: str, warehouse: str) -> bool:
    """Convenience predicate — for hiding a button, not for guarding a write."""
    if is_unrestricted(user):
        return True
    if not warehouse:
        return False
    return warehouse.strip().upper() in managed_warehouses(user, company_code)


def assert_manages(
    user,
    company_code: str,
    warehouses: Iterable[str],
    *,
    action: str,
    blank_ok: bool = False,
) -> None:
    """Refuse unless the user manages EVERY warehouse named.

    All of them, not any: a BST may combine documents from several source
    warehouses, and letting one managed warehouse in the set authorise the whole
    shipment would let a manager ship another site's stock by attaching one of
    their own documents to it.

    `blank_ok` covers the columns that are legitimately empty — an INVOICE BST
    has no destination warehouse at all (it settles to a company), and a combined
    BST leaves `sap_from_warehouse` blank when its documents span warehouses.
    Note what `blank_ok` does NOT do: it never waves through a user who manages
    nothing. Missing SAP data must not become a way for an unassigned user to
    act, so the "are you a manager at all" check runs first and applies either
    way; only the per-warehouse comparison is skipped.
    """
    if is_unrestricted(user):
        return

    allowed = managed_warehouses(user, company_code)
    if not allowed:
        raise PermissionDenied(
            f"Cannot {action}: you are not set as the manager of any warehouse "
            "in this company. An administrator assigns this on Admin → "
            "Warehouse Managers."
        )

    wanted = {(w or "").strip().upper() for w in warehouses}
    wanted.discard("")
    if not wanted:
        if blank_ok:
            return
        raise PermissionDenied(
            f"Cannot {action}: no warehouse is named on it, so it cannot be "
            "checked against the warehouses you manage."
        )

    missing = sorted(wanted - allowed)
    if missing:
        raise PermissionDenied(
            f"Cannot {action}: you do not manage "
            f"{', '.join(missing)}. You manage {', '.join(sorted(allowed))}."
        )


def assert_can_send_from(user, company_code: str, warehouses, **kwargs) -> None:
    """Guard the sending side — raising a transfer request, creating a BST."""
    assert_manages(
        user, company_code, warehouses, action="send stock out", **kwargs
    )


def assert_can_receive_into(user, company_code: str, warehouses, **kwargs) -> None:
    """Guard the receiving side — approving a request, receiving a BST."""
    assert_manages(
        user, company_code, warehouses, action="accept stock", **kwargs
    )


def users_missing_assignment(company_code: Optional[str] = None):
    """Users who hold a warehouse-movement permission but manage nothing.

    These are precisely the people the "no assignment means no access" rule
    would stop, so this is what the report command and the config page's warning
    banner are built on.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from django.contrib.auth.models import Permission

    codenames = [
        "can_create_transfer_request",
        "can_approve_transfer_request",
        "can_create_bst",
        "can_dispatch_bst",
        "can_receive_bst",
    ]
    # A mistyped codename would make this report empty, which reads as "nobody
    # is affected" — the most dangerous possible wrong answer for a lockout
    # check. Fail loudly instead.
    known = set(
        Permission.objects.filter(
            content_type__app_label="warehouse", codename__in=codenames
        ).values_list("codename", flat=True)
    )
    missing_perms = sorted(set(codenames) - known)
    if missing_perms:
        raise RuntimeError(
            "warehouse_scope: unknown permission codename(s) "
            f"{missing_perms} — run migrations, or fix the list."
        )

    User = get_user_model()
    holders = (
        User.objects.filter(is_active=True, is_superuser=False)
        .filter(
            Q(user_permissions__codename__in=codenames)
            | Q(groups__permissions__codename__in=codenames)
        )
        .distinct()
    )

    assigned = UserWarehouse.objects.filter(is_active=True)
    if company_code:
        assigned = assigned.filter(company__code=company_code)
    assigned_ids = set(assigned.values_list("user_id", flat=True))

    return [u for u in holders if u.id not in assigned_ids]
