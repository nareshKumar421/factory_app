"""Who may see and run which SAP report.

The same shape, and the same two deliberately chosen rules, as
``warehouse.services.warehouse_scope``:

**No assignment means no access.** A user with only the view permission sees
exactly the reports an admin has assigned to them -- an unassigned user sees
none. The stricter reading was chosen so the restriction cannot be bypassed by
simply never configuring somebody.

**Administrators are exempt.** Superusers, and anyone holding
``can_manage_sap_reports``, see everything: they run the catalogue and the
assignment page, and scoping them would deadlock the first deploy -- nobody
could assign the reports they cannot see.
"""


def is_unrestricted(user) -> bool:
    """True for users the report scoping does not apply to."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user.has_perm("sap_reports.can_manage_sap_reports")


def accessible_reports(user, reports):
    """Narrows a ``SapReport`` queryset to what this user was assigned.

    Callers must not read an unfiltered queryset as "allowed" -- always go
    through here. Empty for an unassigned user, untouched for an exempt one.
    """
    if is_unrestricted(user):
        return reports
    if not user or not getattr(user, "is_authenticated", False):
        return reports.none()
    return reports.filter(access_grants__user=user, access_grants__is_active=True)
